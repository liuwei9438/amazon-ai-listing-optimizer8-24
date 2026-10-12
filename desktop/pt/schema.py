# -*- coding: utf-8 -*-
"""pt.schema —— 模块 6：Schema 下载、缓存及属性缺口检查
（编号统一见 2026-10-12 批复：6=Schema 与属性）。

只做三件事：①ensure_schema=官方 Product Type Definition → 受限下载
Schema → 校验 → 原子落盘 → 缓存元数据；②analyze_schema=把官方 JSON
Schema 解析成逐属性要求清单；③attribute_gap_report=对照商品材料出
属性缺口报告。铁律（模块 6 批复二·10 条 + 三·安全）：

1. 权威来源唯一：Schema 只经 getDefinitionsProductType 官方响应里的
   schema.link 下载（URL 由官方给出，不经任何二手转述）。
2. 受限下载在客户端强制：http(s)-only、宿主沿用环境传输层白名单
   （mock 只准 mock 服务；live 只准官方 SP-API 域名）、live 仅 https、
   不带令牌（签名 URL 自带授权）、大小/超时/有界重试上帽——本模块
   只再叠一层大小帽与官方 checksum 校验。
3. 原子缓存：临时文件写全 + fsync + os.replace 原子替换，不完整文件
   绝不进入有效缓存；官方 checksum 不符=hash_mismatch，缓存原样保留
   绝不写入；残留 .tmp 永远清理。
4. 缓存键全敏感（store.schema_cache_key）：站点×PT×卖家×locale×
   requirements×requirementsEnforced×parentageLevel×版本×env——
   Mock/Live 缓存严格隔离、绝不跨店铺复用卖家特定 Schema。
5. 元数据与文件一致（批复 ⑥）：命中缓存先验哈希，不一致自动标
   corrupt；文件丢失标 missing；refresh_schema_meta 全量巡检可恢复；
   版本推进靠「复核期到了重拉定义比对 productTypeVersion」——版本
   未变只续期不重下，变了才重下。
6. 分析器绝不误判通过：$ref（文档内）解析、allOf 合并、if/then/else
   条件必填、枚举/pattern/format/数组与嵌套对象递归；外部 $ref、
   anyOf/oneOf/not、超深/超量、无法编译的 pattern 一律标 unknown——
   unknown_count 显式计数，绝不悄悄当 pass。
7. 缺口报告五分类（批复 ⑧）：ok=已有可靠数据 / missing_required=
   缺必填 / conditional_pending=条件必填待判断 / format_mismatch=
   格式不符 / undeterminable=无法确定；每项带来源与理由。
8. 绝不编造商品事实（批复 ⑨）：FACT_SOURCES 只映射材料里真实存在
   的字段（标题/型号/理解出的 product_type 等）；brand/manufacturer/
   GTIN/认证/材质/尺寸等没有来源的必填一律 missing_required「需人工
   提供」，报告恒带 fabrication_guard 声明。
9. 只分析不代工（批复 ⑩）：不修改商品资料、不生成上传请求、不写
   「可刊登」结论——summary 最高只到 complete_for_review。
10. 批量安全（批复三）：ensure_schema 永不抛异常——定义/下载/落盘
    任一环节失败只降级 outcome 状态，绝不带崩批量任务；日志不记
    令牌/签名 URL 敏感参数（客户端已保证，本模块不再转述 URL 查询串）。

纯业务逻辑 + 注入式：client/store 注入，now_ts 可注入可测；不碰
main/上传/图片模块，不改现有上传流程。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pt.material import ProductMaterial
from pt.spapi_client import (
    SpApiClient, SpApiAuthError, SpApiBadRequestError, SpApiEnvironmentError,
    SpApiError, SpApiForbiddenError, SpApiNetworkError, SpApiNotFoundError,
    SpApiParseError, SpApiRateLimitError, SpApiServerError,
)

_LOG = logging.getLogger("pt.schema")

# ---- 通用常量（与商品无关；安全上限集中在这里） ----
SCHEMA_DIR = "schemas"                 # cache_root 下的子目录
MAX_SCHEMA_BYTES = 2_000_000           # 单个 Schema 大小帽（2MB，批复三）
SCHEMA_RECHECK_SECONDS = 3600          # 命中缓存多久复核一次官方版本
MAX_ANALYSIS_DEPTH = 12                # 嵌套解析深度帽（超了=unknown）
MAX_ANALYSIS_PROPS = 2000              # 属性条目帽（超了=截断+unknown）
ENUM_PREVIEW = 50                      # 枚举值入报告的上限
FACT_PREVIEW = 60                      # 事实值入报告的截断长度

# 缓存元数据状态（store.pt_schema_meta.status）
ST_OK = "ok"
ST_MISSING = "missing"
ST_CORRUPT = "corrupt"
ST_PENDING = "pending"

# SchemaOutcome.status（对外口径，区别于 store 元数据状态）
OUT_OK = "ok"
OUT_UNAVAILABLE = "unavailable"        # 官方定义/下载暂不可得（可重试）
OUT_FORBIDDEN = "forbidden"            # 权限不足（401/403）
OUT_HASH_MISMATCH = "hash_mismatch"    # 官方 checksum 不符——拒绝入缓存
OUT_NO_LINK = "no_link"                # 官方响应没有 schema 链接
OUT_ERROR = "error"                    # 己方参数/策略拒绝（URL 违规/超限/写盘失败）

# 缺口五分类（批复 ⑧）
GAP_OK = "ok"
GAP_MISSING = "missing_required"
GAP_CONDITIONAL = "conditional_pending"
GAP_FORMAT = "format_mismatch"
GAP_UNDETERMINABLE = "undeterminable"
GAP_ORDER = (GAP_OK, GAP_MISSING, GAP_CONDITIONAL, GAP_FORMAT,
             GAP_UNDETERMINABLE)

FABRICATION_GUARD = (
    "本报告只对照官方 Schema 检查属性要求：不修改商品资料、不生成上传"
    "请求；品牌/制造商/GTIN/认证/材质/尺寸等商品事实一律不编造——"
    "缺项须由人工或真实数据源提供。")

# 商品事实映射（批复 ⑨：只映射材料里真实存在的来源；没映射 ≠ 可编造）
FACT_SOURCES: dict[str, tuple[str, Callable[[ProductMaterial], Any]]] = {
    "item_name": ("material.title", lambda m: m.title),
    "model_number": ("material.model", lambda m: m.model),
    "feed_product_type": ("understanding.basic_info.product_type",
                          lambda m: m.basic_info_product_type),
    "product_description": ("material.description", lambda m: m.description),
    "bullet_point": ("material.bullets", lambda m: m.bullets or None),
}


# ---------------------------------------------------------------------------
# 错误分类（与 verifier 同一套词汇；独立小函数避免跨模块拉私有名）
# ---------------------------------------------------------------------------


def _err_kind(e: BaseException) -> str:
    for exc, kind in (
        (SpApiEnvironmentError, "environment"),
        (SpApiAuthError, "auth"),
        (SpApiForbiddenError, "forbidden"),
        (SpApiRateLimitError, "rate_limit"),
        (SpApiNetworkError, "network"),
        (SpApiNotFoundError, "not_found"),
        (SpApiParseError, "parse"),
        (SpApiServerError, "server_error"),
        (SpApiBadRequestError, "bad_request"),
        (SpApiError, "spapi"),
    ):
        if isinstance(e, exc):
            return kind
    if isinstance(e, ValueError):
        return "bad_params"
    return "internal"


def _err_detail(e: BaseException) -> dict:
    if isinstance(e, SpApiError):
        d = {"kind": _err_kind(e), "error": type(e).__name__,
             "official_messages": e.official_messages()[:3]}
        if e.request_id:
            d["request_id"] = e.request_id
        return d
    return {"kind": _err_kind(e),
            "error": f"{type(e).__name__}: {e}"[:200]}


def _unavailable_or_forbidden(kind: str) -> str:
    return OUT_FORBIDDEN if kind in ("auth", "forbidden") else OUT_UNAVAILABLE


# ---------------------------------------------------------------------------
# 哈希 / 路径 / 原子写
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5_b64(data: bytes) -> str:
    """官方 checksum 同式：MD5 摘要 base64（与 mock 服务一致）。"""
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def schemas_dir(cache_root: str | os.PathLike) -> Path:
    d = Path(cache_root) / SCHEMA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_filename(cache_key: str) -> str:
    """缓存文件名=sha256(cache_key)——键里有卖家/令牌无关但敏感的上下文，
    文件名本身不回显任何键内容。"""
    return _sha256_hex(cache_key.encode("utf-8"))[:40] + ".json"


def _atomic_write(final: Path, raw: bytes) -> bool:
    """临时文件 + fsync + os.replace 原子替换（批复 ④）：
    不完整文件绝不占据有效缓存名；.tmp 残留一律清理。"""
    tmp = final.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex}.json"
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
        return True
    except OSError:
        return False
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _mark(store, meta: dict, status: str, t: int) -> None:
    """只翻 status（其余参数原样保留）——元数据与文件不一致时自动标记。"""
    try:
        store.upsert_schema_meta(
            marketplace_id=meta.get("marketplace_id") or "",
            pt_name=meta.get("pt_name") or "",
            seller_id=meta.get("seller_id") or "",
            locale=meta.get("locale") or "",
            requirements=meta.get("requirements") or "",
            requirements_enforced=meta.get("requirements_enforced") or "",
            parentage_level=meta.get("parentage_level") or "",
            product_type_version=meta.get("product_type_version") or "",
            payload_sha256=meta.get("payload_sha256"),
            payload_path=meta.get("payload_path"),
            status=status, fetched_at=meta.get("fetched_at"),
            env=meta.get("env") or "live",
            cache_key=meta.get("cache_key"))
    except Exception:  # noqa: BLE001 —— 状态翻转绝不能带崩批量
        _LOG.warning("schema meta 状态翻转失败（忽略）key=%r",
                     meta.get("cache_key"), exc_info=True)


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass
class SchemaOutcome:
    """一次 ensure_schema 的结论（永不抛异常；批量安全，批复三）。"""
    pt_name: str
    marketplace_id: str
    cache_key: str
    status: str = OUT_OK          # 见 OUT_* 常量
    from_cache: bool = False
    schema: dict | None = None    # status=ok 时的官方 Schema（已解析）
    payload_path: str | None = None
    sha256: str | None = None
    product_type_version: str | None = None
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ① Schema 下载与缓存
# ---------------------------------------------------------------------------


def ensure_schema(client: SpApiClient, store, cache_root: str,
                  *, pt_name: str, marketplace_id: str, seller_id: str = "",
                  locale: str = "", requirements: str = "",
                  requirements_enforced: str = "", parentage_level: str = "",
                  product_type_version: str = "", env: str = "live",
                  now_ts: int | None = None, force: bool = False,
                  recheck_after: int = SCHEMA_RECHECK_SECONDS
                  ) -> SchemaOutcome:
    """取一份可用 Schema：缓存新鲜→直接用；复核期到/force→重拉官方
    定义比对版本再定。任何失败只降级 status，绝不抛异常（批复三）。"""
    pt = str(pt_name or "").strip()
    t = int(now_ts if now_ts is not None else time.time())
    key = store.schema_cache_key(
        marketplace_id, pt, seller_id, locale, requirements,
        requirements_enforced, parentage_level, product_type_version, env)
    out = SchemaOutcome(pt_name=pt, marketplace_id=marketplace_id,
                        cache_key=key)
    if not pt:
        out.status = OUT_ERROR
        out.detail = {"kind": "bad_params", "error": "pt_name 不能为空"}
        return out
    try:
        return _ensure(client, store, cache_root, out, pt=pt, key=key,
                       marketplace_id=marketplace_id, seller_id=seller_id,
                       locale=locale, requirements=requirements,
                       requirements_enforced=requirements_enforced,
                       parentage_level=parentage_level,
                       product_type_version=product_type_version, env=env,
                       t=t, force=force, recheck_after=recheck_after)
    except Exception as e:  # noqa: BLE001 —— 批量安全兜底
        out.status = OUT_ERROR
        out.detail = {"kind": _err_kind(e),
                      "error": f"{type(e).__name__}: {e}"[:200]}
        _LOG.warning("ensure_schema 兜底（批量不崩）pt=%s kind=%s", pt,
                     out.detail["kind"], exc_info=True)
        return out


def _ensure(client: SpApiClient, store, cache_root: str,
            out: SchemaOutcome, *, pt: str, key: str, marketplace_id: str,
            seller_id: str, locale: str, requirements: str,
            requirements_enforced: str, parentage_level: str,
            product_type_version: str, env: str, t: int, force: bool,
            recheck_after: int) -> SchemaOutcome:
    sdir = schemas_dir(cache_root)
    final = sdir / _cache_filename(key)
    meta = store.get_schema_meta(key)

    # ① 缓存命中判定：元数据 ok + 文件在 + 哈希一致 + 未到复核期
    if (meta is not None and meta.get("status") == ST_OK
            and meta.get("payload_sha256") and not force
            and final.is_file()):
        raw = final.read_bytes()
        if _sha256_hex(raw) != meta["payload_sha256"]:
            _mark(store, meta, ST_CORRUPT, t)   # 元数据/文件不一致→标不可用
            meta = store.get_schema_meta(key)
        elif t - int(meta.get("fetched_at") or 0) < max(int(recheck_after), 0):
            out.status = OUT_OK
            out.from_cache = True
            out.schema = json.loads(raw.decode("utf-8"))
            out.sha256 = meta["payload_sha256"]
            out.payload_path = str(final)
            out.product_type_version = meta.get("product_type_version") or None
            out.detail = {"note": "缓存命中：哈希一致且未到版本复核期",
                          "age_seconds": max(
                              t - int(meta.get("fetched_at") or 0), 0)}
            return out
        # 复核期到 → 往下走 ② 重拉定义比对版本（未变则只续期不重下）
    elif meta is not None and meta.get("payload_path") and not final.is_file():
        _mark(store, meta, ST_MISSING, t)
        meta = store.get_schema_meta(key)

    # ② 官方 Product Type Definition（版本/链接/checksum 的唯一权威来源）
    try:
        r = client.get_product_type(
            pt, marketplace_id,
            seller_id=seller_id or None,
            product_type_version=product_type_version or None,
            requirements=requirements or None,
            requirements_enforced=requirements_enforced or None,
            locale=locale or None,
            parentage_level=parentage_level or None)
    except (ValueError, SpApiError) as e:
        out.status = _unavailable_or_forbidden(_err_kind(e))
        out.detail = _err_detail(e)
        out.detail["note"] = "官方定义获取失败——缓存（若有）原样保留"
        return out
    data = r.data if isinstance(r.data, dict) else {}
    version = str(data.get("productTypeVersion") or "")

    # ③ 版本未变 + 文件完好 → 只续期元数据（不重复下载）
    if (meta is not None and meta.get("status") == ST_OK and version
            and meta.get("product_type_version") == version and not force
            and final.is_file()):
        raw = final.read_bytes()
        if _sha256_hex(raw) == meta.get("payload_sha256"):
            store.upsert_schema_meta(
                marketplace_id=marketplace_id, pt_name=pt,
                seller_id=seller_id, locale=locale,
                requirements=requirements,
                requirements_enforced=requirements_enforced,
                parentage_level=parentage_level,
                product_type_version=version,
                payload_sha256=meta["payload_sha256"],
                payload_path=meta.get("payload_path"), status=ST_OK,
                fetched_at=t, env=env, cache_key=key)
            out.status = OUT_OK
            out.from_cache = True
            out.schema = json.loads(raw.decode("utf-8"))
            out.sha256 = meta["payload_sha256"]
            out.payload_path = str(final)
            out.product_type_version = version
            out.detail = {"note": "官方版本未变，缓存续期（未重复下载）",
                          "productTypeVersion": version}
            return out

    schema_obj = data.get("schema") if isinstance(data.get("schema"), dict) \
        else {}
    link = (schema_obj.get("link") or {}).get("resource") \
        if isinstance(schema_obj.get("link"), dict) else None
    checksum = schema_obj.get("checksum")
    if not link:
        out.status = OUT_NO_LINK
        out.detail = {"kind": "no_link",
                      "note": "官方响应缺 schema.link——无 Schema 可下载",
                      "productTypeVersion": version or None}
        return out

    # ④ 受限下载（宿主白名单/大小/超时/重试由客户端强制；此处再叠大小帽）
    try:
        res = client.fetch_schema(link, max_bytes=MAX_SCHEMA_BYTES)
    except ValueError as e:
        msg = str(e)
        out.status = OUT_ERROR
        out.detail = {"kind": "too_large" if "超过上限" in msg else "bad_link",
                      "error": msg[:200]}
        return out
    except SpApiError as e:
        out.status = _unavailable_or_forbidden(_err_kind(e))
        out.detail = _err_detail(e)
        out.detail["note"] = "Schema 下载失败——缓存（若有）原样保留"
        return out
    raw = res.raw
    if not raw:
        out.status = OUT_UNAVAILABLE
        out.detail = {"kind": "empty", "error": "Schema 响应为空字节"}
        return out

    # ⑤ 官方 checksum 校验（批复 ③）：不符=拒绝入缓存，绝不写坏文件
    if checksum:
        actual = _md5_b64(raw)
        if actual != str(checksum):
            out.status = OUT_HASH_MISMATCH
            out.detail = {
                "kind": "checksum_mismatch",
                "note": "官方 checksum 不符——拒绝入缓存（缓存原样保留）",
                "expected_md5_b64": str(checksum), "actual_md5_b64": actual}
            return out

    # ⑥ 原子落盘 + 元数据（批复 ④⑤⑥）
    if not _atomic_write(final, raw):
        out.status = OUT_ERROR
        out.detail = {"kind": "write_failed",
                      "error": "临时文件写入/替换失败（.tmp 已清理）"}
        return out
    sha = _sha256_hex(raw)
    store.upsert_schema_meta(
        marketplace_id=marketplace_id, pt_name=pt, seller_id=seller_id,
        locale=locale, requirements=requirements,
        requirements_enforced=requirements_enforced,
        parentage_level=parentage_level, product_type_version=version,
        payload_sha256=sha, payload_path=final.name, status=ST_OK,
        fetched_at=t, env=env, cache_key=key)
    out.status = OUT_OK
    out.schema = res.data if isinstance(res.data, dict) \
        else json.loads(raw.decode("utf-8"))
    out.sha256 = sha
    out.payload_path = str(final)
    out.product_type_version = version or None
    out.detail = {"note": "官方下载完成：checksum 校验通过，原子落盘",
                  "productTypeVersion": version or None,
                  "bytes": len(raw), "attempts": res.attempts}
    return out


def refresh_schema_meta(store, cache_root: str, *, marketplace_id: str,
                        pt_name: str | None = None,
                        seller_id: str | None = None, env: str = "live"
                        ) -> dict:
    """全量巡检（批复 ⑥/三）：逐行重哈希——文件在且哈希一致=ok；不一致
    =corrupt；文件丢失=missing。损坏可恢复：ensure_schema 下次自动重下。
    返回统计（不抛异常——巡检属于批量任务的伴随步骤）。"""
    stats = {"checked": 0, ST_OK: 0, ST_CORRUPT: 0, ST_MISSING: 0,
             "skipped_no_payload": 0}
    t = int(time.time())
    root = Path(cache_root)
    try:
        rows = store.schema_metas_for(marketplace_id, pt_name=pt_name,
                                      seller_id=seller_id, env=env)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("refresh_schema_meta 读元数据失败：%r", e)
        return stats
    for row in rows:
        sha = row.get("payload_sha256")
        path = row.get("payload_path")
        if not sha or not path:
            stats["skipped_no_payload"] += 1
            continue
        stats["checked"] += 1
        f = root / SCHEMA_DIR / Path(str(path)).name
        if not f.is_file():
            _mark(store, row, ST_MISSING, t)
            stats[ST_MISSING] += 1
        elif _sha256_hex(f.read_bytes()) != sha:
            _mark(store, row, ST_CORRUPT, t)
            stats[ST_CORRUPT] += 1
        else:
            stats[ST_OK] += 1
    return stats


# ---------------------------------------------------------------------------
# ② Schema 分析（批复 ⑦：$ref/allOf/条件必填/枚举/数组/嵌套对象；
#    解析不了的约束一律 unknown，绝不误判为通过）
# ---------------------------------------------------------------------------


@dataclass
class PropertyReq:
    """单个属性要求（path 点路径；数组元素用 name[]）。"""
    path: str
    name: str
    types: list[str] = field(default_factory=list)
    requiredness: str = "optional"    # required|conditional|optional|unknown
    enum: list | None = None
    pattern: str | None = None
    format: str | None = None
    condition: str | None = None      # conditional 时的条件摘要
    unknown_reason: str | None = None


@dataclass
class SchemaAnalysis:
    properties: list[PropertyReq]
    unknown_count: int
    note: str = ("无法完整解析的约束一律标 unknown，绝不误判为通过"
                 "（模块 6 批复 ⑦）")

    def find(self, path: str) -> PropertyReq | None:
        for p in self.properties:
            if p.path == path:
                return p
        return None

    def by_category(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for p in self.properties:
            out.setdefault(p.requiredness, []).append(p.path)
        return out


_REQ_ORDER = {"unknown": 0, "required": 1, "conditional": 2, "optional": 3}


def _cond_summary(node: Any) -> str:
    try:
        return json.dumps(node, ensure_ascii=False, sort_keys=True)[:120]
    except (TypeError, ValueError):
        return "<条件不可序列化>"


def analyze_schema(schema: dict) -> SchemaAnalysis:
    """官方 JSON Schema → 逐属性要求清单。支持：文档内 $ref（#/…）、
    allOf 合并、if/then/else 条件必填、enum/pattern/format/type（含数组
    items 与嵌套对象递归）。标 unknown 的情形：外部/失效 $ref、anyOf/
    oneOf/not 分支属性、超解析深度、超属性条目上限——unknown 永不被
    当作通过。"""
    root = schema if isinstance(schema, dict) else {}
    props: dict[str, PropertyReq] = {}
    unknowns: list[str] = []

    def resolve_ref(ref: str) -> tuple[Any, str | None]:
        """文档内 #/ 指针解析；外部 $ref 返回 (None, ref) 标 unknown。"""
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None, ref or "<空引用>"
        node: Any = root
        for seg in ref[2:].split("/"):
            seg = seg.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and seg in node:
                node = node[seg]
            elif isinstance(node, list) and seg.isdigit() and \
                    int(seg) < len(node):
                node = node[int(seg)]
            else:
                return None, ref
        return node, None

    def merge(a: dict, b: dict) -> dict:
        """allOf/$ref 合并：required 并集；properties 递归合并；其余后者胜。"""
        out = dict(a)
        for k, v in b.items():
            if k == "required" and k in out and isinstance(out[k], list):
                out[k] = list(dict.fromkeys(list(out[k]) + list(v or [])))
            elif k == "properties" and isinstance(out.get(k), dict) \
                    and isinstance(v, dict):
                merged = dict(out[k])
                for pn, pnode in v.items():
                    if pn in merged and isinstance(merged[pn], dict) \
                            and isinstance(pnode, dict):
                        merged[pn] = merge(merged[pn], pnode)
                    else:
                        merged[pn] = pnode
                out[k] = merged
            else:
                out[k] = v
        return out

    def expand(node: Any, depth: int = 0) -> tuple[dict, list[str]]:
        """解析本节点的 $ref + allOf → 合并视图；解析不了的记 unknown。"""
        if depth > MAX_ANALYSIS_DEPTH:
            return {}, ["超出解析深度"]
        if not isinstance(node, dict):
            return {}, []
        unknown: list[str] = []
        merged = {k: v for k, v in node.items()
                  if k not in ("$ref", "allOf")}
        ref = node.get("$ref")
        if isinstance(ref, str):
            target, bad = resolve_ref(ref)
            if target is None:
                label = "外部引用" if not str(ref).startswith("#") \
                    else "指针失效"
                unknown.append(f"$ref 未解析（{label}：{str(ref)[:80]}）")
            else:
                m2, u2 = expand(target, depth + 1)
                unknown.extend(u2)
                merged = merge(merged, m2)
        for sub in (node.get("allOf") or []):
            if isinstance(sub, dict):
                m2, u2 = expand(sub, depth + 1)
                unknown.extend(u2)
                merged = merge(merged, m2)
        return merged, unknown

    def bump(path: str, name: str, **kw) -> None:
        p = props.get(path)
        if p is None:
            if len(props) >= MAX_ANALYSIS_PROPS:
                return
            p = props[path] = PropertyReq(path=path, name=name)
        for k, v in kw.items():
            if v is None:
                continue
            if k == "requiredness":
                if _REQ_ORDER[v] < _REQ_ORDER[p.requiredness]:
                    p.requiredness = v        # unknown 一票否决，required 次之
            elif not getattr(p, k, None):     # None 或空容器才允许首填
                setattr(p, k, v)

    def walk(node: Any, path: str, depth: int) -> None:
        if len(props) >= MAX_ANALYSIS_PROPS:
            unknowns.append("属性条目超过解析上限，其余截断")
            return
        view, unknown = expand(node, depth)
        name = path.rsplit(".", 1)[-1].removesuffix("[]") if path else ""
        if path:
            t = view.get("type")
            types = [str(x) for x in t] if isinstance(t, list) \
                else ([t] if isinstance(t, str) else [])
            bump(path, name, types=types,
                 enum=[e for e in (view.get("enum") or [])
                       ][:ENUM_PREVIEW] if isinstance(view.get("enum"),
                                                      list) else None,
                 pattern=view.get("pattern") if isinstance(
                     view.get("pattern"), str) else None,
                 format=view.get("format") if isinstance(
                     view.get("format"), str) else None)
        if unknown:
            if path:
                bump(path, name, requiredness="unknown",
                     unknown_reason="；".join(dict.fromkeys(unknown))[:200])
            unknowns.extend(f"{path or '<根>'}:{u}" for u in unknown)
        req_here = {str(r) for r in (view.get("required") or [])
                    if isinstance(r, str)}
        cond: dict[str, str] = {}
        if isinstance(view.get("if"), dict):
            summary = _cond_summary(view["if"])
            for branch in ("then", "else"):
                b = view.get(branch)
                if isinstance(b, dict):
                    m2, _ = expand(b, depth + 1)
                    for r in (m2.get("required") or []):
                        if isinstance(r, str):
                            cond.setdefault(r, summary)
        # anyOf/oneOf/not 组合约束未完全解析：分支属性一律 unknown（铁律 6）
        for comb in ("anyOf", "oneOf", "not"):
            if comb in view:
                branches = view[comb] if isinstance(view[comb], list) \
                    else [view[comb]]
                for sub in branches:
                    if not isinstance(sub, dict):
                        continue
                    for pname in (sub.get("properties") or {}):
                        cpath = f"{path}.{pname}" if path else pname
                        bump(cpath, pname, requiredness="unknown",
                             unknown_reason=f"{comb} 组合约束未完全解析")
                        unknowns.append(cpath)
        prop_defs = view.get("properties") or {}
        for pname, pnode in (prop_defs.items()
                             if isinstance(prop_defs, dict) else []):
            if not isinstance(pname, str):
                continue
            cpath = f"{path}.{pname}" if path else pname
            if pname in req_here:
                bump(cpath, pname, requiredness="required")
            elif pname in cond:
                bump(cpath, pname, requiredness="conditional",
                     condition=cond[pname])
            else:
                bump(cpath, pname, requiredness="optional")
            walk(pnode, cpath, depth + 1)      # 本节点自身标量约束（含 array）
            if isinstance(pnode, dict) and pnode.get("type") == "array" \
                    and isinstance(pnode.get("items"), dict):
                walk(pnode["items"], cpath + "[]", depth + 1)
        # 必填/条件必填但官方没给属性定义的——照样入清单（不能漏报）
        for r in req_here:
            if r not in (prop_defs or {}):
                cpath = f"{path}.{r}" if path else r
                bump(cpath, r, requiredness="required",
                     unknown_reason="官方 Schema 未给该属性的定义节点"
                                    "——仍按必填报告")
        for r, summary in cond.items():
            if r not in (prop_defs or {}):
                cpath = f"{path}.{r}" if path else r
                bump(cpath, r, requiredness="conditional", condition=summary)

    walk(root, "", 0)
    return SchemaAnalysis(properties=[props[k] for k in sorted(props)],
                          unknown_count=len(dict.fromkeys(unknowns)))


# ---------------------------------------------------------------------------
# ③ 属性缺口报告（批复 ⑧⑨⑩）
# ---------------------------------------------------------------------------


@dataclass
class GapEntry:
    """单个属性的缺口判定。"""
    path: str
    name: str
    requiredness: str          # 来自 SchemaAnalysis
    category: str              # GAP_* 五分类
    fact_source: str | None    # FACT_SOURCES 来源说明；None=无映射来源
    fact_preview: str | None   # 事实值预览（截断）；None=无事实
    reason: str
    constraint: dict = field(default_factory=dict)


@dataclass
class GapReport:
    pt_name: str
    marketplace_id: str
    entries: list[GapEntry]
    counts: dict
    summary: str                      # 恒非放行结论；最高 complete_for_review
    fabrication_guard: str = FABRICATION_GUARD


def _fact_of(material: ProductMaterial, name: str
             ) -> tuple[str | None, Any]:
    src = FACT_SOURCES.get(name)
    if src is None:
        return None, None
    try:
        val = src[1](material)
    except Exception:  # noqa: BLE001 —— 取事实失败按无事实处理，绝不编造
        return src[0], None
    return src[0], val


def _present(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, tuple, dict)):
        return len(val) > 0
    return True


def _validate_fact(val: Any, p: PropertyReq) -> tuple[str, str]:
    """类型/枚举/pattern 校验。返回 (ok|mismatch|unknown, 理由)。
    unknown=约束本身校验不了（类型关键词不识/pattern 编译失败）——
    绝不因校验不了而放行。"""
    if p.enum:
        vals = [str(e) for e in p.enum]
        s = str(val)
        if s not in vals and s.upper() not in {v.upper() for v in vals}:
            return "mismatch", f"取值不在官方枚举内：{p.enum[:8]}"
    if p.pattern:
        try:
            hit = re.search(p.pattern, str(val)) is not None
        except re.error:
            return "unknown", "官方 pattern 本引擎无法编译——无法校验"
        if not hit:
            return "mismatch", f"不符合官方格式约束 pattern={p.pattern}"
    types = [t for t in p.types if t != "null"]
    if types:
        def is_num(x: Any) -> bool:
            return isinstance(x, (int, float)) and not isinstance(x, bool) \
                or (isinstance(x, str) and re.fullmatch(
                    r"-?\d+(\.\d+)?", x.strip()) is not None)
        ok_map = {
            "string": lambda x: isinstance(x, str),
            "integer": lambda x: isinstance(x, int) and not isinstance(
                x, bool) or (isinstance(x, str) and re.fullmatch(
                    r"-?\d+", x.strip()) is not None),
            "number": is_num,
            "boolean": lambda x: isinstance(x, bool),
            "array": lambda x: isinstance(x, (list, tuple)),
            "object": lambda x: isinstance(x, dict),
        }
        if not any(t in ok_map for t in types):
            return "unknown", f"官方类型关键词本引擎不识别：{p.types}"
        if not any(t in ok_map and ok_map[t](val) for t in types):
            return "mismatch", f"数据类型与官方要求不符（要 {p.types}，" \
                               f"实为 {type(val).__name__}）"
    return "ok", ""


def attribute_gap_report(material: ProductMaterial, analysis: SchemaAnalysis,
                         *, pt_name: str = "",
                         marketplace_id: str = "") -> GapReport:
    """对照商品材料出属性缺口报告（五分类，批复 ⑧；不编造，批复 ⑨；
    只分析不代工，批复 ⑩）。可选属性不构成缺口（不进 entries）。"""
    entries: list[GapEntry] = []
    optional_skipped = 0
    for p in analysis.properties:
        if p.requiredness == "optional":
            optional_skipped += 1
            continue
        fact_source, val = _fact_of(material, p.name)
        has_fact = _present(val)
        preview = str(val)[:FACT_PREVIEW] if has_fact else None
        constraint = {"types": p.types, "enum": p.enum, "pattern": p.pattern,
                      "format": p.format}
        if p.requiredness == "unknown":
            category = GAP_UNDETERMINABLE
            reason = p.unknown_reason or "Schema 约束无法完整解析——按无法确定报告"
        elif not has_fact and p.requiredness == "conditional":
            category = GAP_CONDITIONAL
            reason = (f"条件必填待判断（条件：{p.condition or '未解析'}），"
                      "当前无数据")
            constraint["condition"] = p.condition
        elif not has_fact and p.requiredness == "required":
            category = GAP_MISSING
            reason = ("无映射来源——需人工提供（品牌/GTIN/认证/材质等商品"
                      "事实绝不编造）" if fact_source is None
                      else f"来源 {fact_source} 无值")
        else:
            verdict, why = _validate_fact(val, p)
            if verdict == "ok":
                category = GAP_OK
                reason = f"已有可靠数据（来源 {fact_source}）"
            elif verdict == "mismatch":
                category = GAP_FORMAT
                reason = why
            else:
                category = GAP_UNDETERMINABLE
                reason = why
            if p.requiredness == "conditional":
                constraint["condition"] = p.condition
        entries.append(GapEntry(
            path=p.path, name=p.name, requiredness=p.requiredness,
            category=category, fact_source=fact_source,
            fact_preview=preview, reason=reason, constraint=constraint))
    counts = {c: sum(1 for e in entries if e.category == c)
              for c in GAP_ORDER}
    counts["optional_skipped"] = optional_skipped
    blocking = sum(counts[c] for c in (GAP_MISSING, GAP_CONDITIONAL,
                                       GAP_FORMAT, GAP_UNDETERMINABLE))
    summary = "complete_for_review" if (not entries or not blocking) \
        else "incomplete"
    return GapReport(pt_name=pt_name, marketplace_id=marketplace_id,
                     entries=entries, counts=counts, summary=summary)

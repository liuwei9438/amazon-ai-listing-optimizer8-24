# -*- coding: utf-8 -*-
"""pt.candidates —— 第一阶段模块 3（乙）：三策略真实候选检索。

只做候选，不做最终 AI 选择（选择在模块 4 matcher，编号统一见
2026-10-12 批复：4=AI 匹配 / 5=验证与 Browse Node / 6=Schema 与属性 /
7=批量任务 / 8=UI）。铁律：
1. 只返回真实 API 候选：一切 pt_name 都来自 SP-API 响应原文，
   绝不本地造词，绝不允许「兜底 PRODUCT」——查不到就空着。
2. 多策略命中合并去重：同一 pt_name 聚成一条，保留全部来源与证据
   （每策略各自的查询词、命中数、S3 的 ASIN/分类证据）。
3. 身份一致性：S3 相似商品必须与产品本体对得上（分类名/商品名与本体
   检索词有重叠）；兼容设备（iPhone/宝马……）绝不能替本体决定分类。
4. 各策略独立容错：无候选 / 无权限 / 限流 / 网络失败分别落报告
   （status=ok|skipped|empty|error + kind 细分），一条策略失败不拖垮
   其余策略。
5. 环境诚实：结果的 env_mode/mock 直接取自客户端可信环境配置
   （spapi_client.Environment），mock 环境检索出的候选绝不冒充官方验证。

三策略（对应用户批复的检索方案）：
  S1 s1_identity_item_name：产品身份 → PTD itemName 搜索（最准）
  S2 s2_keywords：本体关键词 → PTD keywords 搜索
  S3 s3_catalog_similar：Catalog 相似商品参考其 productType
本模块不写 store（候选入库与 verified_against 标记在后续模块按环境
分流——Mock 缓存与 Live 验证状态严格隔离的铁律不允许这里顺手落库）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from pt.material import ProductMaterial, search_plan
from pt.spapi_client import (
    SpApiClient, SpApiAuthError, SpApiBadRequestError, SpApiEnvironmentError,
    SpApiError, SpApiForbiddenError, SpApiNetworkError, SpApiNotFoundError,
    SpApiParseError, SpApiRateLimitError, SpApiServerError,
)

S1 = "s1_identity_item_name"
S2 = "s2_keywords"
S3 = "s3_catalog_similar"
STRATEGIES = (S1, S2, S3)

# 错误分类表：SpApi 异常 → 报告 kind（策略报告细分失败原因）
_ERROR_KINDS: list[tuple[type, str]] = [
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
]


@dataclass
class StrategyReport:
    """单策略执行报告（证据链的一部分，随候选一起给上层）。"""
    strategy: str
    status: str                  # ok | skipped | empty | error
    kind: str = ""               # error 时的细分（auth/forbidden/…）
    detail: str = ""             # 人话说明（含官方 ErrorList 摘要）
    query: dict = field(default_factory=dict)   # 实际发出的查询参数
    hits: int = 0                # API 返回的原始命中数
    used: int = 0                # 真正采信的命中数（S3 会剔除不一致的）
    attempts: int = 0            # 客户端含重试的尝试数
    status_hint: int | None = None   # error 时的 HTTP 状态（辅助排查）


@dataclass
class Candidate:
    """一个 PT 候选（可能多策略命中合并而成）。"""
    pt_name: str
    display_name: str = ""
    sources: dict[str, dict] = field(default_factory=dict)  # 策略→证据
    score: float = 0.0


@dataclass
class CandidatesResult:
    pid: str
    marketplace_id: str
    env_mode: str                # mock | live（可信环境配置原样透传）
    candidates: list[Candidate] = field(default_factory=list)
    reports: dict[str, StrategyReport] = field(default_factory=dict)
    rejected: list[dict] = field(default_factory=list)  # S3 被拒的相似商品

    @property
    def mock(self) -> bool:
        return self.env_mode == "mock"


# 官方 searchCatalogItems includedData（本阶段用三块）
_S3_INCLUDED = ("summaries", "productTypes", "classifications")
_CJK_RE = re.compile(r"[一-鿿]")


def _token_set(text: str) -> set[str]:
    """拉丁词 + 中文 2 字滑窗（与 material._tokenize 同法，出小写集合）。"""
    out = {w.lower() for w in
           re.findall(r"[A-Za-z0-9][\w\-\+\.]*", str(text or "")) if len(w) >= 2}
    cjk = "".join(_CJK_RE.findall(str(text or "")))
    for i in range(len(cjk) - 1):
        out.add(cjk[i:i + 2])
    return out


def _item_text(item: dict) -> str:
    """相似商品的「自我描述」文本：summaries 商品名优先，缺则分类名
    （官方 Item 两块都可选，mock 服务就不带 summaries）。"""
    parts: list[str] = []
    for s in (item.get("summaries") or []):
        parts.append(str(s.get("itemName") or ""))
    for grp in (item.get("classifications") or []):
        for c in (grp.get("classifications") or []):
            parts.append(str(c.get("displayName") or ""))
    return " ".join(p for p in parts if p)


def _mid_product_types(item: dict, marketplace_id: str) -> list[str]:
    """只采信目标站点上的 productType（跨站点证据不算数）。"""
    return sorted({str(e.get("productType") or "")
                   for e in (item.get("productTypes") or [])
                   if e.get("marketplaceId") == marketplace_id
                   and e.get("productType")})


def _error_kind(e: Exception) -> str:
    for exc_type, kind in _ERROR_KINDS:
        if type(e) is exc_type:
            return kind
    for exc_type, kind in _ERROR_KINDS:      # 子类兜底（表按具体→一般排序）
        if isinstance(e, exc_type):
            return kind
    return "internal"


def _err_detail(e: Exception) -> str:
    if isinstance(e, SpApiError):
        msgs = "; ".join(e.official_messages())[:200]
        base = f"{type(e).__name__}"
        if msgs:
            base += f"：{msgs}"
        if e.request_id:
            base += f"（request_id={e.request_id}）"
        return base
    return f"{type(e).__name__}: {e}"[:200]


def _run_strategy(strategy: str, fn: Callable[[], StrategyReport]
                  ) -> StrategyReport:
    """单策略独立执行：失败只污染自己的报告，绝不外抛。"""
    try:
        return fn()
    except SpApiError as e:
        return StrategyReport(strategy, "error", kind=_error_kind(e),
                              detail=_err_detail(e),
                              status_hint=e.status or None)
    except ValueError as e:
        return StrategyReport(strategy, "error", kind="bad_params",
                              detail=f"参数不合法：{e}"[:200])


def retrieve_candidates(client: SpApiClient, material: ProductMaterial, *,
                        marketplace_id: str) -> CandidatesResult:
    """三策略检索 + 合并去重。只读 API，不写库，无副作用。"""
    plan = search_plan(material)
    result = CandidatesResult(pid=material.pid, marketplace_id=marketplace_id,
                              env_mode=client.env_mode)
    by_pt: dict[str, Candidate] = {}

    def offer(pt_name: str, display_name: str, strategy: str,
              evidence: dict) -> None:
        c = by_pt.get(pt_name)
        if c is None:
            c = Candidate(pt_name=pt_name, display_name=display_name or pt_name)
            by_pt[pt_name] = c
        elif display_name and not c.display_name:
            c.display_name = display_name
        c.sources[strategy] = evidence

    # ---------- S1：产品身份 → itemName ----------
    def s1() -> StrategyReport:
        item_name = plan["s1_item_name"]
        if not item_name:
            return StrategyReport(
                S1, "skipped", detail="无准确产品身份（理解结果缺身份且无"
                "本体型号）——不拿 SKU/原始标题硬当 itemName")
        kw: dict = {"item_name": item_name}
        if plan["search_locale"]:
            kw["search_locale"] = plan["search_locale"]
        r = client.search_product_types(marketplace_id, **kw)
        pts = r.data.get("productTypes") or []
        for p in pts:
            name = str(p.get("name") or "")
            if name:
                offer(name, str(p.get("displayName") or ""), S1,
                      {"query": dict(kw), "marketplace_id": marketplace_id})
        status = "ok" if pts else "empty"
        return StrategyReport(S1, status, query=dict(kw), hits=len(pts),
                              used=len(pts), attempts=r.attempts,
                              detail="" if pts else "官方返回空候选（未命中）")

    # ---------- S2：本体关键词 → keywords ----------
    def s2() -> StrategyReport:
        keywords = plan["s2_keywords"]
        if not keywords:
            return StrategyReport(
                S2, "skipped", detail="本体检索词为空（既无身份也无型号、"
                "无 basic_info 线索）——绝不拿兼容词凑数")
        kw: dict = {"keywords": list(keywords)}
        if plan["search_locale"]:
            kw["search_locale"] = plan["search_locale"]
        r = client.search_product_types(marketplace_id, **kw)
        pts = r.data.get("productTypes") or []
        for p in pts:
            name = str(p.get("name") or "")
            if name:
                offer(name, str(p.get("displayName") or ""), S2,
                      {"query": dict(kw), "marketplace_id": marketplace_id,
                       "keywords": list(keywords)})
        status = "ok" if pts else "empty"
        return StrategyReport(S2, status, query=dict(kw), hits=len(pts),
                              used=len(pts), attempts=r.attempts,
                              detail="" if pts else "官方返回空候选（未命中）")

    # ---------- S3：Catalog 相似商品 → 参考其 PT ----------
    def s3() -> StrategyReport:
        keywords = plan["s3_keywords"]
        if not keywords:
            return StrategyReport(S3, "skipped", detail="无任何可用检索词")
        kw: dict = {"keywords": list(keywords),
                    "included_data": list(_S3_INCLUDED)}
        if plan["search_locale"]:
            kw["keywords_locale"] = plan["search_locale"]
        r = client.search_catalog_items(marketplace_id, **kw)
        items = r.data.get("items") or []
        body = {t.lower() for t in material.body_terms()}
        if not body:
            return StrategyReport(
                S3, "skipped", query=dict(kw), hits=len(items),
                detail="本体词为空，无法核验相似商品身份一致性——全部不采信"
                "（宁缺毋滥）")
        used = 0
        ev_by_pt: dict[str, dict] = {}
        for item in items:
            asin = str(item.get("asin") or "")
            text_tokens = _token_set(_item_text(item))
            overlap = body & text_tokens
            pts = _mid_product_types(item, marketplace_id)
            if not overlap or not pts:
                result.rejected.append({
                    "asin": asin,
                    "reason": ("body_terms_no_overlap" if not overlap
                               else "no_product_type_for_marketplace"),
                    "item_text": _item_text(item)[:120],
                    "overlap": sorted(overlap)[:8],
                })
                continue
            used += 1
            cls_ids = sorted({c.get("classificationId")
                              for grp in (item.get("classifications") or [])
                              for c in (grp.get("classifications") or [])
                              if c.get("classificationId")})
            for pt in pts:
                ev = ev_by_pt.setdefault(pt, {"query": dict(kw), "asins": [],
                                              "classification_ids": cls_ids,
                                              "overlap_examples":
                                                  sorted(overlap)[:4]})
                if asin and asin not in ev["asins"]:
                    ev["asins"].append(asin)
        for pt, ev in ev_by_pt.items():
            offer(pt, "", S3, ev)
        status = "ok" if used else "empty"
        detail = ""
        if not used:
            detail = (f"{len(items)} 个相似商品全部被拒：与产品本体检索词"
                      f"无重叠或无本站点 PT——兼容设备冒充本体的路堵死")
        return StrategyReport(S3, status, query=dict(kw), hits=len(items),
                              used=used, attempts=r.attempts, detail=detail)

    result.reports[S1] = _run_strategy(S1, s1)
    result.reports[S2] = _run_strategy(S2, s2)
    result.reports[S3] = _run_strategy(S3, s3)

    # ---------- 合并去重 + 评分（排序提示，不是最终选择） ----------
    for c in by_pt.values():
        score = 0.0
        if S1 in c.sources:
            score += 3.0
        if S2 in c.sources:
            score += 2.0
        if S3 in c.sources:
            score += 1.0 + 0.1 * len(c.sources[S3].get("asins") or [])
        c.score = round(score, 2)
    result.candidates = sorted(by_pt.values(),
                               key=lambda c: (-c.score, c.pt_name))
    return result

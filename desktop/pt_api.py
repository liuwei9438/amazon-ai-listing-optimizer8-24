# -*- coding: utf-8 -*-
"""pt_api —— 模块 8：pt 引擎与桌面主程序之间的独立适配层。

职责（模块 8 批复三）：
- 把 /api/pt/* 的 HTTP 语义接到模块 1–7（PtStore / TaskEngine / GPT
  决策器），**main.py 只在路由链头部各加一行转发**，其余一概不改；
- 本层与 pt 包、main.py 双向解耦：不 import main（工程约束），全部
  外部能力（登录态/商品读取/店铺清单/AI 配置/令牌）经构造期注入。

所有 /api/pt/* 端点统一强制（批复三·安全条款）：
1. 用户身份认证：未登录 401（注入的 user() 为空）；
2. 商品所有权：pids 逐一经注入的 prod_list（带会话 token，由服务器
   按账号强制归属）读取——读不到=不归此账号/不存在，拒绝且不静默跳过；
3. 店铺与 Marketplace 权限：客户端只能提交店铺 id；店铺记录、站点
   （marketplace_id）、卖家 id 一律从服务器店铺清单解析；提交的站点
   与店铺记录不符=403；**绝不采信客户端提交的 seller_id/marketplace**
   （防绕过）；
4. Mock-Live 环境检查：mock 任务仅在配置了 mock 服务时允许；live 任务
   需具备 live 环境能力；任务 env 与客户端 env 不符由 TaskEngine 运行
   时硬闸二次拦截；
5. 参数校验：pids 非空去重上限、store_id/env/pt_name/node_id 词表校验；
6. 幂等与并发保护：建任务带 user+店铺+站点+env+pids 指纹的 idem_key
   （重复点击返回同一任务）；同店×站×env 活跃重叠守卫由 store 抛
   TaskConflictError → 409；控制操作走 CAS 相位（重复暂停/取消=幂等
   返回当前态）；
7. 操作日志：每次写操作追加 JSONL（谁/何时/干什么/结果），不改任何
   既有表。

数据与状态安全（批复七）：cat/btmap/ptmap 与上传管道零接触；新分类
结果只进 pt 库；不自动覆盖、不自动改上传 PT、不调刊登接口；mock 结果
显式标记 env="mock"（UI 必须显示「模拟数据」）；未验证绝不显示「官方
验证通过」；人工确认走 chosen_by="human" 分配链（历史全留、可追溯）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from pt.ai_gpt import GptDecision
from pt.attribute import MAX_ATTRIBUTES_PER_CALL, normalize_row
from pt.facts import extract_facts
from pt.fill import fill_attributes
from pt.listing import (
    DEFAULT_LEASE_TTL, DEFAULT_POLL_BUDGET, DEFAULT_POLL_INTERVAL,
    ListingEngine, ListingStateError, ListingSubmitError, attr_revision,
    content_fingerprint, expand_skus, item_snapshot,
)
from pt.normalize import normalize_product_record
from pt.schema import ST_OK, analyze_schema, schemas_dir, _cache_filename
from pt.spapi_client import RetryConfig, SpApiClient, build_environment
from pt.authz import AuthzError
from pt.store import PtStore, StaleTaskError, TaskConflictError
from pt.tasks import TaskEngine
from pt.validate import RESOLUTION_ACTIONS, report_to_dict, validate_product

_MAX_PIDS = 500            # 单任务商品数上限（参数校验）
_PRODUCT_CACHE_MAX = 5000  # load_product 缓存上限（item_results 补读用）


def _q1(d: dict, key: str, default: str = "") -> str:
    """查询/正文参数归一：GET 查询串是 {k:[v]}，POST 正文是标量——
    统一取单值（与既有 status_map/product_detail 的取参惯例一致）。"""
    v = d.get(key)
    if isinstance(v, list):
        return str(v[0]) if v else default
    return default if v is None else str(v)

# 任务条目终态子集（UI 状态徽章用）
_ITEM_BADGE = {
    "completed": "✅ 已完成",
    "needs_review": "👁 待人工复核",
    "retryable_failed": "🔁 可重试失败",
    "permanent_failed": "❌ 彻底失败",
    "cancelled": "⛔ 已取消",
    "pending": "⚪ 排队中",
    "running": "🔄 处理中",
}


class PtApiError(Exception):
    """适配层错误（携带 HTTP 状态码）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PtApi:
    """/api/pt/* 适配器（一个进程一个实例；状态真源=SQLite）。

    deps（全部构造期注入，便于独立测试）：
      user()              -> str            当前登录账号（""=未登录）
      prod_list(pid)      -> dict|None      服务器商品读取（token 鉴权、
                                             归属由服务器强制）；异常上抛
      stores()            -> (list, err)    服务器店铺清单（token 鉴权）
      active_store_id()   -> str            本机记住的默认店铺 id
      resolve_ai()        -> (key, provider, model)
      ai_base()           -> str            AI 根地址覆盖（mock 测试用）
      mock_url()          -> str            SP-API mock 地址（""=无 mock）
      live_env()          -> Environment|None  live 传输环境（None=不具备）
      store_token(sid)    -> str            店铺 SP-API 短令牌（live 用）
    """

    def __init__(self, *, app_dir, deps: dict):
        self._deps = deps
        self._dir = Path(app_dir) / "pt_data"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store = PtStore(self._dir / "pt.sqlite3")
        self._cache = self._dir / "schema_cache"
        self._ops_path = self._dir / "ops.jsonl"
        self._ai_log = self._dir / "ai_decisions.jsonl"
        self._lock = threading.Lock()
        self._runners: dict[str, threading.Thread] = {}
        self._products: dict[str, tuple] = {}     # pid -> (record, und, ts)
        self._mock_client: SpApiClient | None = None
        # S3-C 二阶段：多店铺授权管理（内存态；审计只收分类不收令牌）
        from pt.authz import StoreAuthzManager
        self._authz = StoreAuthzManager(
            stores=deps["stores"], store_token=deps["store_token"],
            audit=lambda cat, **kw: self._op(
                self._user_cached() or "-", cat, **kw))
        # 装配即恢复：进程重启后把残留任务接回来（真源=SQLite）
        self.startup_recover()

    # ==================================================================
    # HTTP 入口（main.py 各转发一行）
    # ==================================================================

    def handle(self, method: str, path: str, q: dict, body: dict
               ) -> tuple[dict, int]:
        try:
            seg = [s for s in path.split("/") if s]   # api/pt/...
            if len(seg) < 3 or seg[:2] != ["api", "pt"]:
                return {"ok": False, "error": "not found"}, 404
            rest = seg[2:]
            if method == "GET":
                return self._route_get(rest, q or {}), 200
            if method == "POST":
                return self._route_post(rest, body or {}), 200
            return {"ok": False, "error": "方法不支持"}, 405
        except PtApiError as e:
            return {"ok": False, "error": str(e)}, e.status
        except TaskConflictError as e:
            return {"ok": False, "error": f"已有进行中的任务覆盖这些商品"
                                          f"：{e}"}, 409
        except StaleTaskError as e:
            # S3-B2：终态任务+内容已变化（或历史行无指纹）——不得静默
            # 复用旧任务；旧任务与快照原样保留，新一轮回填 new_operation
            return {"ok": False, "error": str(e), "code": "stale_task"}, 409
        except Exception as e:  # noqa: BLE001 —— 绝不带崩主服务
            return {"ok": False,
                    "error": f"pt 服务内部错误：{type(e).__name__}: "
                             f"{e}"[:300]}, 500

    def _route_get(self, rest: list, q: dict) -> dict:
        if rest == ["overview"]:
            return self.overview()
        if rest == ["stores", "auth"]:
            return self.stores_auth_view(q)
        if rest == ["smoke", "readiness"]:
            return self.smoke_readiness_view(q)
        if rest == ["status"]:
            return self.status_map(q)
        if len(rest) == 2 and rest[0] == "products":
            return self.product_detail(rest[1], q)
        if len(rest) == 2 and rest[0] == "workbench":
            return self.workbench(rest[1], q)
        if len(rest) == 2 and rest[0] == "candidates":
            return self.candidates(rest[1], q)
        if rest == ["tasks"]:
            return self.tasks_list(q)
        if len(rest) == 2 and rest[0] == "tasks":
            return self.task_detail(rest[1])
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "items":
            return self.task_items(rest[1])
        if rest == ["listings", "tasks"]:
            return self.listing_tasks_list(q)
        if len(rest) == 3 and rest[0] == "listings" and rest[1] == "tasks":
            return self.listing_task_detail(rest[2])
        if len(rest) == 4 and rest[0] == "listings" and rest[1] == "tasks" \
                and rest[3] == "items":
            return self.listing_task_items_view(rest[2])
        if len(rest) == 2 and rest[0] == "attributes":
            return self.attr_list(rest[1], q)
        if len(rest) == 3 and rest[0] == "attributes" \
                and rest[2] == "history":
            return self.attr_history(rest[1], q)
        if len(rest) == 2 and rest[0] == "validation":
            return self.validation_run(rest[1], q)
        if rest == ["review", "exceptions"]:
            return self.review_queue_view(q)
        raise PtApiError("not found", 404)

    def _route_post(self, rest: list, body: dict) -> dict:
        if rest == ["tasks"]:
            return self.task_create(body)
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] in (
                "pause", "resume", "cancel", "retry"):
            return self.task_control(rest[1], rest[2], body)
        if rest == ["listings", "tasks"]:
            return self.listing_task_create(body)
        if len(rest) == 4 and rest[0] == "listings" and rest[1] == "tasks" \
                and rest[3] in ("pause", "resume", "cancel", "retry"):
            return self.listing_task_control(rest[2], rest[3], body)
        if len(rest) == 3 and rest[0] == "assignments" \
                and rest[2] == "confirm":
            return self.assignment_confirm(rest[1], body)
        if len(rest) == 2 and rest[0] == "attributes":
            return self.attr_propose(rest[1], body)
        if len(rest) == 3 and rest[0] == "attributes" \
                and rest[2] == "review":
            return self.attr_review(rest[1], body)
        if len(rest) == 3 and rest[0] == "attributes" \
                and rest[2] == "edit":
            return self.attr_edit(rest[1], body)
        if len(rest) == 3 and rest[0] == "attributes" \
                and rest[2] == "fill":
            return self.attr_fill(rest[1], body)
        if len(rest) == 4 and rest[0] == "review" \
                and rest[1] == "exceptions" and rest[3] == "resolve":
            return self.review_resolve(rest[2], body)
        raise PtApiError("not found", 404)

    # ==================================================================
    # 安全基元（批复三·1–5）
    # ==================================================================

    def _user(self) -> str:
        user = str(self._deps["user"]() or "")
        if not user:
            raise PtApiError("未登录", 401)
        return user

    def _user_cached(self) -> str:
        """审计用取用户（不抛 401——审计失败不能拖垮业务路径）。"""
        try:
            return str(self._deps["user"]() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _resolve_store(self, store_id: str) -> dict:
        """店铺记录只认服务器清单（客户端 id 仅用于在清单里挑选）。"""
        stores, err = self._deps["stores"]()
        if err:
            raise PtApiError(f"读店铺清单失败：{err}", 502)
        sid = str(store_id or "").strip()
        if not sid:
            sid = str(self._deps["active_store_id"]() or "").strip()
            if not sid:
                raise PtApiError("还没选择店铺（先在「上传亚马逊」里绑定/"
                                 "选择一家店）", 400)
        store = next((s for s in stores if str(s.get("id")) == sid), None)
        if store is None:
            raise PtApiError(f"店铺不存在或无权使用：{sid}", 403)
        mid = str(store.get("marketplace") or "").strip()
        if not mid:
            raise PtApiError("这家店没有站点信息（重新绑定一次）", 400)
        return {"id": str(store["id"]), "name": str(store.get("name") or ""),
                "marketplace_id": mid,
                "seller_id": str(store.get("seller_id") or "")}

    def _check_env(self, env: str) -> str:
        env = str(env or "live").strip().lower()
        if env not in ("mock", "live"):
            raise PtApiError("env 只能是 mock 或 live", 400)
        if env == "mock" and not str(self._deps["mock_url"]() or ""):
            raise PtApiError("本机没配置 SP-API mock 服务，不能建模拟任务",
                             403)
        if env == "live" and self._deps["live_env"]() is None:
            raise PtApiError("live 环境未配置（需官方传输环境与店铺令牌）",
                             403)
        return env

    def _validate_pids(self, pids) -> list:
        if not isinstance(pids, list) or not pids:
            raise PtApiError("缺 pids（要分类的商品列表）", 400)
        out, seen = [], set()
        for p in pids:
            pid = str(p or "").strip()
            if not pid:
                continue
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
        if not out:
            raise PtApiError("pids 全为空", 400)
        if len(out) > _MAX_PIDS:
            raise PtApiError(f"单次最多 {_MAX_PIDS} 个商品", 400)
        return out

    def _my_store_ids(self) -> set:
        """当前账号可用的店铺 id 集（权限边界=服务器店铺清单，token
        鉴权，客户端不可伪造）。"""
        stores, err = self._deps["stores"]()
        if err:
            raise PtApiError(f"读店铺清单失败：{err}", 502)
        return {str(s.get("id")) for s in stores}

    def _auth_task(self, task_id: str) -> dict:
        """任务访问/控制统一闸：不存在与无权限**同一响应**（404「任务
        不存在」）——不给猜 ID 探测任务存在性的口子。"""
        task = self._store.get_task(str(task_id))
        if task is None or task["store_id"] not in self._my_store_ids():
            raise PtApiError("任务不存在", 404)
        return task

    def _own_index_items(self) -> list[dict]:
        """本账号商品索引（服务器一次读取；条目含 pid/title/sku/cat）。
        读取失败=宁可拒绝不泄露（越权风险 fail-closed）。"""
        try:
            data = self._deps["prod_list"](None)
        except Exception:  # noqa: BLE001 —— 索引读不到=宁可拒绝不泄露
            raise PtApiError("读商品索引时服务器不可达——为避免越权泄露，"
                             "本次查询取消（稍后重试）", 502)
        items = (data or {}).get("items")
        if not (data or {}).get("ok") or not isinstance(items, list):
            raise PtApiError("商品索引读取失败（稍后重试）", 502)
        return [x for x in items
                if isinstance(x, dict) and x.get("pid") is not None]

    def _own_pid_set(self) -> set:
        """本账号商品 pid 全集（服务器索引一次读取）。status_map 按
        它过滤：即便同店内混入他人商品，其分类状态/候选元数据也一个
        都不外泄（服务器端过滤，前端隐藏不算数）。"""
        return {str(x.get("pid")) for x in self._own_index_items()}

    def _own_products(self, pids: list) -> dict:
        """所有权：逐一经服务器 token 读取（服务器强制归属）。"""
        missing = []
        for pid in pids:
            try:
                data = self._deps["prod_list"](pid)
            except Exception:  # noqa: BLE001 —— 服务器不可达≠不归他
                raise PtApiError(
                    f"读商品 {pid} 时服务器不可达——为避免误判归属，"
                    "本次操作取消（稍后重试）", 502)
            if not (data or {}).get("ok") or not (data or {}).get("product"):
                missing.append(pid)
        if missing:
            raise PtApiError(
                f"这些商品不存在或不归当前账号：{','.join(missing[:10])}"
                + ("…" if len(missing) > 10 else ""), 403)
        return {p: self._product_record(p) for p in pids}

    # S3-B2-F：操作日志尺寸轮转（原为无限追加——S3-A P2 遗留）。
    # 超过 _OPS_ROTATE_BYTES 时世代滚动：ops.jsonl → .1、.1 → .2（丢弃
    # 最旧代），保留最近 _OPS_KEEP 代；滚动失败不阻塞审计写（OSError
    # 吞掉与原语义一致）。阈值/代数可被测试替换（模块级常量）。
    _OPS_ROTATE_BYTES = 5 * 1024 * 1024
    _OPS_KEEP = 2

    def _op_rotate(self) -> None:
        if not self._ops_path.exists() \
                or self._ops_path.stat().st_size < self._OPS_ROTATE_BYTES:
            return
        for i in range(self._OPS_KEEP - 1, 0, -1):
            cur = self._ops_path.with_name(self._ops_path.name + f".{i}")
            nxt = self._ops_path.with_name(self._ops_path.name + f".{i + 1}")
            if cur.exists():
                nxt.unlink(missing_ok=True)
                cur.replace(nxt)
        gen1 = self._ops_path.with_name(self._ops_path.name + ".1")
        gen1.unlink(missing_ok=True)
        self._ops_path.replace(gen1)

    def _op(self, user: str, action: str, **kw) -> None:
        """操作日志（JSONL 追加；不改既有表；超限世代滚动）。"""
        try:
            self._op_rotate()
            rec = {"ts": round(time.time(), 3), "user": user,
                   "action": action, **kw}
            with open(self._ops_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ==================================================================
    # 路由实现
    # ==================================================================

    def stores_auth_view(self, q: dict) -> dict:
        """S3-C 二阶段：店铺授权状态（只读）。

        默认 dry（零网络：只看内存态）；probe=1 才对每店做一次授权
        probe（打 worker 换令牌——是内部服务器调用，不是 SP-API）。
        输出绝不含令牌值（AuthRecord.view 已脱敏）。
        """
        user = self._user()
        probe = str((q.get("probe") or ["0"])[0]
                    if isinstance(q.get("probe"), list)
                    else (q.get("probe") or "0")) in ("1", "true")
        sid = str((q.get("store_id") or [""])[0]
                  if isinstance(q.get("store_id"), list)
                  else (q.get("store_id") or "")).strip()
        if sid:
            try:
                return {"ok": True,
                        "stores": [self._authz.status(sid, probe=probe)]}
            except AuthzError as e:
                raise PtApiError(str(e), 403)
        try:
            rows = self._authz.status_all(probe=probe)
        except AuthzError as e:
            raise PtApiError(str(e), 502)
        self._op(user, "store_auth_view", probe=probe, n=len(rows))
        return {"ok": True, "stores": rows}

    def smoke_readiness_view(self, q: dict) -> dict:
        """S3-C 三阶段：只读 smoke 就绪计划（**永远 dry**）。

        本路由不构造 SmokeAuthorization（fail-closed：真实执行=未来
        获批后独立提交接线）；probe=1 时才打 worker 验证店铺授权。
        """
        user = self._user()
        sid = str((q.get("store_id") or [""])[0]
                  if isinstance(q.get("store_id"), list)
                  else (q.get("store_id") or "")).strip()
        if not sid:
            sid = str(self._deps["active_store_id"]() or "").strip()
        self._resolve_store(sid)     # 同一 403 防探测闸（他人店=不存在）
        probe = str((q.get("probe") or ["0"])[0]
                    if isinstance(q.get("probe"), list)
                    else (q.get("probe") or "0")) in ("1", "true")

        def one(k: str) -> str:
            v = q.get(k)
            return str((v[0] if isinstance(v, list) else v) or "").strip()

        params = {k: one(k) for k in
                  ("search_keywords", "product_type", "schema_link",
                   "sku", "asin") if one(k)}
        from pt.smoke import SmokeRunner
        runner = SmokeRunner(
            authz=self._authz,
            live_env=self._deps["live_env"],
            client=lambda s: self._client_for("live", s),
            params=lambda: params)
        try:
            plan = runner.plan(sid, probe=probe)
        except AuthzError as e:
            raise PtApiError(str(e), 403)
        self._op(user, "smoke_readiness", store_id=sid,
                 probe=probe, status=plan["status"])
        return plan

    def overview(self) -> dict:
        self._user()
        stores, err = self._deps["stores"]()
        if err:
            raise PtApiError(f"读店铺清单失败：{err}", 502)
        key, provider, model = self._deps["resolve_ai"]()
        mock_url = str(self._deps["mock_url"]() or "")
        live_ok = self._deps["live_env"]() is not None
        my = self._my_store_ids()          # 只统计自己店铺的任务相位
        counts: dict[str, int] = {}
        for t in self._store.tasks():
            if t["store_id"] in my:
                counts[t["phase"]] = counts.get(t["phase"], 0) + 1
        return {
            "ok": True,
            "stores": [{"id": str(s.get("id") or ""),
                        "name": str(s.get("name") or ""),
                        "marketplace": str(s.get("marketplace") or "")}
                       for s in stores],
            "active_store": str(self._deps["active_store_id"]() or ""),
            "ai": {"ready": bool(key and model),
                   "provider": provider or "", "model": model or ""},
            "env": {"mock": bool(mock_url), "live": live_ok},
            "task_phase_counts": counts,
        }

    def status_map(self, q: dict) -> dict:
        """商品列表状态图（页面 A 徽章数据源）：当前分配 + 最近条目状态
        合并 {pid: {state, pt, chosen_by, confidence}}。"""
        self._user()
        env = self._check_env(
            (q.get("env") or ["live"])[0] if isinstance(q.get("env"), list)
            else (q.get("env") or "live"))
        store = self._resolve_store(
            (q.get("store") or [""])[0] if isinstance(q.get("store"), list)
            else str(q.get("store") or ""))
        sid, mid = store["id"], store["marketplace_id"]
        # 权限轴：本账号商品集之外的 pid，其分配/条目元数据一个都不出
        # （服务器端过滤，不是前端隐藏）
        own = self._own_pid_set()
        asg = self._store.assignments_all(sid, mid, env=env)
        items = self._store.latest_item_states(sid, mid, env=env)
        items = self._store.latest_item_states(sid, mid, env=env)
        out: dict = {}
        for pid, a in asg.items():
            if pid not in own:
                continue
            out[pid] = {"pt": a.get("pt_name"),
                        "chosen_by": a.get("chosen_by"),
                        "confidence": a.get("confidence"),
                        "state": "classified"}
        for pid, it in items.items():
            if pid not in own:
                continue
            row = out.setdefault(pid, {})
            row["task_state"] = it["state"]
            row["error_kind"] = it.get("error_kind") or None
            if it["state"] in ("needs_review", "retryable_failed",
                               "permanent_failed"):
                row.setdefault("state", it["state"])
        return {"ok": True, "env": env, "store_id": sid, "status": out,
                "env_note": "模拟数据" if env == "mock" else ""}

    def _safe_review_evidence(self, sid: str, mid: str, pid: str,
                              env: str):
        """复核证据安全读取（任务二.6）：任何故障降级 None，详情不崩。"""
        try:
            return self._store.latest_review_evidence(sid, mid, pid, env=env)
        except Exception:  # noqa: BLE001 —— 证据链绝不拖垮主流程
            return None

    def product_detail(self, pid: str, q: dict) -> dict:
        user = self._user()
        pid = str(pid or "").strip()
        if not pid:
            raise PtApiError("缺 pid", 400)
        env = self._check_env(q.get("env", ["live"])[0]
                              if isinstance(q.get("env"), list)
                              else (q.get("env") or "live"))
        store = self._resolve_store(
            (q.get("store") or [""])[0] if isinstance(q.get("store"), list)
            else str(q.get("store") or ""))
        sid, mid = store["id"], store["marketplace_id"]
        # 所有权（同批次原则：能看详情=必须拥有）
        try:
            data = self._deps["prod_list"](pid)
        except Exception:  # noqa: BLE001
            raise PtApiError("读商品时服务器不可达", 502)
        if not (data or {}).get("ok") or not (data or {}).get("product"):
            raise PtApiError("商品不存在或不归当前账号", 403)
        prod = (data or {}).get("product") or {}
        asg = self._store.current_assignment(sid, mid, pid, env=env)
        hist = self._store.assignment_history(sid, mid, pid, env=env)[:8]
        cands = self._store.candidates(sid, mid, pid, env=env)
        vs = self._store.verifications(sid, mid, pid)
        kinds = {}
        for v in vs:
            kinds.setdefault(v["check_kind"], []).append(v)
        item = self._latest_item(sid, mid, pid, env)
        return {
            "ok": True, "pid": pid, "user": user,
            "store_id": sid, "marketplace_id": mid, "env": env,
            "env_note": "模拟数据（Mock 演示，不是官方结论）"
                        if env == "mock" else "",
            "product": {"title": str(prod.get("title") or ""),
                        "sku": self._sku_of(prod, pid)},
            "assignment": asg, "assignment_history": hist,
            "candidates": _group_candidates(cands),
            "verifications": {
                k: (v[-1] if k != "browse_node" else v)
                for k, v in kinds.items()},
            "latest_task_item": item,
            # P5 集成验收（任务二.6）：复核证据读取失败绝不影响商品详情
            # 主流程——降级为 None（UI 照常渲染其余字段）。
            "review_evidence": self._safe_review_evidence(sid, mid, pid, env),
            "review_note": "review_evidence=AI 建议快照（供人工复核参考），"
                           "绝不等于已确认分类；确认只经人工 assignment_confirm",
            "badge": _ITEM_BADGE.get((item or {}).get("state", ""), ""),
            "human_note": "人工确认只代表人的判断，不等于「官方验证通过」",
        }

    def candidates(self, pid: str, q: dict) -> dict:
        self._user()
        pid = str(pid or "").strip()
        env = self._check_env(
            (q.get("env") or ["live"])[0] if isinstance(q.get("env"), list)
            else (q.get("env") or "live"))
        store = self._resolve_store(
            (q.get("store") or [""])[0] if isinstance(q.get("store"), list)
            else str(q.get("store") or ""))
        # 所有权：候选元数据（官方证据/评分）与商品详情同一道闸
        try:
            data = self._deps["prod_list"](pid)
        except Exception:  # noqa: BLE001
            raise PtApiError("读商品时服务器不可达", 502)
        if not (data or {}).get("ok") or not (data or {}).get("product"):
            raise PtApiError("商品不存在或不归当前账号", 403)
        rows = self._store.candidates(store["id"], store["marketplace_id"],
                                      pid, env=env)
        return {"ok": True, "pid": pid, "env": env,
                "env_note": "模拟数据" if env == "mock" else "",
                "candidates": rows}

    def tasks_list(self, q: dict) -> dict:
        self._user()
        limit = 30
        try:
            limit = max(1, min(int((q.get("limit") or ["30"])[0]
                                   if isinstance(q.get("limit"), list)
                                   else q.get("limit") or 30), 100))
        except (TypeError, ValueError):
            pass
        eng = self._light_engine()
        my = self._my_store_ids()          # 任务列表只出自己店铺的任务
        rows = []
        n = 0
        for t in self._store.tasks():
            if t["store_id"] not in my:
                continue
            n += 1
            if n > limit:
                break
            pr = eng.task_progress(t["id"])
            rows.append({**t, **({
                "total": pr.total, "progress_percent": pr.progress_percent,
                "completed": pr.completed, "needs_review": pr.needs_review,
                "retryable_failed": pr.retryable_failed,
                "permanent_failed": pr.permanent_failed,
            } if pr else {})})
        return {"ok": True, "tasks": rows}

    def task_detail(self, task_id: str) -> dict:
        self._user()
        task = self._auth_task(str(task_id))   # 他人任务=同一句 404
        pr = self._light_engine().task_progress(task["id"])
        perf = None
        try:
            perf = json.loads(task.get("perf_json") or "null")
        except Exception:  # noqa: BLE001 —— 审计字段坏了不拖垮详情
            perf = None
        return {"ok": True, "task": task, "progress": _progress_dict(pr),
                "perf": perf,
                "env_note": "模拟数据（Mock 演示）"
                            if task["env"] == "mock" else ""}

    def task_items(self, task_id: str) -> dict:
        self._user()
        task = self._auth_task(str(task_id))   # 条目含 PID/SKU：同闸
        eng = self._light_engine()
        items = []
        for x in eng.item_results(task["id"]):
            items.append({
                "pid": x.pid, "sku": x.sku, "state": x.state,
                "badge": _ITEM_BADGE.get(x.state, x.state),
                "stage": x.stage, "attempt": x.attempt,
                "error_kind": x.error_kind, "error": x.error,
                "pt": x.ai_chosen_pt, "confidence": x.confidence,
                "ai_pt": x.ai_pt, "ai_confidence": x.ai_confidence,
                "schema_ok": bool(x.schema_ok), "attr_status": x.attr_status,
                "pt_status": (x.pt_verification or {}).get("status"),
                "bn_verified": bool(x.bn_verifications) and any(
                    v.get("status") == "passed" for v in x.bn_verifications),
                "attr_summary": ((x.attribute_verification or {})
                                 .get("detail") or {}).get("summary"),
                "schema_status": x.schema_status,
                "candidates_n": len(x.official_candidates),
            })
        return {"ok": True, "task_id": task["id"], "phase": task["phase"],
                "env": task["env"],
                "env_note": "模拟数据（Mock 演示）"
                            if task["env"] == "mock" else "",
                "items": items}

    def task_create(self, body: dict) -> dict:
        user = self._user()
        pids = self._validate_pids(body.get("pids"))
        env = self._check_env(body.get("env"))
        store = self._resolve_store(str(body.get("store_id") or ""))
        # 客户端提交的 marketplace 一律只做「是否与店铺一致」的校验，
        # 绝不采信其值（防绕过）
        mk = str(body.get("marketplace_id") or "").strip()
        if mk and mk != store["marketplace_id"]:
            raise PtApiError(
                f"站点与店铺不符：店铺 {store['name']} 是 "
                f"{store['marketplace_id']}，请求却是 {mk}", 403)
        self._own_products(pids)          # 所有权（403 拒绝，绝不静默跳过）
        sid, mid = store["id"], store["marketplace_id"]
        new_op = body.get("new_operation")
        if new_op is not None and not isinstance(new_op, bool):
            raise PtApiError("new_operation 只能是布尔", 400)
        idem = "ui:" + hashlib.sha1(
            f"{user}|{sid}|{mid}|{env}|{','.join(sorted(pids))}"
            .encode("utf-8")).hexdigest()[:20]
        eng = self._engine_for(env, sid, pids)
        task = eng.create_task(
            kind="single" if len(pids) == 1 else "multi",
            store_id=sid, marketplace_id=mid, pids=pids, env=env,
            source=f"ui:{user}", idem_key=idem,
            new_operation=bool(new_op))
        self._op(user, "task_create", task_id=task["id"], env=env,
                 store_id=sid, marketplace_id=mid, pids=len(pids),
                 created=bool(task.get("_created")),
                 new_operation=bool(new_op),
                 replay=not bool(task.get("_created")))
        if task.get("_created"):
            self._spawn_run(env, sid, task["id"])
        return {"ok": True, "task": task,
                "env_note": "模拟数据（Mock 演示）" if env == "mock" else ""}

    def task_control(self, task_id: str, op: str, body: dict) -> dict:
        user = self._user()
        task = self._auth_task(str(task_id))   # 控制面同闸：越权=不存在
        env, sid = task["env"], task["store_id"]
        eng = self._light_engine()
        tid = task["id"]
        pending = False     # resume 撞上 pausing 窗口：意图已受理、待落定
        if op == "pause":
            ok = eng.request_pause(tid)
        elif op == "cancel":
            ok = eng.request_cancel(tid)
        elif op == "resume":
            ok = eng.resume(tid)
            if ok:
                self._spawn_run(env, sid, tid)
            elif task["phase"] == "pausing":
                # 暂停尚未落定：CAS 抢不到相位是预期竞争而非失败。
                # 把「继续」意图持久化进任务行，暂停落定时由引擎原子
                # 消费并自动续跑（状态机层收敛，不是前端延时重试）。
                # 后到的 pause/cancel 会先压掉该意图——后到指令说了算。
                self._store.set_resume_pending(tid)
                pending = True
        elif op == "retry":
            # 显式重试（用户操作）：失败条目拉回 pending 后续跑；
            # needs_review 是人工判断位，绝不自动重跑（模块 7 语义）。
            # 终态相位（completed*/failed）不在 RESUMABLE_PHASES 里，
            # 经 store CAS 直接回 running（run() 只认 created/running）
            if task["phase"] not in ("completed", "completed_with_issues",
                                     "failed", "paused", "interrupted"):
                raise PtApiError(
                    f"任务 {task['phase']} 状态下不能重试"
                    "（先暂停或等它结束）", 409)
            n = self._store.retry_failed(tid)
            if not n:
                raise PtApiError(
                    "没有可重试的失败商品（待复核的商品走人工复核，"
                    "不会自动重跑）", 400)
            ok = self._store.cas_task_phase(
                tid, ("completed", "completed_with_issues", "failed",
                      "paused", "interrupted"), "running")
            if ok:
                self._store.set_resume_pending(tid, False)  # 意图随重跑达成
                self._spawn_run(env, sid, tid)
            else:
                raise PtApiError("重试没抢到运行相位（任务状态刚变了，"
                                 "刷新再看）", 409)
        else:                                    # pragma: no cover
            raise PtApiError("未知操作", 400)
        extra: dict = {"phase": self._store.get_task(tid)["phase"]}
        if op == "resume":
            extra["pending"] = bool(pending)
        if op == "retry":
            # 历史完成时间审计：cas_task_phase 回 running 时已清
            # finished_at，旧值只留在操作日志里（可追溯，不误导现态）
            extra["prev_finished_at"] = task.get("finished_at")
        self._op(user, f"task_{op}", task_id=tid, ok=bool(ok), **extra)
        cur = self._store.get_task(tid)
        return {"ok": True, "applied": bool(ok), "pending": bool(pending),
                "task": cur}

    # ==================================================================
    # S2c：统一刊登任务（Mock-only 第一阶段）—— /api/pt/listings/*
    # 安全闸与分类任务同源：_user 401 → _resolve_store（只认服务器店铺
    # 清单）→ 商品归属 403（同响应防探测）→ _auth_listing_task（他人
    # 任务=404 同句）。铁律：本阶段只收 env=mock（live 一律 403「真实
    # 通道未接入」，绝不伪装可提交）；Mock 提交结果处处标注「模拟」。
    # ==================================================================

    _LISTING_ITEM_BADGE = {
        "completed": "✅ 已完成（模拟）",
        "blocked": "🚧 前置条件阻断",
        "retryable_failed": "🔁 可重试失败",
        "permanent_failed": "❌ 彻底失败",
        "cancelled": "⛔ 已取消",
        "pending": "⚪ 排队中",
        "running": "🔄 处理中",
    }

    def _auth_listing_task(self, task_id: str) -> dict:
        """刊登任务访问/控制统一闸：不存在与无权限同一响应（404）。"""
        task = self._store.get_listing_task(str(task_id))
        if task is None or task["store_id"] not in self._my_store_ids():
            raise PtApiError("刊登任务不存在", 404)
        return task

    def listing_tasks_list(self, q: dict) -> dict:
        self._user()
        limit = 30
        try:
            limit = max(1, min(int((q.get("limit") or ["30"])[0]
                                   if isinstance(q.get("limit"), list)
                                   else q.get("limit") or 30), 100))
        except (TypeError, ValueError):
            pass
        my = self._my_store_ids()
        rows = []
        n = 0
        for t in self._store.listing_tasks():
            if t["store_id"] not in my:
                continue
            n += 1
            if n > limit:
                break
            counts = self._store.listing_item_counts(t["id"])
            total = max(counts.get("total") or 0, 1)
            done = counts["completed"] + counts["blocked"] \
                + counts["retryable_failed"] + counts["permanent_failed"] \
                + counts["cancelled"]
            rows.append({**t, "total": counts["total"],
                         "progress_percent": round(done * 100.0 / total, 1),
                         **{k: counts[k] for k in (
                             "completed", "blocked", "retryable_failed",
                             "permanent_failed", "cancelled")}})
        return {"ok": True, "tasks": rows}

    def listing_task_detail(self, task_id: str) -> dict:
        self._user()
        task = self._auth_listing_task(str(task_id))
        counts = self._store.listing_item_counts(task["id"])
        total = max(counts.get("total") or 0, 1)
        done = sum(counts[k] for k in (
            "completed", "blocked", "retryable_failed",
            "permanent_failed", "cancelled"))
        return {"ok": True, "task": task,
                "progress": {**counts,
                             "progress_percent": round(
                                 done * 100.0 / total, 1)},
                "env_note": "模拟数据（Mock 演示，非官方 Listings 结果）"
                            if task["env"] == "mock" else ""}

    def listing_task_items_view(self, task_id: str) -> dict:
        self._user()
        task = self._auth_listing_task(str(task_id))
        items = []
        for x in self._store.listing_task_items(task["id"]):
            sr = {}
            try:
                sr = json.loads(x.get("submit_result_json") or "{}")
            except (TypeError, ValueError):
                sr = {}
            items.append({
                "pid": x["pid"], "sku": x["sku"],
                "parent_sku": x.get("parent_sku") or "",
                "variant_role": x.get("variant_role") or "",
                "state": x["state"],
                "badge": self._LISTING_ITEM_BADGE.get(x["state"],
                                                      x["state"]),
                "stage": x.get("stage"), "attempt": x.get("attempt") or 0,
                "error_kind": x.get("error_kind"),
                "error": x.get("error"),
                "content_sha": str(x.get("content_sha") or "")[:12],
                "assignment_id": x.get("assignment_id"),
                "pt_name": x.get("pt_name") or "",
                "schema_version": x.get("schema_version") or "",
                "attr_revision": str(x.get("attr_revision") or "")[:12],
                "submission_id": x.get("submission_id"),
                "submit_status": sr.get("status"),
                "submit_mock": bool(sr.get("mock")),
                "submit_note": sr.get("note") or "",
                "issues": sr.get("issues") or [],
                # S3-B2 平台结果契约视图（旧行按 state/受理号推导，
                # 不改历史数据）
                "platform": PtStore.platform_view(x),
                "updated_at": x.get("updated_at"),
            })
        return {"ok": True, "task_id": task["id"], "phase": task["phase"],
                "env": task["env"],
                "env_note": "模拟数据（Mock 演示，非官方 Listings 结果）"
                            if task["env"] == "mock" else "",
                "items": items}

    def listing_task_create(self, body: dict) -> dict:
        """创建统一刊登任务（三种来源共用）：single（单商品）/selected
        （勾选多商品）/category（内部分类下商品——集合在此刻定死快照，
        之后分类移动不改变任务范围）。

        - env 硬闸：本阶段仅 mock（live=403 真实 Listings 写通道未接入，
          fail-closed，绝不留「假装能建」的口子）。
        - 商品/店铺/站点/归属与分类任务同一道服务端闸。
        - 条目按实际 SKU 展开（父子变体关系保留）；快照（内容指纹/
          assignment/schema/属性修订）创建时固化，执行前重检。
        - 幂等键=user+店铺+站点+env+SKU 集合指纹；同店同 SKU 活跃任务
          冲突 → 409（store 层 BEGIN IMMEDIATE 事务判定）。
        """
        user = self._user()
        env = self._check_env(body.get("env"))
        if env != "mock":
            raise PtApiError(
                "S2c 第一阶段仅支持 Mock 刊登任务（真实 Listings 写通道"
                "未接入，S2d 再评估）——env 请用 mock", 403)
        store = self._resolve_store(str(body.get("store_id") or ""))
        mk = str(body.get("marketplace_id") or "").strip()
        if mk and mk != store["marketplace_id"]:
            raise PtApiError(
                f"站点与店铺不符：店铺 {store['name']} 是 "
                f"{store['marketplace_id']}，请求却是 {mk}", 403)
        sid, mid = store["id"], store["marketplace_id"]
        source_mode = str(body.get("source_mode") or "").strip().lower()
        if source_mode and source_mode not in ("single", "selected",
                                               "category"):
            raise PtApiError("source_mode 只能是 single/selected/category",
                             400)
        if not source_mode and str(body.get("category") or "").strip() \
                and not body.get("pids"):
            # 给了 category 且没给 pids：按 category 来源推导（显式
            # source_mode 优先；pids 与 category 同给仍以 source_mode 为准）
            source_mode = "category"
        source_ref = ""
        if source_mode == "category":
            cat = str(body.get("category") or "").strip()
            if not cat:
                raise PtApiError("category 来源必须给 category（内部找货"
                                 "分类名）", 400)
            idx = self._own_index_items()
            pids = [str(x.get("pid")) for x in idx
                    if str(x.get("cat") or "").strip() == cat]
            if not pids:
                raise PtApiError(f"分类「{cat}」下没有你的商品"
                                 "（集合为空，任务不建）", 400)
            if len(pids) > _MAX_PIDS:
                raise PtApiError(
                    f"分类下商品 {len(pids)} 个超单任务上限 {_MAX_PIDS}"
                    "——请分批", 400)
            source_ref = cat
        else:
            pids = self._validate_pids(body.get("pids"))
            if not source_mode:
                source_mode = "single" if len(pids) == 1 else "selected"
            if source_mode == "single" and len(pids) != 1:
                raise PtApiError("single 来源必须恰好一个商品", 400)
        prods = self._own_products(pids)      # 所有权（403 拒绝不静默跳过）
        prods = {pid: (self._product_record_fresh(pid) or prods[pid])
                 for pid in prods}   # 快照用此刻真内容（不吃旧缓存）
        items: list[dict] = []
        for pid in pids:
            rec = prods[pid]
            snap = item_snapshot(self._store, rec, sid, mid, env, pid)
            for v in expand_skus(rec, pid):
                items.append({"pid": pid, **v, **snap})
        key_set = ",".join(sorted(f"{i['pid']}:{i['sku']}"
                                  for i in items))
        idem = "lui:" + hashlib.sha1(
            f"{user}|{sid}|{mid}|{env}|{key_set}"
            .encode("utf-8")).hexdigest()[:20]
        # S3-B2 内容指纹：集合×快照×assignment×schema×属性修订——幂等
        # 命中后用它判定“同一业务操作的重试”还是“内容已变的新一轮”
        fp = hashlib.sha256(
            ",".join(sorted(
                f"{i['pid']}:{i['sku']}:{i.get('content_sha')}:"
                f"{i.get('assignment_id')}:{i.get('schema_version')}:"
                f"{i.get('attr_revision')}" for i in items))
            .encode("utf-8")).hexdigest()[:24]
        new_op = body.get("new_operation")
        if new_op is None:
            new_op = False
        if not isinstance(new_op, bool):
            raise PtApiError("new_operation 只能是布尔值（true=显式声明"
                             "新一轮刊登，旧任务保留）", 400)
        eng = self._listing_engine(sid)
        task = eng.create_task(
            user_id=user, store_id=sid, marketplace_id=mid, env=env,
            source_mode=source_mode, items=items, source=f"ui:{user}",
            source_ref=source_ref, idem_key=idem, idem_fp=fp,
            new_operation=new_op)
        self._op(user, "listing_task_create", task_id=task["id"], env=env,
                 store_id=sid, marketplace_id=mid, source_mode=source_mode,
                 source_ref=source_ref, pids=len(pids), skus=len(items),
                 created=bool(task.get("_created")),
                 new_operation=bool(new_op), idem_fp=fp,
                 replay=bool(task.get("_verdict") == "replay"))
        if task.get("_created"):
            self._spawn_listing_run(sid, task["id"])
        return {"ok": True, "task": task,
                "env_note": "模拟数据（Mock 演示，非官方 Listings 结果）"}

    def listing_task_control(self, task_id: str, op: str, body: dict
                             ) -> dict:
        user = self._user()
        task = self._auth_listing_task(str(task_id))   # 越权=不存在
        sid = task["store_id"]
        eng = self._listing_engine(sid)
        tid = task["id"]
        pending = False
        if op == "pause":
            ok = eng.request_pause(tid)
        elif op == "cancel":
            ok = eng.request_cancel(tid)
        elif op == "resume":
            ok = eng.resume(tid)
            if ok:
                self._spawn_listing_run(sid, tid)
            elif task["phase"] == "pausing":
                # 暂停未落定窗口：持久化「继续」意图，暂停落定时由引擎
                # 原子消费并自动续跑（后到的 pause/cancel 压掉它）。
                self._store.set_listing_resume_pending(tid)
                pending = True
        elif op == "retry":
            # 显式重试：blocked（人工修复后重检）+ 失败条目拉回 pending。
            # completed 条目绝不动——已受理提交（submission_id 落库）
            # 不允许盲目重试提交（验收令硬约束）。
            if task["phase"] not in ("completed", "completed_with_issues",
                                     "failed", "paused", "interrupted"):
                raise PtApiError(
                    f"任务 {task['phase']} 状态下不能重试"
                    "（先暂停或等它结束）", 409)
            n = self._store.retry_listing_failed(tid)
            if not n:
                raise PtApiError(
                    "没有可重试的条目（已受理的提交不会重发；全部条目"
                    "已是终态）", 400)
            ok = self._store.cas_listing_phase(
                tid, ("completed", "completed_with_issues", "failed",
                      "paused", "interrupted"), "running")
            if ok:
                self._store.set_listing_resume_pending(tid, False)
                self._spawn_listing_run(sid, tid)
            else:
                raise PtApiError("重试没抢到运行相位（任务状态刚变了，"
                                 "刷新再看）", 409)
        else:                                    # pragma: no cover
            raise PtApiError("未知操作", 400)
        extra: dict = {"phase": self._store.get_listing_task(tid)["phase"]}
        if op == "resume":
            extra["pending"] = bool(pending)
        self._op(user, f"listing_task_{op}", task_id=tid, ok=bool(ok),
                 **extra)
        cur = self._store.get_listing_task(tid)
        return {"ok": True, "applied": bool(ok), "pending": bool(pending),
                "task": cur,
                "env_note": "模拟数据（Mock 演示，非官方 Listings 结果）"}

    def _listing_mock_call(self, envobj, method: str, seller_id: str,
                           marketplace_id: str, sku: str,
                           payload: dict | None, idem_token: str | None
                           ) -> dict:
        """经 mock 环境传输调 Listings 模拟端点（host 锁死在 mock 服务，
        物理发不出真实请求）；HTTP 错误映射 ListingSubmitError。"""
        if getattr(envobj, "mode", "") != "mock":
            # 双保险：环境对象不是 mock（哪怕上游装配错了）也绝不发请求
            raise ListingSubmitError(
                "提交通道环境不是 mock——S2c 第一阶段仅允许模拟提交",
                kind="environment", retryable=False)
        base = envobj.endpoint_for(marketplace_id).rstrip("/")
        url = (base + f"/listings/2021-08-01/items/"
               + quote(str(seller_id or "SELLER"), safe="")
               + "/" + quote(str(sku), safe=""))
        headers = {"content-type": "application/json",
                   "x-amz-access-token": "T-MOCK",
                   "user-agent": "wz-desktop-pt/1.0"}
        data = None
        if method == "PUT":
            data = json.dumps(payload or {}, ensure_ascii=False).encode(
                "utf-8")
            headers["X-WZ-Idempotency-Token"] = str(idem_token or "")
        try:
            status, _hdrs, raw = envobj.transport(method, url, headers, 10.0,
                                                  body=data)
        except Exception as e:  # noqa: BLE001 —— 网络/超时归一
            raise ListingSubmitError(f"提交通道网络错误：{e}", kind="network",
                                     retryable=True) from e
        if status != 200 or not raw:
            msg = raw[:200].decode("utf-8", "replace") if raw else ""
            retryable = status in (429, 500, 502, 503, 504)
            kind = {400: "badrequest", 401: "auth", 403: "forbidden",
                    404: "notfound", 429: "ratelimit"}.get(
                        status, "server" if status >= 500 else "http")
            raise ListingSubmitError(
                f"提交 HTTP {status}：{msg}", kind=kind, retryable=retryable)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ListingSubmitError("提交响应不是合法 JSON", kind="parse",
                                     retryable=True) from e

    def _listing_engine(self, sid: str) -> ListingEngine:
        """刊登引擎（仅 mock：提交通道只装配 mock 传输——live 任务在
        创建入口已被 403 拒绝，这里物理上不存在 live 通道）。"""
        url = str(self._deps["mock_url"]() or "")
        if not url:
            raise PtApiError("本机没配置 SP-API mock 服务，不能建模拟"
                             "刊登任务", 403)
        envobj = build_environment("mock", mock_url=url)
        seller = self._seller_of(sid)

        def mock_submit(seller_id, marketplace_id, sku, payload, idem_token):
            return self._listing_mock_call(
                envobj, "PUT", seller_id, marketplace_id, sku, payload,
                idem_token)

        def mock_status(seller_id, marketplace_id, sku, submission_id):
            return self._listing_mock_call(
                envobj, "GET", seller_id, marketplace_id, sku, None, None)

        def _envf(name: str, default: float) -> float:
            # 测试/运维旋钮（受理后轮询预算、轮询间隔、租约 TTL）——
            # 只影响等待节奏，不改任何安全语义
            try:
                return float(os.environ.get(name) or "") or default
            except ValueError:
                return default

        return ListingEngine(
            store=self._store, mock_submit=mock_submit,
            mock_status=mock_status,
            load_record=self._load_record_for_listing,
            schema_analysis=self._schema_analysis,
            cache_root=str(self._cache), seller_id=seller,
            poll_budget=_envf("WZ_PT_POLL_BUDGET", DEFAULT_POLL_BUDGET),
            poll_interval=_envf("WZ_PT_POLL_INTERVAL",
                                DEFAULT_POLL_INTERVAL),
            lease_ttl=_envf("WZ_PT_LEASE_TTL", DEFAULT_LEASE_TTL))

    def _product_record_fresh(self, pid: str) -> dict | None:
        """绕过 pid 级记录缓存直读服务器（S2c 刊登快照专用）。

        创建与执行前重检都必须看到商品此刻的真内容——走 _products
        缓存会把旧记录喂给快照，内容变更重检就形同虚设。读到的
        新记录顺手刷新缓存（分类等后续读受益）。"""
        try:
            data = self._deps["prod_list"](pid)
        except Exception:  # noqa: BLE001
            return None
        if not (data or {}).get("ok") or not (data or {}).get("product"):
            return None
        rec = normalize_product_record((data or {}).get("product") or {},
                                       pid)
        if len(self._products) >= _PRODUCT_CACHE_MAX:
            self._products.pop(next(iter(self._products)))
        self._products[pid] = (rec, None, time.time())
        return rec

    def _load_record_for_listing(self, pid: str) -> dict | None:
        """标准化商品记录（不存在/失权/读不到=None——快照重检据此
        blocked，绝不静默跳过）。"""
        return self._product_record_fresh(pid)

    def _spawn_listing_run(self, sid: str, task_id: str) -> None:
        """后台推进一个刊登任务（线程退出即从注册表摘除）。"""
        eng = self._listing_engine(sid)    # 构造失败立刻上抛（HTTP 层
        with self._lock:                   # 显式报错，任务留在库里）
            th = self._runners.get(task_id)
            if th is not None and th.is_alive():
                return

            def work():
                try:
                    eng.run(task_id)
                except Exception:  # noqa: BLE001 —— 状态真源在库，
                    # 线程死亡可经 recover/续跑兜底，绝不让异常穿到 HTTP
                    import logging

                    logging.getLogger("pt_api").exception(
                        "刊登任务运行线程异常退出 task=%s", task_id)
                finally:
                    with self._lock:
                        self._runners.pop(task_id, None)

            th = threading.Thread(target=work, daemon=True,
                                  name=f"pt-lrun-{task_id[:16]}")
            self._runners[task_id] = th
            th.start()

    def assignment_confirm(self, pid: str, body: dict) -> dict:
        """人工复核确认（批复五·D / 批复七）：从官方候选证据里选一个
        PT（可带候选证据里的 BN 节点），写 chosen_by="human" 分配。
        人工确认≠官方验证通过；历史经 supersede 链全留。"""
        user = self._user()
        pid = str(pid or "").strip()
        if not pid:
            raise PtApiError("缺 pid", 400)
        env = self._check_env(body.get("env"))
        store = self._resolve_store(str(body.get("store_id") or ""))
        sid, mid = store["id"], store["marketplace_id"]
        pt = str(body.get("pt_name") or "").strip().upper()
        node = str(body.get("node_id") or "").strip()
        reason = str(body.get("reason") or "").strip()[:200]
        if not pt:
            raise PtApiError("缺 pt_name（要确认的商品类型）", 400)
        try:
            data = self._deps["prod_list"](pid)
        except Exception:  # noqa: BLE001
            raise PtApiError("读商品时服务器不可达", 502)
        if not (data or {}).get("ok") or not (data or {}).get("product"):
            raise PtApiError("商品不存在或不归当前账号", 403)
        cands = self._store.candidates(sid, mid, pid, env=env)
        if pt not in {str(c.get("pt_name") or "").upper() for c in cands}:
            raise PtApiError(
                f"{pt} 不在官方候选集里（人工确认也只能从候选证据里选，"
                "不能编造）", 400)
        if node:
            # 节点只能来自该候选全部策略行的 evidence.classification_ids
            # （库里一策略一行、evidence 是该策略的扁平证据 dict，
            # 同一 PT 可能多条策略行——跨行查证）
            ok_nodes = []
            for c in cands:
                if str(c.get("pt_name") or "").upper() != pt:
                    continue
                evd = c.get("evidence")
                if isinstance(evd, dict):
                    ok_nodes += [str(x) for x in
                                 evd.get("classification_ids") or []]
            if node not in ok_nodes:
                raise PtApiError(
                    f"节点 {node} 不在该候选的官方证据里（不能凭记忆填）",
                    400)
        prev = self._store.current_assignment(sid, mid, pid, env=env)
        aid = self._store.add_assignment(
            store_id=sid, marketplace_id=mid, pid=pid, pt_name=pt,
            schema_cache_key=(prev or {}).get("schema_cache_key"),
            chosen_by="human", confidence=None,
            reason=(f"人工复核（{user}）：" + (reason or "从候选中确认")),
            env=env)
        self._op(user, "assignment_confirm", pid=pid, pt_name=pt,
                 node_id=node or None, env=env, assignment_id=aid,
                 superseded=(prev or {}).get("id"))
        return {"ok": True, "pid": pid, "pt_name": pt,
                "assignment_id": aid,
                "superseded_assignment": (prev or {}).get("id"),
                "chosen_by": "human",
                "note": "已记录人工确认（历史保留）；人工确认不等于"
                        "「官方验证通过」"}

    # ==================================================================
    # 属性值读写（S1 隔离能力：不接 UI、不接分类任务管线）
    # ==================================================================

    def _attr_scope(self, pid: str, body_or_q: dict
                    ) -> tuple[str, str, str, str, str]:
        """属性操作统一闸：身份 + 店铺 + env + 商品归属 + 服务器侧 sku。

        user/store/marketplace 全部服务端推导（会话/店铺清单/token 商品
        读取）——客户端提交的 user_id/store_id 一律忽略，marketplace/
        env 不得由客户端自定义越权（env 只能在 mock|live 里选，且各自
        有配置闸）。返回 (user, sid, mid, env, sku)。
        """
        user = self._user()
        pid = str(pid or "").strip()
        if not pid:
            raise PtApiError("缺 pid", 400)
        env = self._check_env(_q1(body_or_q, "env", "live"))
        store = self._resolve_store(_q1(body_or_q, "store")
                                    or _q1(body_or_q, "store_id"))
        sid, mid = store["id"], store["marketplace_id"]
        try:
            data = self._deps["prod_list"](pid)
        except Exception:  # noqa: BLE001
            raise PtApiError("读商品时服务器不可达", 502)
        if not (data or {}).get("ok") or not (data or {}).get("product"):
            raise PtApiError("商品不存在或不归当前账号", 403)
        sku = ""
        try:
            sku = str(self._product_record(pid).get("sku") or "")
        except Exception:  # noqa: BLE001 —— sku 只是伴随字段，取不到不拦
            sku = ""
        return user, sid, mid, env, sku

    def _attr_assignment(self, sid: str, mid: str, pid: str, env: str,
                         assignment_id) -> tuple[int, dict]:
        """assignment 归属与现行性校验（不存在与无权同一响应 404）。"""
        try:
            aid = int(assignment_id)
        except (TypeError, ValueError):
            raise PtApiError("assignment_id 必须是数字", 400)
        a = self._store.assignment_by_id(aid)
        if (a is None or a.get("store_id") != sid
                or a.get("marketplace_id") != mid or a.get("pid") != pid
                or a.get("env") != env):
            raise PtApiError("assignment 不存在", 404)
        if not a.get("is_current"):
            raise PtApiError("该分类分配已被顶替（属性须绑定现行分配；"
                             "旧属性已随失效保留在历史）", 409)
        meta = self._store.get_schema_meta(
            str(a.get("schema_cache_key") or "")) or {}
        return aid, {"pt_name": str(a.get("pt_name") or ""),
                     "schema_cache_key": str(a.get("schema_cache_key") or ""),
                     "schema_version": str(meta.get("product_type_version")
                                           or "")}

    def attr_list(self, pid: str, q: dict) -> dict:
        """当前有效属性值（可选 ?history=1 带全部修订链）。"""
        user, sid, mid, env, _sku = self._attr_scope(pid, q)
        rows = self._store.get_attributes(
            sid, mid, env, pid,
            include_history=_q1(q, "history") in ("1", "true"))
        self._op(user, "attr_list", pid=pid, env=env,
                 n=len(rows), history=_q1(q, "history") in ("1", "true"))
        return {"ok": True, "pid": pid, "env": env,
                "store_id": sid, "marketplace_id": mid,
                "attributes": [normalize_row(r) for r in rows],
                "note": "A/B/C/D 是证据分级，不是官方校验状态，更不是"
                        "可刊登结论"}

    def attr_history(self, pid: str, q: dict) -> dict:
        """单属性修订链（含 stale/superseded 历史，新→旧）。"""
        user, sid, mid, env, _sku = self._attr_scope(pid, q)
        path = _q1(q, "path").strip()
        if not path:
            raise PtApiError("缺 path（属性路径）", 400)
        try:
            rows = self._store.get_attribute_history(
                sid, mid, env, pid, path)
        except ValueError as e:
            raise PtApiError(str(e), 400)
        self._op(user, "attr_history", pid=pid, env=env, path=path[:80],
                 n=len(rows))
        return {"ok": True, "pid": pid, "attribute_path": path,
                "history": [normalize_row(r) for r in rows]}

    def attr_propose(self, pid: str, body: dict) -> dict:
        """批量登记属性值（引擎/工具侧入口；一律 proposed，不确认）。

        body: {store_id?, env, assignment_id, attributes: [
          {attribute_path, value, source_grade A-D, source_type,
           source_reference?, confidence?, validation_status?,
           validation_errors?}]}
        pt_name / schema_cache_key / schema_version / sku 全部服务端从
        assignment 与商品记录推导——客户端不可自定义绑定上下文。
        """
        user, sid, mid, env, sku = self._attr_scope(pid, body)
        aid, ctx = self._attr_assignment(sid, mid, pid, env,
                                         body.get("assignment_id"))
        try:
            out = self._store.put_attributes(
                user_id=user, store_id=sid, marketplace_id=mid, env=env,
                pid=pid, sku=sku, assignment_id=aid, pt_name=ctx["pt_name"],
                schema_cache_key=ctx["schema_cache_key"],
                schema_version=ctx["schema_version"],
                attributes=body.get("attributes"))
        except ValueError as e:
            raise PtApiError(str(e), 400)
        self._op(user, "attr_propose", pid=pid, env=env, assignment_id=aid,
                 created=out["created"], unchanged=out["unchanged"],
                 paths=[str(r.get("attribute_path")) for r in out["rows"]
                        ][:20])
        return {"ok": True, "pid": pid, "assignment_id": aid, **{
            k: out[k] for k in ("created", "unchanged")},
            "attributes": [normalize_row(r) for r in out["rows"]],
            "note": "登记值一律 proposed——确认只能经人工复核接口"}

    def _schema_analysis(self, cache_key: str):
        """S2a fill 前置：只读校验 Schema 缓存，fail-closed。

        元数据 ok / 文件在 / sha256 一致 / 可解析 / 至少一个属性——
        任一不符 409 拒绝生成候选，绝不拿旧 Schema 或坏文件填充。
        """
        key = str(cache_key or "").strip()
        if not key:
            raise PtApiError("assignment 未携带 schema_cache_key"
                             "（先完成分类与 Schema 下载）", 409)
        meta = self._store.get_schema_meta(key) or {}
        path = schemas_dir(self._cache) / _cache_filename(key)
        if (meta.get("status") != ST_OK or not meta.get("payload_sha256")
                or not path.is_file()):
            raise PtApiError("Schema 缓存不可用（元数据/文件缺失）——"
                             "先重跑分类等 Schema 下载", 409)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != str(meta.get("payload_sha256")):
            raise PtApiError("Schema 缓存哈希不符（疑似被改写）——"
                             "拒绝用于生成候选", 409)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise PtApiError("Schema 缓存不是合法 JSON——拒绝用于生成候选",
                             409)
        if not isinstance(payload, dict) or not analyze_schema(payload).properties:
            raise PtApiError("Schema 解析后零属性（疑似无效 Schema）——"
                             "拒绝用于生成候选", 409)
        return analyze_schema(payload)

    def attr_fill(self, pid: str, body: dict) -> dict:
        """S2a 属性候选生成：facts+Schema → proposed 候选（只生成不确认）。

        user/store/marketplace/env/sku 全部服务端推导（_attr_scope）；
        pt_name/schema_cache_key/schema_version 从现行 assignment 推导
        （客户端可给 assignment_id，服务端校验归属与现行性）。只读本地
        Schema 缓存（_schema_analysis fail-closed），不调任何 SP-API
        Listings 写接口。
        """
        user, sid, mid, env, sku = self._attr_scope(pid, body)
        if body.get("assignment_id") is not None:
            aid, ctx = self._attr_assignment(sid, mid, pid, env,
                                             body.get("assignment_id"))
        else:
            cur = self._store.current_assignment(sid, mid, pid, env=env)
            if cur is None:
                raise PtApiError("该商品还没有分类分配——先完成分类再补属性",
                                 409)
            meta = self._store.get_schema_meta(
                str(cur.get("schema_cache_key") or "")) or {}
            aid = int(cur["id"])
            ctx = {"pt_name": str(cur.get("pt_name") or ""),
                   "schema_cache_key": str(cur.get("schema_cache_key") or ""),
                   "schema_version": str(meta.get("product_type_version")
                                         or "")}
        analysis = self._schema_analysis(ctx["schema_cache_key"])
        facts = extract_facts(
            self._product_record(pid),
            item={"model": self._product_model(pid)})
        out = fill_attributes(facts, analysis, pt_name=ctx["pt_name"],
                              assignment_id=aid)
        cands = out["candidates"]
        created = unchanged = 0
        rows: list[dict] = []
        try:
            for i in range(0, len(cands), MAX_ATTRIBUTES_PER_CALL):
                r = self._store.put_attributes(
                    user_id=user, store_id=sid, marketplace_id=mid, env=env,
                    pid=pid, sku=sku, assignment_id=aid,
                    pt_name=ctx["pt_name"],
                    schema_cache_key=ctx["schema_cache_key"],
                    schema_version=ctx["schema_version"],
                    attributes=cands[i:i + MAX_ATTRIBUTES_PER_CALL])
                created += r["created"]
                unchanged += r["unchanged"]
                rows += list(r["rows"])
        except ValueError as e:
            raise PtApiError(str(e), 400)
        self._op(user, "attr_fill", pid=pid, env=env, assignment_id=aid,
                 created=created, unchanged=unchanged, counts=out["counts"])
        return {"ok": True, "pid": pid, "assignment_id": aid,
                "created": created, "unchanged": unchanged,
                "counts": out["counts"], "missing": out["missing"][:50],
                "conflicts": out["conflicts"], "compat": out["compat"],
                "notes": out["notes"],
                "attributes": [normalize_row(r) for r in rows],
                "note": "填充候选一律 proposed——确认只能人工复核；"
                        "A/B/C/D 是证据分级，不是官方校验结论"}

    def _product_model(self, pid: str) -> str:
        """商品顶层 model 字段（采集器 20 列没有型号列；取不到按无型号）。"""
        try:
            prod = (self._deps["prod_list"](pid) or {}).get("product") or {}
            return str(prod.get("model") or "").strip()
        except Exception:  # noqa: BLE001 —— model 只是伴随字段，失败不拦
            return ""

    def attr_review(self, pid: str, body: dict) -> dict:
        """人工复核（confirm / reject）。confirmed 只经此人工路径。"""
        user, sid, mid, env, _sku = self._attr_scope(pid, body)
        action = str(body.get("action") or "").strip().lower()
        if action not in ("confirm", "reject"):
            raise PtApiError("action 只能是 confirm 或 reject", 400)
        path = str(body.get("attribute_path") or "").strip()
        if not path:
            raise PtApiError("缺 attribute_path", 400)
        new_status = "confirmed" if action == "confirm" else "rejected"
        note = str(body.get("note") or "").strip()[:200]
        try:
            row = self._store.set_review_status(
                user_id=user, store_id=sid, marketplace_id=mid, env=env,
                pid=pid, attribute_path=path, new_status=new_status,
                note=note, human=True)
        except ValueError as e:
            raise PtApiError(str(e),
                             409 if "失效" in str(e) else 400)
        self._op(user, "attr_review", pid=pid, env=env, path=path[:80],
                 do=action, to=new_status)
        return {"ok": True, "attribute": normalize_row(row),
                "note": "人工复核记录；confirmed ≠ 官方校验通过，更不是"
                        "可刊登结论"}

    def attr_edit(self, pid: str, body: dict) -> dict:
        """人工编辑属性值（新修订行 review_status=edited；旧行进历史）。"""
        user, sid, mid, env, sku = self._attr_scope(pid, body)
        path = str(body.get("attribute_path") or "").strip()
        if not path:
            raise PtApiError("缺 attribute_path", 400)
        if "value" not in body:
            raise PtApiError("缺 value", 400)
        # 人工编辑绑定的上下文：优先现行 assignment（服务端推导）
        cur = self._store.current_assignment(sid, mid, pid, env=env)
        if cur is None:
            raise PtApiError("该商品还没有分类分配——先完成分类再补属性",
                             409)
        ctx = {"assignment_id": int(cur["id"]),
               "pt_name": str(cur.get("pt_name") or ""),
               "schema_cache_key": str(cur.get("schema_cache_key") or "")}
        meta = self._store.get_schema_meta(ctx["schema_cache_key"]) or {}
        note = str(body.get("note") or "").strip()[:200]
        try:
            row = self._store.edit_attribute(
                user_id=user, store_id=sid, marketplace_id=mid, env=env,
                pid=pid, attribute_path=path, value=body.get("value"),
                assignment_id=ctx["assignment_id"],
                pt_name=ctx["pt_name"],
                schema_cache_key=ctx["schema_cache_key"],
                schema_version=str(meta.get("product_type_version") or ""),
                sku=sku, note=(note or "人工编辑"))
        except ValueError as e:
            raise PtApiError(str(e),
                             409 if "失效" in str(e) else 400)
        self._op(user, "attr_edit", pid=pid, env=env, path=path[:80],
                 assignment_id=ctx["assignment_id"])
        return {"ok": True, "attribute": normalize_row(row),
                "note": "人工编辑=新修订（旧行保留在历史链）"}

    # ==================================================================
    # S2d：校验引擎入口 + 异常复核队列（引擎只开工单，处置只能人工）
    # ==================================================================

    def validation_run(self, pid: str, q: dict) -> dict:
        """S2d 校验：此刻重跑完整校验（引擎=pt.validate）并落报告快照
        +异常队列。?stored=1 只读现行报告（不重跑不落库）。
        ?sku= 落单 SKU 位（缺省商品级 sku=''）。"""
        user, sid, mid, env, _sku = self._attr_scope(pid, q)
        if _q1(q, "stored") in ("1", "true"):
            cur = self._store.current_validation(
                sid, mid, pid, env, sku=_q1(q, "sku"))
            if cur is None:
                raise PtApiError("还没有校验报告（先跑一次校验）", 404)
            try:
                rep = json.loads(cur.get("report_json") or "{}")
            except ValueError:
                rep = {}
            self._op(user, "validation_stored", pid=pid, env=env,
                     vid=cur.get("id"))
            return {"ok": True, "pid": pid, "env": env, "stored": True,
                    "validation_id": cur.get("id"),
                    "validated": bool(cur.get("validated")),
                    "exception_types": str(cur.get("exception_types")
                                           or "").split(","),
                    "report": rep}
        cur_asg = self._store.current_assignment(sid, mid, pid, env=env)
        if cur_asg is None:
            raise PtApiError("该商品还没有分类分配——先完成分类再校验",
                             409)
        analysis = self._schema_analysis(
            str(cur_asg.get("schema_cache_key") or ""))
        rows = self._store.get_attributes(sid, mid, env, pid)
        record = self._product_record(pid)
        vers = self._store.verifications(sid, mid, pid, "pt_exists")
        ver = next((v for v in reversed(vers)
                    if v.get("verified_against") == env), {})
        vstat = ver.get("status") or None
        rep = validate_product(
            analysis, rows, variant_roles=expand_skus(record, pid),
            assignment=cur_asg,
            pt_verified=(True if vstat == "passed"
                         else (False if vstat else None)))
        meta = self._store.get_schema_meta(
            str(cur_asg.get("schema_cache_key") or "")) or {}
        vid = self._store.save_validation(
            sid, mid, pid, env, sku=_q1(q, "sku"),
            fingerprint={
                "content_sha": content_fingerprint(record),
                "assignment_id": cur_asg.get("id"),
                "pt_name": str(cur_asg.get("pt_name") or ""),
                "schema_cache_key": str(cur_asg.get("schema_cache_key")
                                        or ""),
                "schema_version": str(meta.get("product_type_version")
                                      or ""),
                "attr_revision": attr_revision(
                    self._store, sid, mid, env, pid)},
            report=report_to_dict(rep), source="api:validation")
        self._op(user, "validation_run", pid=pid, env=env, vid=vid,
                 validated=rep.validated, blocking=rep.blocking)
        return {"ok": True, "pid": pid, "env": env, "stored": False,
                "validation_id": vid, "validated": rep.validated,
                "report": report_to_dict(rep, ), "note": rep.guard}

    def review_queue_view(self, q: dict) -> dict:
        """异常复核队列：按店铺/站点/env/商品/SKU/类型/状态过滤（验收令
        「按商品、店铺、站点、类型查看」）。店铺轴=本人店铺清单（授权
        边界）；按 pid 过滤时再走商品所有权闸。"""
        user = self._user()
        env = self._check_env(_q1(q, "env", "live"))
        store = self._resolve_store(_q1(q, "store")
                                    or _q1(q, "store_id"))
        pid = _q1(q, "pid")
        if pid:
            self._own_products([pid])
        try:
            rows = self._store.review_exceptions(
                store_id=store["id"],
                marketplace_id=store["marketplace_id"], env=env,
                pid=pid or None, sku=_q1(q, "sku") or None,
                exception_type=_q1(q, "type") or None,
                status=_q1(q, "status") or "open",
                limit=int(_q1(q, "limit") or 200))
        except ValueError as e:
            raise PtApiError(str(e), 400)
        out = []
        for r in rows:
            r = dict(r)
            try:
                r["detail"] = json.loads(r.pop("detail_json") or "{}")
            except ValueError:
                r["detail"] = {}
            out.append(r)
        self._op(user, "review_queue", env=env, store_id=store["id"],
                 status=_q1(q, "status") or "open", n=len(out))
        return {"ok": True, "env": env, "store_id": store["id"],
                "marketplace_id": store["marketplace_id"],
                "status": _q1(q, "status") or "open", "exceptions": out,
                "note": "处置经 POST /review/exceptions/{id}/resolve"
                        "（人工授权+审计）；引擎只开工单不开结论"}

    def review_resolve(self, exception_id: str, body: dict) -> dict:
        """人工处置异常（验收令三：明确授权 API；操作人=会话用户，
        客户端不可顶名；审计四件套=操作人/时间/原值/新值）。处置动作
        落到属性层时走 S1 人工路径（set_review_status human=True /
        edit_attribute）——本接口绝不代引擎写 confirmed。"""
        user = self._user()
        try:
            xid = int(str(exception_id).strip())
        except (TypeError, ValueError):
            raise PtApiError("异常 id 必须是数字", 400)
        row = self._store.review_exception(xid)
        if row is None:
            raise PtApiError("异常不存在", 404)
        # 授权：异常行的店铺必须是本人店铺，商品必须归本人
        store = self._resolve_store(str(row.get("store_id") or ""))
        sid, mid = store["id"], store["marketplace_id"]
        if (sid != str(row.get("store_id"))
                or mid != str(row.get("marketplace_id"))):
            raise PtApiError("异常行的店铺信息与店铺清单不符", 403)
        pid = str(row.get("pid") or "")
        self._own_products([pid])
        env = str(row.get("env") or "mock")
        path = str(row.get("attribute_path") or "")
        action = str(body.get("action") or "").strip().lower()
        if action not in RESOLUTION_ACTIONS:
            raise PtApiError(
                f"action 只能是 {'/'.join(RESOLUTION_ACTIONS)}", 400)
        note = str(body.get("note") or "").strip()[:200]
        attribute = None
        original = resolved = None
        if action in ("confirm", "edit"):
            rows = self._store.get_attributes(sid, mid, env, pid)
            cur = next((r for r in rows
                        if str(r.get("attribute_path")) == path), None)
            try:
                original = json.loads(
                    (cur or {}).get("value_json") or "null")
            except ValueError:
                original = None
        if action == "confirm":
            if cur is None:
                raise PtApiError(
                    "该属性没有现行行可确认（缺值类异常请用 edit 提供值）",
                    409)
            try:
                attribute = self._store.set_review_status(
                    user_id=user, store_id=sid, marketplace_id=mid,
                    env=env, pid=pid, attribute_path=path,
                    new_status="confirmed",
                    note=note or "复核队列人工确认", human=True)
            except ValueError as e:
                raise PtApiError(str(e),
                                 409 if "失效" in str(e) else 400)
        elif action == "edit":
            if "value" not in body:
                raise PtApiError("edit 必须给 value", 400)
            asg = self._store.current_assignment(sid, mid, pid, env=env)
            if asg is None:
                raise PtApiError("该商品还没有分类分配", 409)
            meta = self._store.get_schema_meta(
                str(asg.get("schema_cache_key") or "")) or {}
            try:
                attribute = self._store.edit_attribute(
                    user_id=user, store_id=sid, marketplace_id=mid,
                    env=env, pid=pid, attribute_path=path,
                    value=body.get("value"), assignment_id=int(asg["id"]),
                    pt_name=str(asg.get("pt_name") or ""),
                    schema_cache_key=str(asg.get("schema_cache_key")
                                         or ""),
                    schema_version=str(meta.get("product_type_version")
                                       or ""),
                    sku=str(row.get("sku") or ""),
                    note=note or "复核队列人工修正")
            except ValueError as e:
                raise PtApiError(str(e),
                                 409 if "失效" in str(e) else 400)
            resolved = body.get("value")
        # dismiss/reclassify：不动属性（人工判定记录在案；改分类走既有
        # assignments/confirm 人工链）
        try:
            rec = self._store.resolve_review_exception(
                xid, action=action, operator=user, note=note,
                original_value=original, resolved_value=resolved)
        except KeyError:
            raise PtApiError("异常不存在", 404)
        except ValueError as e:
            raise PtApiError(str(e), 409)
        rec = dict(rec)
        try:
            rec["detail"] = json.loads(rec.pop("detail_json") or "{}")
        except ValueError:
            rec["detail"] = {}
        self._op(user, "review_resolve", xid=xid, pid=pid, env=env,
                 do=action, path=path[:80])
        return {"ok": True, "exception": rec,
                "attribute": normalize_row(attribute)
                if attribute is not None else None,
                "note": "人工处置已审计；建议重跑校验（GET /validation/"
                        + pid + "）确认全链"}

    # ==================================================================
    # 装配：engine / 客户端 / 商品装载 / 后台 runner
    # ==================================================================

    def _product_record(self, pid: str) -> dict:
        """prod_list 响应 → pt.material 的 record 形状（带缓存）。

        D1 修复：统一走 pt.normalize.normalize_product_record——
        live 服务器（英文键+raw_data 嵌套中文 20 列）与 mock/历史
        （中文键顶层）三形状都标准化成 material 契约；原始行保留在
        record["_raw_original"] 供追踪。生产与 mock 同一函数（任务一.6）。
        """
        hit = self._products.get(pid)
        if hit:
            return hit[0]
        data = self._deps["prod_list"](pid)
        prod = (data or {}).get("product") or {}
        rec = normalize_product_record(prod, pid)
        if len(self._products) >= _PRODUCT_CACHE_MAX:
            self._products.pop(next(iter(self._products)))
        self._products[pid] = (rec, None, time.time())
        return rec

    def _load_product(self, pid: str):
        """TaskEngine.load_product：商品不存在返回 None（→资料缺失复核）。
        服务器读不到（异常）也按 None 处理——绝不带崩批量。"""
        try:
            data = self._deps["prod_list"](pid)
        except Exception:  # noqa: BLE001
            return None
        if not (data or {}).get("ok") or not (data or {}).get("product"):
            return None
        return (self._product_record(pid), None)   # understanding 暂缺省

    def _client_for(self, env: str, sid: str) -> SpApiClient:
        if env == "mock":
            if self._mock_client is None:
                self._mock_client = SpApiClient(
                    build_environment("mock",
                                      mock_url=str(self._deps["mock_url"]())),
                    token_provider=lambda: "T-MOCK",
                    retry=RetryConfig(max_attempts=2, base_delay=0.05,
                                      retry_after_cap=0.2),
                    timeout=10.0)
            return self._mock_client
        env_obj = self._deps["live_env"]()
        if env_obj is None:
            raise PtApiError("live 环境未配置", 403)

        # S3-C 二阶段：令牌通道改走授权管理器（按店隔离+缓存+失败分类）。
        # 构造即 fail-fast 预取一次——未授权店铺在进 live 工作流入口就被
        # 403 拦下（分类直达文案），不再等到请求瞬间才炸；
        # token_provider 保留给令牌自然过期后的续取。
        try:
            self._authz.token(sid)
        except AuthzError as e:
            raise PtApiError(
                f"店铺令牌不可用（{e.category}）：{str(e)[:140]}", 403
            ) from e

        def token():
            try:
                return self._authz.token(sid)
            except AuthzError as e:
                raise RuntimeError(f"[{e.category}] 店铺令牌不可用") from e
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"取店铺令牌失败：{e}"[:200]) from e

        return SpApiClient(
            env_obj, token_provider=token,
            retry=RetryConfig(max_attempts=2, base_delay=0.5,
                              retry_after_cap=2.0),
            timeout=30.0)

    def _ai_decide_for(self, env: str):
        key, provider, model = self._deps["resolve_ai"]()
        base = str(self._deps["ai_base"]() or "")
        return GptDecision(key=key, model=model, base=base,
                           provider=provider or "openai",
                           evidence_log=self._ai_log)

    def _light_engine(self) -> TaskEngine:
        """只读/控制用引擎（进度、条目、暂停/取消）：不建网络客户端——
        这些操作的真源是 SQLite，不需要 SP-API/AI。"""
        return TaskEngine(
            store=self._store, client=None,
            ai_decide=lambda payload: {"chosen": None},
            load_product=self._load_product,
            cache_root=str(self._cache))

    def _engine_for(self, env: str, sid: str, pids) -> TaskEngine:
        for p in pids:                     # 预热缓存（建任务时已验所有权）
            if p not in self._products:
                try:
                    self._product_record(p)
                except Exception:  # noqa: BLE001
                    pass
        gpt = self._ai_decide_for(env)
        return TaskEngine(
            store=self._store,
            client=self._client_for(env, sid),
            ai_decide=gpt,
            # P2：受限语义裁决与决策器共用凭证/网络/证据管线；语义裁决
            # 本身判的是「商品↔PT 语义归属」，S3 采信才按 env 门控（在
            # matcher.s3_semantic_score 里），这里不因 mock 缺席。
            ai_semantic=gpt.semantic_verdict,
            load_product=self._load_product,
            cache_root=str(self._cache),
            seller_id=self._seller_of(sid),
            max_workers=3, item_max_attempts=2, http_retry_attempts=2,
            ai_concurrency=3)

    def _seller_of(self, sid: str) -> str:
        stores, _ = self._deps["stores"]()
        s = next((x for x in stores if str(x.get("id")) == sid), None)
        return str((s or {}).get("seller_id") or "")

    def _spawn_run(self, env: str, sid: str, task_id: str) -> None:
        """后台推进一个任务（run 是同步循环；线程退出即从注册表摘除）。"""
        eng = self._engine_for(env, sid, [])    # 构造失败立刻上抛（HTTP 层
        with self._lock:                        # 显式报错，任务留在库里）
            th = self._runners.get(task_id)
            if th is not None and th.is_alive():
                return

            def work():
                try:
                    eng.run(task_id)
                except Exception:  # noqa: BLE001 —— 状态真源在库，
                    # 线程死亡可经 recover/续跑兜底，绝不让异常穿到 HTTP
                    import logging

                    logging.getLogger("pt_api").exception(
                        "任务运行线程异常退出 task=%s", task_id)
                finally:
                    with self._lock:
                        self._runners.pop(task_id, None)

            th = threading.Thread(target=work, daemon=True,
                                  name=f"pt-run-{task_id[:16]}")
            self._runners[task_id] = th
            th.start()

    def _latest_item(self, sid: str, mid: str, pid: str, env: str
                     ) -> dict | None:
        """该商品最近一次任务条目状态（新任务优先，扫最近 20 个任务）。"""
        for t in self._store.tasks(store_id=sid, marketplace_id=mid,
                                   env=env)[:20]:
            for it in self._store.task_items(t["id"]):
                if it["pid"] == pid:
                    return {"task_id": t["id"], "state": it["state"],
                            "stage": it["stage"],
                            "error_kind": it["error_kind"],
                            "error": it["error"]}
        return None

    @staticmethod
    def _sku_of(prod: dict, pid: str) -> str:
        sku = str(prod.get("sku") or "").strip()
        if sku:
            return sku
        for r in prod.get("raw") or []:
            v = str((r or {}).get("SKU") or "").strip()
            if v:
                return v
        return pid

    # ==================================================================
    # 生命周期
    # ==================================================================

    def startup_recover(self) -> list:
        """进程重启恢复：残留 running/pausing/cancelling → interrupted，
        随后自动续跑（completed 的绝不重跑——真源=条目状态）。单个任务
        起跑失败（如环境能力缺失）只记日志，绝不拖垮启动。
        S2c：刊登任务同构恢复（已受理条目 submission_id 已落库，续跑只
        会续终态轮询，绝不二次提交）。"""
        recovered = self._store.recover_interrupted()
        resumed = []
        for tid in recovered:
            try:
                t = self._store.get_task(tid)
                if t and self._store.cas_task_phase(tid, ("interrupted",),
                                                    "running"):
                    self._spawn_run(t["env"], t["store_id"], tid)
                    resumed.append(tid)
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger("pt_api").exception(
                    "启动恢复单个任务失败 task=%s（保留 interrupted，"
                    "可手动续跑）", tid)
        for tid in self._store.recover_listing_interrupted():
            try:
                t = self._store.get_listing_task(tid)
                if t and self._store.cas_listing_phase(
                        tid, ("interrupted",), "running"):
                    self._spawn_listing_run(t["store_id"], tid)
                    resumed.append(tid)
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger("pt_api").exception(
                    "启动恢复单个刊登任务失败 task=%s（保留 interrupted，"
                    "可手动续跑）", tid)
        return recovered


    # ==================================================================
    # S2b 只读刊登工作台（商品详情弹窗聚合视图，零写零 Listings 调用）
    # ==================================================================

    def _wb_default_env(self, sid: str, mid: str, pid: str) -> str:
        """工作台 env 缺省值（只读面板跟随数据，响应里 env 如实回标）：

        双环境能力齐备时看这个商品在哪边真有分类分配（都有→live，与
        分类中心默认一致）；只有 mock 能力→mock；只有 live→live；
        都没有=409（绝不静默给空面板冒充「没数据」）。"""
        live_cap = self._deps["live_env"]() is not None
        mock_cap = bool(str(self._deps["mock_url"]() or ""))
        if live_cap and mock_cap:
            if self._store.current_assignment(sid, mid, pid, env="live"):
                return "live"
            if self._store.current_assignment(sid, mid, pid, env="mock"):
                return "mock"
            return "live"
        if mock_cap:
            return "mock"
        if live_cap:
            return "live"
        raise PtApiError("没有可用的 PT 环境（live 未配置且 mock 未启用）"
                         "——工作台无法读取；商品详情其余功能不受影响", 409)

    def workbench(self, pid: str, q: dict) -> dict:
        """S2b 只读刊登工作台聚合：分类/属性/校验/Schema/准备度一屏快照。

        身份/店铺/站点/商品归属与 product_detail 同一道服务端闸；准备度
        六步链在服务端算死（done|partial|pending|not_wired）——未接通的
        环节如实标注，绝不伪装完成；内部 cat 只作伴随信息展示，绝不
        冒充 Amazon Product Type。"""
        user = self._user()
        pid = str(pid or "").strip()
        if not pid:
            raise PtApiError("缺 pid", 400)
        env_arg = str(_q1(q, "env") or "").strip().lower()
        env = self._check_env(env_arg) if env_arg else ""
        store = self._resolve_store(_q1(q, "store") or _q1(q, "store_id"))
        sid, mid = store["id"], store["marketplace_id"]
        try:
            data = self._deps["prod_list"](pid)
        except Exception:  # noqa: BLE001
            raise PtApiError("读商品时服务器不可达", 502)
        if not (data or {}).get("ok") or not (data or {}).get("product"):
            raise PtApiError("商品不存在或不归当前账号", 403)
        prod = (data or {}).get("product") or {}
        if not env:                       # 缺省=跟随数据（live/mock 哪边有）
            env = self._wb_default_env(sid, mid, pid)

        asg = self._store.current_assignment(sid, mid, pid, env=env)
        kinds: dict[str, list] = {}
        for v in self._store.verifications(sid, mid, pid):
            kinds.setdefault(v["check_kind"], []).append(v)
        pt_exists = (kinds.get("pt_exists") or [None])[-1]
        browse_node = kinds.get("browse_node") or []

        active = [_wb_row(r) for r in self._store.get_attributes(
            sid, mid, env, pid)]
        hist = self._store.get_attributes(sid, mid, env, pid,
                                          include_history=True)
        stale = [_wb_row(r) for r in hist
                 if r.get("lifecycle") == "stale" and r.get("is_current")]

        by_review: dict[str, int] = {}
        by_grade: dict[str, int] = {}
        val = {"passed": 0, "failed": 0, "pending": 0}
        for r in active:
            rs = str(r.get("review_status") or "")
            by_review[rs] = by_review.get(rs, 0) + 1
            g = str(r.get("source_grade") or "")
            by_grade[g] = by_grade.get(g, 0) + 1
            vs = str(r.get("validation_status") or "")
            if vs in val:
                val[vs] += 1
        conflicts = []
        missing = []
        for r in active:
            if r.get("source_type") == "gap.conflict":
                v = r.get("value")
                conflicts.append({
                    "attribute_path": r.get("attribute_path"),
                    "values": (v.get("conflict")
                               if isinstance(v, dict) else None),
                    "source_reference": r.get("source_reference") or ""})
            elif r.get("source_type") == "gap.missing":
                missing.append({
                    "attribute_path": r.get("attribute_path"),
                    "reason": r.get("source_reference") or ""})

        schema = None
        if asg:
            key = str(asg.get("schema_cache_key") or "")
            meta = self._store.get_schema_meta(key) or {}
            version = str(meta.get("product_type_version") or "")
            schema = {
                "status": str(meta.get("status") or ""),
                "version": version,
                "file_present": bool(key) and (
                    schemas_dir(self._cache) / _cache_filename(key)).is_file(),
                "row_version_mismatch": (sum(
                    1 for r in active
                    if version and str(r.get("schema_version") or "")
                    != version) if version else 0),
            }
        schema_ok = bool(schema and schema["file_present"]
                         and schema["status"] == ST_OK)

        # —— 准备度六步链（服务端算死；未完成项给真实原因）——
        n_active = len(active)
        n_ok = by_review.get("confirmed", 0) + by_review.get("edited", 0)
        pt_ok = bool(asg and str(asg.get("pt_name") or ""))
        ptd_ok = bool(pt_exists and pt_exists.get("status") == "passed")
        blockers: list[str] = []
        if not pt_ok:
            blockers.append("分类未完成（无现行 Product Type 分配）")
        if n_active == 0:
            blockers.append("属性候选还没生成")
        elif n_ok < n_active:
            blockers.append(f"属性未全部确认（{n_ok}/{n_active} 已确认）")
        if not ptd_ok:
            blockers.append("官方 PTD 校验未通过")
        if conflicts:
            blockers.append(f"存在冲突属性 {len(conflicts)} 项（须人工裁决）")
        if asg and not schema_ok:
            blockers.append("Schema 缓存不可用（元数据/文件缺失或状态异常）")
        can_prepare = bool(pt_ok and n_active > 0 and n_ok >= n_active
                           and ptd_ok and not conflicts and schema_ok)

        ptd_detail = "官方 PTD 校验通过（最近一次）"
        if not ptd_ok:
            if pt_exists:
                d = {}
                try:
                    d = json.loads(pt_exists.get("detail_json") or "{}")
                except (TypeError, ValueError):
                    d = {}
                why = str(d.get("reason") or d.get("error")
                          or pt_exists.get("status") or "")
                ptd_detail = f"最近一次官方校验未通过（{why}）——详见分类任务记录"
            else:
                ptd_detail = "官方 PTD 校验未跑（分类任务推进到验证阶段才有）"
        conf = (asg or {}).get("confidence")
        conf_txt = (f"，置信 {round(float(conf), 2)}"
                    if isinstance(conf, (int, float)) else "")
        stale_reason = (str((stale[0] or {}).get("stale_reason") or "")
                        if stale else "")
        # 提交步（S2c 起数据驱动回读，仍零按钮零写入）：live 恒 not_wired
        # （真实 Listings 通道未接入）；mock 读最近一次刊登任务条目——
        # 已受理/平台终态如实展示并处处标注「模拟」，绝不冒充官方结论；
        # 没跑过刊登任务时保持原 not_wired 文案。
        if env == "live":
            sub_state, sub_detail = "not_wired", (
                "真实 Listings 提交通道未接入（S2c 第一阶段 Mock-only，"
                "S2d 评估）——工作台只读，不调任何 Listings 接口")
        else:
            sub_state = "not_wired"
            sub_detail = ("提交通道未接入（S2c 统一任务引擎）——本轮工作台"
                          "只读，不调任何 Listings 接口")
            li = self._store.latest_listing_item(sid, mid, env, pid)
            if li is not None:
                sr = {}
                try:
                    sr = json.loads(li.get("submit_result_json") or "{}")
                except (TypeError, ValueError):
                    sr = {}
                st = str(sr.get("status") or "")
                if li.get("submission_id") and st:
                    if st == "PROCESSED":
                        sub_state = "done"
                        sub_detail = (f"模拟提交已处理完成（Mock，任务 "
                                      f"{li.get('t_id')}，submission "
                                      f"{li.get('submission_id')}）——非"
                                      "官方结论")
                    elif st == "REJECTED":
                        sub_state = "pending"
                        sub_detail = (f"最近一次模拟提交被拒（Mock，任务 "
                                      f"{li.get('t_id')}）："
                                      f"{li.get('error') or '原因见条目'}"
                                      "——修复后经任务重试")
                    else:
                        sub_state = "partial"
                        sub_detail = (f"模拟提交已受理、平台处理中（Mock，"
                                      f"任务 {li.get('t_id')}，submission "
                                      f"{li.get('submission_id')}）——非"
                                      "官方结论")
                else:
                    sub_state = "pending"
                    sub_detail = (f"最近一次刊登任务条目未产生受理记录"
                                  f"（{li.get('state')}"
                                  f"{'·' + str(li.get('error_kind') or '') if li.get('error_kind') else ''}"
                                  f"，任务 {li.get('t_id')}）——处理完前置"
                                  "条件后经任务提交")
        steps = [
            {"key": "classification", "label": "分类完成",
             "state": "done" if pt_ok else "pending",
             "detail": (f"{asg.get('pt_name')}（{asg.get('chosen_by')}"
                        f"{conf_txt}）") if pt_ok else
             "该商品还没有分类分配——先到「AI 分类中心」完成分类或人工确认"},            {"key": "candidates", "label": "属性候选已生成",
             "state": "done" if n_active else "pending",
             "detail": (
                 f"现行候选 {n_active} 行（A {by_grade.get('A', 0)}/"
                 f"B {by_grade.get('B', 0)}/C {by_grade.get('C', 0)}/"
                 f"D {by_grade.get('D', 0)}）"
                 + (f"；已失效 {len(stale)} 行待重新生成" if stale else ""))
             if n_active else (
                 (f"候选已全部失效（{stale_reason}）——须在新 Schema 下重新"
                  "生成" if stale else
                  "还没生成候选（先完成分类，再到属性面板填充）"))},
            {"key": "confirmed", "label": "属性已确认",
             "state": "done" if (n_active and n_ok >= n_active) else
                      ("partial" if n_ok else "pending"),
             "detail": f"{n_ok}/{n_active} 已人工确认" if n_active
             else "无候选可确认"},
            {"key": "ptd_verified", "label": "PTD 校验通过",
             "state": "done" if ptd_ok else "pending",
             "detail": ptd_detail},
            {"key": "ready_to_prepare", "label": "可准备刊登（预检）",
             "state": "done" if can_prepare else "pending",
             "detail": ("全部预检条件满足" if can_prepare else
                        "未完成条件：" + "；".join(blockers)),
             "note": "预检=只读判断，不代表可提交；提交通道见下一步"},
            {"key": "submitted", "label": "已提交及平台处理结果",
             "state": sub_state, "detail": sub_detail},
        ]

        # —— S2e 聚合扩展（全部只读）：本地校验状态 + 该商品异常工单 +
        # 最近一次刊登任务条目（mock 单品入口/结果展示用）——
        validation = None
        cv = self._store.current_validation(sid, mid, pid, env)
        if cv is not None:
            validation = {
                "id": cv.get("id"),
                "validated": bool(cv.get("validated")),
                "exception_types": [x for x in str(
                    cv.get("exception_types") or "").split(",") if x],
                "updated_at": cv.get("updated_at"),
            }
        exceptions = []
        for r in self._store.review_exceptions(
                store_id=sid, marketplace_id=mid, env=env, pid=pid,
                sku=None, exception_type=None, status="open", limit=100):
            r = dict(r)
            try:
                r["detail"] = json.loads(r.pop("detail_json") or "{}")
            except (TypeError, ValueError):
                r["detail"] = {}
            exceptions.append(r)
        latest_listing = None
        if env == "mock":
            li2 = self._store.latest_listing_item(sid, mid, env, pid)
            if li2 is not None:
                latest_listing = {
                    "task_id": li2.get("t_id"), "pid": li2.get("pid"),
                    "sku": li2.get("sku"),
                    "state": li2.get("state"), "stage": li2.get("stage"),
                    "error_kind": li2.get("error_kind"),
                    "error": li2.get("error"),
                    "submission_id": li2.get("submission_id"),
                }

        self._op(user, "workbench_view", pid=pid, env=env, store_id=sid,
                 rows=n_active, stale=len(stale),
                 can_prepare=can_prepare)
        return {
            "ok": True, "pid": pid, "user": user,
            "store_id": sid, "marketplace_id": mid, "env": env,
            "env_note": "模拟数据（Mock 演示，不是官方结论）"
                        if env == "mock" else "",
            "product": {"title": str(prod.get("title") or ""),
                        "sku": self._sku_of(prod, pid),
                        "model": str(prod.get("model") or ""),
                        "internal_cat": str(prod.get("cat") or "")},
            "internal_cat_note": "internal_cat 是内部找货分类，"
                                 "不是 Amazon Product Type（二者语义不同，"
                                 "绝不能混用）",
            "assignment": asg,
            "verifications": {"pt_exists": pt_exists,
                              "browse_node": browse_node},
            "attributes": {"rows": active, "stale": stale,
                           "counts": {"total": n_active,
                                      "by_review_status": by_review,
                                      "by_grade": by_grade,
                                      "validation": val},
                           "conflicts": conflicts, "missing": missing},
            "schema": schema,
            "readiness": {"steps": steps, "can_prepare": can_prepare,
                          "blockers": blockers,
                          "note": "已确认/PTD 通过/预检通过都不等于已刊登；"
                                  "提交与平台处理结果由 S2c 统一任务引擎"
                                  "接入后才有"},
            "validation": validation,
            "validation_note": ("本地校验通过 ≠ 亚马逊平台最终接受"
                                if validation and validation["validated"]
                                else None),
            "exceptions": exceptions,
            "latest_listing": latest_listing,
            "actions_note": ("mock 环境开放人工复核/校验/单品模拟任务入口"
                             "（全部模拟测试）；live 保持只读"
                             if env == "mock" else ""),
        }


def _group_candidates(rows: list[dict]) -> list[dict]:
    """候选按 pt_name 归并（库里一策略一行；同一 PT 多策略合并展示）：
    {pt_name, display_name, score(最高), strategies:[], evidence:{策略: ev}}。
    UI 复核单选/节点证据都以 PT 为单位。"""
    out: dict[str, dict] = {}
    for c in rows:
        pt = str(c.get("pt_name") or "")
        g = out.setdefault(pt, {
            "pt_name": pt,
            "display_name": c.get("display_name") or "",
            "score": None, "strategies": [], "evidence": {}})
        s = str(c.get("strategy") or "")
        if s and s not in g["strategies"]:
            g["strategies"].append(s)
        if s:
            g["evidence"][s] = c.get("evidence") or {}
        sc = c.get("score")
        if sc is not None and (g["score"] is None or sc > g["score"]):
            g["score"] = sc
    return sorted(out.values(),
                  key=lambda g: (g["score"] is None,
                                 -(g["score"] or 0), g["pt_name"]))


def _progress_dict(pr) -> dict:
    """TaskProgress → JSON 安全 dict。"""
    if pr is None:
        return {}
    return {
        "task_id": pr.task_id, "phase": pr.phase, "env": pr.env,
        "total": pr.total, "pending": pr.pending, "running": pr.running,
        "completed": pr.completed, "needs_review": pr.needs_review,
        "retryable_failed": pr.retryable_failed,
        "permanent_failed": pr.permanent_failed, "cancelled": pr.cancelled,
        "progress_percent": pr.progress_percent,
        "current_stage": pr.current_stage, "error": pr.error,
        "finished_at": pr.finished_at,
    }


# S2b 工作台行瘦身：只留展示列（内部列 user_id/store 等不出工作台响应）
_WB_ROW_FIELDS = (
    "attribute_path", "value", "source_grade", "source_type",
    "source_reference", "confidence", "validation_status",
    "validation_errors", "review_status", "schema_version",
    "lifecycle", "stale_reason", "updated_at")


def _wb_row(r: dict) -> dict:
    n = normalize_row(r)
    return {k: n.get(k) for k in _WB_ROW_FIELDS}

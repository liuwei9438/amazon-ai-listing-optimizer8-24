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
import threading
import time
from pathlib import Path

from pt.ai_gpt import GptDecision
from pt.normalize import normalize_product_record
from pt.spapi_client import RetryConfig, SpApiClient, build_environment
from pt.store import PtStore, TaskConflictError
from pt.tasks import TaskEngine

_MAX_PIDS = 500            # 单任务商品数上限（参数校验）
_PRODUCT_CACHE_MAX = 5000  # load_product 缓存上限（item_results 补读用）

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
        except Exception as e:  # noqa: BLE001 —— 绝不带崩主服务
            return {"ok": False,
                    "error": f"pt 服务内部错误：{type(e).__name__}: "
                             f"{e}"[:300]}, 500

    def _route_get(self, rest: list, q: dict) -> dict:
        if rest == ["overview"]:
            return self.overview()
        if rest == ["status"]:
            return self.status_map(q)
        if len(rest) == 2 and rest[0] == "products":
            return self.product_detail(rest[1], q)
        if len(rest) == 2 and rest[0] == "candidates":
            return self.candidates(rest[1], q)
        if rest == ["tasks"]:
            return self.tasks_list(q)
        if len(rest) == 2 and rest[0] == "tasks":
            return self.task_detail(rest[1])
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "items":
            return self.task_items(rest[1])
        raise PtApiError("not found", 404)

    def _route_post(self, rest: list, body: dict) -> dict:
        if rest == ["tasks"]:
            return self.task_create(body)
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] in (
                "pause", "resume", "cancel", "retry"):
            return self.task_control(rest[1], rest[2], body)
        if len(rest) == 3 and rest[0] == "assignments" \
                and rest[2] == "confirm":
            return self.assignment_confirm(rest[1], body)
        raise PtApiError("not found", 404)

    # ==================================================================
    # 安全基元（批复三·1–5）
    # ==================================================================

    def _user(self) -> str:
        user = str(self._deps["user"]() or "")
        if not user:
            raise PtApiError("未登录", 401)
        return user

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

    def _own_pid_set(self) -> set:
        """本账号商品 pid 全集（服务器索引一次读取）。status_map 按
        它过滤：即便同店内混入他人商品，其分类状态/候选元数据也一个
        都不外泄（服务器端过滤，前端隐藏不算数）。"""
        try:
            data = self._deps["prod_list"](None)
        except Exception:  # noqa: BLE001 —— 索引读不到=宁可拒绝不泄露
            raise PtApiError("读商品索引时服务器不可达——为避免越权泄露，"
                             "本次查询取消（稍后重试）", 502)
        items = (data or {}).get("items")
        if not (data or {}).get("ok") or not isinstance(items, list):
            raise PtApiError("商品索引读取失败（稍后重试）", 502)
        return {str(x.get("pid")) for x in items
                if isinstance(x, dict) and x.get("pid") is not None}

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

    def _op(self, user: str, action: str, **kw) -> None:
        """操作日志（JSONL 追加；不改既有表）。"""
        try:
            rec = {"ts": round(time.time(), 3), "user": user,
                   "action": action, **kw}
            with open(self._ops_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ==================================================================
    # 路由实现
    # ==================================================================

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
        return {"ok": True, "task": task, "progress": _progress_dict(pr),
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
        idem = "ui:" + hashlib.sha1(
            f"{user}|{sid}|{mid}|{env}|{','.join(sorted(pids))}"
            .encode("utf-8")).hexdigest()[:20]
        eng = self._engine_for(env, sid, pids)
        task = eng.create_task(
            kind="single" if len(pids) == 1 else "multi",
            store_id=sid, marketplace_id=mid, pids=pids, env=env,
            source=f"ui:{user}", idem_key=idem)
        self._op(user, "task_create", task_id=task["id"], env=env,
                 store_id=sid, marketplace_id=mid, pids=len(pids),
                 created=bool(task.get("_created")))
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

        def token():
            try:
                return str(self._deps["store_token"](sid))
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
            max_workers=2, item_max_attempts=2, http_retry_attempts=2)

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
        起跑失败（如环境能力缺失）只记日志，绝不拖垮启动。"""
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
        return recovered


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

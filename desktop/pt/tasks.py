# -*- coding: utf-8 -*-
"""pt.tasks —— 模块 7：批量分类任务编排（只编排，不重复实现）。

把模块 1–6 连成可靠、可恢复、可审计的批量分类任务系统。本模块只做
编排：商品理解（模块 3 extract_material）/候选检索（retrieve_candidates）
/AI 匹配算法（模块 4 match_product）/HTTP 客户端与限流（模块 2）/
PT·BN 验证（模块 5 verify_*）/Schema 下载解析缓存（模块 6 ensure_schema）
全部经注入调用，绝不二次实现（批复十·1），不碰 main（工程约束），
不碰上传管道、不调刊登接口（第一阶段铁律）。

两层状态（批复二，词表=store v3）：
- 任务整体 created/running/pausing/paused/completed/completed_with_issues/
  failed/interrupted/cancelling/cancelled——控制请求即相位（pausing/
  cancelling 是「已请求、等在途商品到安全检查点」的中间相位），终态由
  逐商品真实状态推导，绝不依赖内存计数器。
- 逐商品 pending/running/completed/needs_review/retryable_failed/
  permanent_failed/cancelled，另记处理阶段 stage（material→…→
  persist_result，与状态两个维度）、失败次数 attempt、错误分类
  error_kind、结果引用 result_ref。
- 单商品失败≠任务失败：任务 failed 只留给任务级不可恢复故障（领取
  循环的存储层异常）。

流程铁律（批复三）：AI 匹配成功≠官方验证；PT 存在≠BN 适配；Schema
成功≠属性齐全；缺口报告≠可刊登；Mock≠官方。needs_review=终态：保留
完整证据（候选/评分/AI 取舍/官方结论全落库）、停止本商品一切自动后续
决策；属性缺口 incomplete 不构成 needs_review（completed≠可刊登，
缺口只是报告）。

暂停/恢复/取消（批复六）：
- 暂停=不再领新商品（claim_item 相位门控）+在途商品到阶段边界安全
  检查点置回 pending+全停后 paused。绝不强杀线程、绝不打断写事务。
- 恢复=resume（created/paused/interrupted→running）后 run()：从逐商品
  状态恢复，完成的不重跑；retryable_failed 按预算自动重排
  （requeue_retryable，attempt<item_max_attempts）；needs_review 不自动
  跳过人工（只有显式 retry_failed 才动 failed 类，且 needs_review 仍不动）。
- 取消=停止领取+未执行条目记 cancelled+已完成结果保留+审计不删+
  取消后不发新网络请求（检查点先行）。
- 进程异常退出=recover()（store.recover_interrupted）：running/pausing/
  cancelling 任务→interrupted、其中 running 条目→pending，防重复分配/
  计数/写入；重跑走 add_assignment 追加链（新 current 顶旧 current），
  绝不产生两条并存 current 的矛盾分配记录（批复五·恢复条款）。

错误分类（批复七，error_kind 词表）：
- auth/forbidden（401/403）→ 条目 retryable_failed(kind=auth)+任务自动
  暂停（停止相关请求等待处理；重授权后 resume 在预算内自动重试）；
- rate_limit/network/server_error/unavailable/schema_unavailable/
  schema_hash_mismatch/storage → retryable_failed（有界：attempt 预算）；
- bad_params/too_large 等参数类 → permanent_failed（不自动重试）；
- no_candidates/all_candidates_unsuitable/review/pt_not_found/
  semantic_failed/semantic_insufficient/bn_no_nodes/bn_insufficient/
  material_missing/schema_no_link → needs_review（证据留全，交人工）。
两层重试统一预算（批复七·末）：条目失败预算 × HTTP 层重试次数 ≤
MAX_STAGE_REQUEST_BUDGET，构造期校验，防重试放大。

并发（批复八）：默认保守 max_workers=2（上限 8）；同商品互斥由
claim_item 原子领取保证（BEGIN IMMEDIATE 事务内 pending→running）；
网络/AI 调用一律在写事务外（store 全短事务）；同商品重复完成事件幂等
（add_assignment 单活链 + 终态 set 幂等）；AI 决策经引擎锁串行（注入的
ai_decide 不必线程安全）。本地编排，不引入分布式 Worker。

持久化（批复五）：全部经 store v3 既有方法 + 三张既有表（pt_task/
pt_task_item/pt_candidate/pt_assignment/pt_verification），不新建表；
env（mock|live）贯穿每笔写入，Mock/Live 任务互不可见；来源可追溯
（task_id/source/idem_key）；历史不被静默覆盖（分配追加式、验证追加式）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Iterable

from pt.candidates import retrieve_candidates
from pt.material import extract_material
from pt.matcher import match_product
from pt.schema import (
    OUT_FORBIDDEN, OUT_HASH_MISMATCH, OUT_NO_LINK, OUT_OK, OUT_UNAVAILABLE,
    analyze_schema, attribute_gap_report, ensure_schema,
)
from pt.verifier import (
    S_FORBIDDEN, S_UNAVAILABLE, save_browse_node_verification,
    save_pt_verification, verify_browse_node, verify_product_type,
)
from pt.store import ACTIVE_TASK_PHASES

_LOG = logging.getLogger("pt.tasks")

# ---- 常量（与商品无关；批复七/八的上限集中在此） ----
DEFAULT_MAX_WORKERS = 2          # 批复八：默认保守
MAX_WORKERS_CAP = 8              # 可配并发上限帽
DEFAULT_ITEM_MAX_ATTEMPTS = 2    # 条目失败预算（attempt=失败次数）
MAX_STAGE_REQUEST_BUDGET = 12    # 两层重试统一预算（条目×HTTP ≤ 上限）
BN_NODE_CAP = 2                  # 每商品最多验证的 BN 节点数
FINALIZE_QUIESCE_TRIES = 50      # 双 run 竞态收尾静默等待轮数
FINALIZE_QUIESCE_SLEEP = 0.1

# error_kind 分类（批复七）
RETRYABLE_KINDS = frozenset({
    "rate_limit", "network", "server_error", "timeout", "unavailable",
    "schema_unavailable", "schema_hash_mismatch", "storage",
})
AUTH_KINDS = frozenset({"auth", "forbidden"})
# needs_review 类（证据留全、停止自动决策）
REVIEW_KINDS = frozenset({
    "material_missing", "no_candidates", "all_candidates_unsuitable",
    "review", "pt_not_found", "semantic_failed", "semantic_insufficient",
    "bn_no_nodes", "bn_insufficient", "schema_no_link", "schema_error",
})

# 任务层可直接操作的相位集合（store.ACTIVE_TASK_PHASES 的再导出视图）
RESUMABLE_PHASES = ("created", "paused", "interrupted")


class TaskStateError(Exception):
    """任务相位不允许此操作（如对终态任务 run/resume）。"""


@dataclass
class TaskProgress:
    """结构化任务状态（批复九：进度分子=已结束本轮处理的商品数）。"""
    task_id: str
    store_id: str
    marketplace_id: str
    env: str
    phase: str
    total: int
    pending: int
    running: int
    completed: int
    needs_review: int
    retryable_failed: int
    permanent_failed: int
    cancelled: int
    progress_percent: float
    current_stage: str | None      # 在途条目所处阶段（多在途取字典序首个）
    error: str | None
    created_at: int
    updated_at: int
    finished_at: int | None


@dataclass
class ItemResult:
    """逐商品结构化结果（批复九：全部证据可回溯、来源诚实）。"""
    pid: str
    sku: str | None
    state: str
    stage: str | None
    attempt: int
    error_kind: str | None
    error: str | None
    result_ref: str | None
    ai_chosen_pt: str | None       # 当前分配（模块 4 选择+模块 5 验证后落）
    confidence: float | None
    official_candidates: list[dict] = field(default_factory=list)
    pt_verification: dict | None = None      # 最新 pt_exists 行
    semantic_verification: dict | None = None
    bn_verifications: list[dict] = field(default_factory=list)
    attribute_verification: dict | None = None   # 缺口结论（≠可刊登）
    schema_status: str | None = None
    verified_against: str | None = None      # mock | live（诚实来源）


class TaskEngine:
    """批量分类任务编排器（依赖全注入，无隐藏全局）。

    参数：
      store        pt.store.PtStore（模块 1，v3）
      client       pt.spapi_client.SpApiClient（模块 2，env 与任务一致）
      ai_decide    AI 决策器（模块 4 契约；引擎内串行调用）
      load_product pid -> (record, understanding) | (None, None)
                   商品资料装载器（桌面版数据侧提供；无此商品返回 None）
      cache_root   Schema 缓存根目录（模块 6）
      seller_id    卖家上下文（Schema 缓存键隔离用）
      max_workers  并发商品数（默认 2，上限 8）
      item_max_attempts  条目失败预算（retryable_failed 计数上限）
      http_retry_attempts HTTP 层每请求重试次数（须与 client RetryConfig
                   一致；参与两层预算校验）
    """

    def __init__(self, *, store, client, ai_decide: Callable[[dict], dict],
                 load_product: Callable[[str], tuple],
                 cache_root: str, seller_id: str = "",
                 max_workers: int = DEFAULT_MAX_WORKERS,
                 item_max_attempts: int = DEFAULT_ITEM_MAX_ATTEMPTS,
                 http_retry_attempts: int = 1):
        mw = int(max_workers)
        if mw < 1 or mw > MAX_WORKERS_CAP:
            raise ValueError(
                f"max_workers 须在 1..{MAX_WORKERS_CAP}（批复八：默认保守）")
        ima = int(item_max_attempts)
        hra = max(1, int(http_retry_attempts))
        if ima < 1:
            raise ValueError("item_max_attempts 至少为 1")
        if ima * hra > MAX_STAGE_REQUEST_BUDGET:
            raise ValueError(
                f"两层重试预算超限：条目 {ima} × HTTP {hra} = {ima * hra} > "
                f"{MAX_STAGE_REQUEST_BUDGET}（批复七：统一预算防放大）")
        self._store = store
        self._client = client
        self._ai_decide = ai_decide
        self._load_product_fn = load_product
        self._cache_root = str(cache_root)
        self._seller_id = str(seller_id or "")
        self._max_workers = mw
        self._item_max_attempts = ima
        self._ai_lock = threading.Lock()   # 注入的 AI 决策器无需线程安全

    # ------------------------------------------------------------------
    # 生命周期 API
    # ------------------------------------------------------------------

    def create_task(self, *, kind: str, store_id: str, marketplace_id: str,
                    pids: Iterable[str], env: str = "live", source: str = "",
                    idem_key: str | None = None,
                    task_id: str | None = None) -> dict:
        """创建任务（三种入口=三种 kind+source，编排层不区分特例）。
        幂等/重叠守卫由 store.create_task 保证（批复四）。"""
        tid = task_id or f"pt-{uuid.uuid4().hex[:12]}"
        return self._store.create_task(
            tid, kind, store_id, marketplace_id, pids, env=env,
            source=source, idem_key=idem_key)

    def resume(self, task_id: str) -> bool:
        """恢复/起跑：created|paused|interrupted → running（CAS）。"""
        return self._store.cas_task_phase(task_id, RESUMABLE_PHASES, "running")

    def request_pause(self, task_id: str) -> bool:
        """请求暂停：在途到安全检查点后 paused（不领新、不强杀）。
        created/无在途时直接落 paused（无 run 循环可等）。"""
        ok = self._store.cas_task_phase(
            task_id, ("created", "running"), "pausing")
        if ok and self._store.task_item_counts(task_id)["running"] == 0:
            self._store.cas_task_phase(task_id, ("pausing",), "paused")
        return ok

    def request_cancel(self, task_id: str) -> bool:
        """请求取消：任何活跃相位可请求；终态条目保留、审计不删。
        无在途时直接收尾（未执行条目记 cancelled）。"""
        ok = self._store.cas_task_phase(
            task_id, ACTIVE_TASK_PHASES, "cancelling")
        if ok and self._store.task_item_counts(task_id)["running"] == 0:
            for it in self._store.task_items(task_id,
                                             states=["pending"]):
                self._store.set_item_state(
                    task_id, it["pid"], "cancelled", error="用户取消",
                    error_kind="cancelled")
            self._store.cas_task_phase(
                task_id, ("cancelling",), "cancelled", finished=True)
        return ok

    def recover(self) -> list[str]:
        """进程异常退出后的启动恢复（批复六）：残留 running/pausing/
        cancelling 任务→interrupted、其中 running 条目→pending。
        恢复后逐任务 resume()+run() 续跑。"""
        return self._store.recover_interrupted()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self, task_id: str) -> dict:
        """同步推进一个任务到当前可达的稳定相位，返回任务行。

        - created 自动起跑；running=续跑（同任务并发 run 由原子领取与
          CAS 收尾保证不重复处理、不矛盾收尾）；
        - 其余相位（pausing/paused/cancelling/终态/interrupted）直接
          TaskStateError——先 resume()/request_* 再说；
        - 暂停/取消/授权失效只经检查点生效：绝不强杀线程、绝不打断
          SQLite 写事务（批复六）。
        """
        task = self._store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        # Mock/Live 互不污染的运行时硬闸（批复四）：任务 env 必须与
        # 客户端可信环境一致——mock 客户端绝不驱动 live 任务，反之亦然。
        cenv = getattr(self._client, "env_mode", task["env"])
        if task["env"] != cenv:
            raise TaskStateError(
                f"任务 {task_id} env={task['env']} 与客户端 env={cenv} "
                f"不一致——Mock/Live 任务必须各用各的客户端")
        if not self._store.cas_task_phase(
                task_id, ("created", "running"), "running"):
            raise TaskStateError(
                f"任务 {task_id} 处于 {task['phase']}，不可运行"
                f"（resume 后再 run；暂停/取消由 request_* 发起）")
        ctx = _Ctx(task_id=task_id, store_id=task["store_id"],
                   marketplace_id=task["marketplace_id"], env=task["env"])
        auth_evt = threading.Event()
        inflight: set = set()
        pool = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix=f"pt-{str(task_id)[:16]}")
        try:
            while True:
                phase = self._phase_of(task_id)
                stop = phase != "running" or auth_evt.is_set()
                if not stop:
                    # 补位领取（store 层原子：pending→running，相位门控）
                    while len(inflight) < self._max_workers:
                        try:
                            pid = self._store.claim_item(task_id)
                        except sqlite3.Error as e:
                            # 领取层存储故障=任务级不可恢复（批复二）
                            _LOG.exception("claim_item 存储故障，任务失败")
                            self._store.set_task_phase(
                                task_id, "failed",
                                error=f"领取失败（存储）: {e}")
                            phase = "failed"
                            break
                        if pid is None:
                            break
                        inflight.add(pool.submit(
                            self._process_item_safe, ctx, pid, auth_evt))
                    if phase == "failed":
                        break
                if inflight:
                    # 等任一在途到检查点/终态（暂停不强杀：等它自己到边界）
                    _, inflight = self._wait_some(inflight)
                    continue
                if stop:
                    break
                # 无在途：失败预算内的条目拉回 pending 继续本轮
                requeued = self._store.requeue_retryable(
                    task_id, self._item_max_attempts)
                counts = self._store.task_item_counts(task_id)
                if counts["pending"] == 0:
                    break                       # 全部条目已到终态
                if requeued == 0:
                    time.sleep(0.01)            # 有 pending 领不出=相位瞬变
        finally:
            pool.shutdown(wait=True)
        return self._finalize(task_id, auth_evt)

    # ------------------------------------------------------------------
    # 进度与结果（批复九：真源=逐商品状态，不依赖内存计数器）
    # ------------------------------------------------------------------

    def task_progress(self, task_id: str) -> TaskProgress | None:
        task = self._store.get_task(task_id)
        if task is None:
            return None
        counts = self._store.task_item_counts(task_id)
        total = counts["total"]
        finished = sum(counts[s] for s in (
            "completed", "needs_review", "retryable_failed",
            "permanent_failed", "cancelled"))
        running_items = self._store.task_items(task_id, states=["running"])
        stages = sorted({i["stage"] for i in running_items if i["stage"]})
        return TaskProgress(
            task_id=task_id, store_id=task["store_id"],
            marketplace_id=task["marketplace_id"], env=task["env"],
            phase=task["phase"], total=total, pending=counts["pending"],
            running=counts["running"], completed=counts["completed"],
            needs_review=counts["needs_review"],
            retryable_failed=counts["retryable_failed"],
            permanent_failed=counts["permanent_failed"],
            cancelled=counts["cancelled"],
            progress_percent=round(finished * 100.0 / total, 1) if total
            else 100.0,
            current_stage=stages[0] if stages else None,
            error=task["error"], created_at=task["created_at"],
            updated_at=task["updated_at"], finished_at=task["finished_at"])

    def item_results(self, task_id: str) -> list[ItemResult]:
        """逐商品结构化结果：候选证据/AI 取舍/官方验证/BN/Schema/缺口
        全部从 store 回读（审计真源），SKU 经 load_product 只读补全。"""
        task = self._store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        tid, sid, mid, env = (task_id, task["store_id"],
                              task["marketplace_id"], task["env"])
        out: list[ItemResult] = []
        for it in self._store.task_items(tid):
            pid = it["pid"]
            cands = self._store.candidates(sid, mid, pid, env=env)
            vs = self._store.verifications(sid, mid, pid)
            ptv = next((v for v in reversed(vs)
                        if v["check_kind"] == "pt_exists"), None)
            semv = next((v for v in reversed(vs)
                         if v["check_kind"] == "semantic"), None)
            bnvs = [v for v in vs if v["check_kind"] == "browse_node"]
            attr = next((v for v in reversed(vs)
                         if v["check_kind"] == "attribute"), None)
            asg = self._store.current_assignment(sid, mid, pid, env=env)
            schema_status = None
            if asg and asg.get("schema_cache_key"):
                meta = self._store.get_schema_meta(asg["schema_cache_key"])
                schema_status = (meta or {}).get("status")
            out.append(ItemResult(
                pid=pid, sku=self._sku_of(pid), state=it["state"],
                stage=it["stage"], attempt=it["attempt"],
                error_kind=it["error_kind"], error=it["error"],
                result_ref=it["result_ref"],
                ai_chosen_pt=(asg or {}).get("pt_name"),
                confidence=(asg or {}).get("confidence"),
                official_candidates=cands, pt_verification=ptv,
                semantic_verification=semv, bn_verifications=bnvs,
                attribute_verification=attr, schema_status=schema_status,
                verified_against=env))
        return out

    # ------------------------------------------------------------------
    # 内部：逐商品流水线
    # ------------------------------------------------------------------

    def _process_item_safe(self, ctx: "_Ctx", pid: str,
                           auth_evt: threading.Event) -> None:
        """线程入口：任何异常都不带崩线程池，分类入库（批复三）。"""
        try:
            self._pipeline(ctx, pid, auth_evt)
        except Exception as e:  # noqa: BLE001 —— 兜底，绝不带崩批量
            kind = "storage" if isinstance(e, sqlite3.Error) else "internal"
            state = "retryable_failed" if kind == "storage" \
                else "permanent_failed"
            _LOG.exception("条目处理异常 pid=%s kind=%s", pid, kind)
            try:
                self._store.set_item_state(
                    ctx.task_id, pid, state,
                    error=f"{type(e).__name__}: {e}"[:300], error_kind=kind)
            except Exception:  # noqa: BLE001
                _LOG.exception("异常状态入库也失败 pid=%s（留 running，"
                               "recover 兜底）", pid)

    def _pipeline(self, ctx: "_Ctx", pid: str,
                  auth_evt: threading.Event) -> None:
        t = ctx.task_id
        # ---- 阶段 material（claim 已置 stage=material） ----
        loaded = self._load_product_fn(pid)
        record = understanding = None
        if loaded is not None:
            record, understanding = loaded[0], loaded[1]
        if record is None:
            self._store.set_item_state(
                t, pid, "needs_review",
                error="商品资料缺失（load_product 无此商品）——记录缺口，"
                      "必要时人工补资料", error_kind="material_missing")
            return
        material = extract_material(pid, record, understanding)

        # ---- 阶段 candidate_search ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._store.set_item_stage(t, pid, "candidate_search")
        cands = retrieve_candidates(
            self._client, material, marketplace_id=ctx.marketplace_id)
        # 候选证据先落库（审计+复核可见；历史不被静默覆盖）
        self._persist_candidates(ctx, pid, cands)
        blocker = self._strategy_blocker(cands)
        if blocker is not None:
            kind, msg = blocker
            if kind in AUTH_KINDS:
                auth_evt.set()
                self._fail_retryable(t, pid, kind, msg)
            elif kind in RETRYABLE_KINDS:
                self._fail_retryable(t, pid, kind, msg)
            else:                      # 参数/环境类错误：不自动重试
                self._store.set_item_state(
                    t, pid, "permanent_failed", error=msg, error_kind=kind)
            return

        # ---- 阶段 ai_matching ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._store.set_item_stage(t, pid, "ai_matching")
        with self._ai_lock:
            mr = match_product(material, cands, self._ai_decide,
                               marketplace_id=ctx.marketplace_id)
        if mr.status != "matched" or mr.needs_review or not mr.chosen:
            kind = {
                "no_candidates": "no_candidates",
                "all_candidates_unsuitable": "all_candidates_unsuitable",
            }.get(mr.status, "review")
            self._store.set_item_state(
                t, pid, "needs_review",
                error="；".join(mr.review_reasons)[:300] or "匹配未放行",
                error_kind=kind)
            return
        chosen = mr.chosen

        # ---- 阶段 pt_verification（AI 匹配成功≠官方验证） ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._store.set_item_stage(t, pid, "pt_verification")
        cand_sources = {c.pt_name: sorted(c.sources)
                        for c in cands.candidates}
        pv = verify_product_type(
            self._client, material, chosen,
            marketplace_id=ctx.marketplace_id,
            candidate_sources=cand_sources)
        save_pt_verification(self._store, pv, store_id=ctx.store_id, pid=pid)
        ex = pv.exists
        if ex.status == S_FORBIDDEN:
            auth_evt.set()
            self._fail_retryable(t, pid, "auth",
                                  "授权不足（401/403）——任务将自动暂停")
            return
        if ex.status == S_UNAVAILABLE:
            self._fail_retryable(t, pid, "unavailable",
                                  f"官方定义接口暂不可用：{ex.detail}")
            return
        if ex.status == "error":
            self._store.set_item_state(
                t, pid, "permanent_failed",
                error=f"官方定义请求参数/环境错误：{ex.detail}",
                error_kind="bad_params")
            return
        if ex.status == "failed":
            self._store.set_item_state(
                t, pid, "needs_review",
                error="官方 404：AI 所选 PT 该站点不存在——不保存非法选择",
                error_kind="pt_not_found")
            return
        sem = pv.semantic
        if sem.status in ("failed", "insufficient", "error", "skipped"):
            self._store.set_item_state(
                t, pid, "needs_review",
                error=f"PT 语义适配未通过（{sem.status}）："
                      f"{sem.detail.get('note', '')}",
                error_kind=f"semantic_{sem.status}")
            return
        if sem.status == S_UNAVAILABLE:
            self._fail_retryable(t, pid, "unavailable",
                                  f"语义佐证接口暂不可用：{sem.detail}")
            return

        # ---- 阶段 browse_node_verification（PT 存在≠BN 适配） ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._store.set_item_stage(t, pid, "browse_node_verification")
        nodes = self._bn_nodes_for(cands, chosen)
        if not nodes:
            self._store.set_item_state(
                t, pid, "needs_review",
                error="PT 已验证但候选证据无 Browse Node（classification_ids"
                      " 缺失）——交人工选节点", error_kind="bn_no_nodes")
            return
        best_node: str | None = None
        for nid in nodes:
            if self._checkpoint(t, pid, auth_evt):
                return
            bnv = verify_browse_node(
                self._client, material, nid,
                marketplace_id=ctx.marketplace_id)
            save_browse_node_verification(
                self._store, bnv, store_id=ctx.store_id, pid=pid)
            if bnv.existence.status == S_FORBIDDEN:
                auth_evt.set()
                self._fail_retryable(t, pid, "auth",
                                     "目录验证授权不足（401/403）")
                return
            if bnv.existence.status == S_UNAVAILABLE:
                self._fail_retryable(t, pid, "unavailable",
                                     f"目录接口暂不可用（节点 {nid}）")
                return
            if bnv.existence.status == "passed":
                best_node = nid
                break
        if best_node is None:
            self._store.set_item_state(
                t, pid, "needs_review",
                error=f"BN 证据不足（{len(nodes)} 个候选节点无一确认）——"
                      "绝不猜节点，交人工", error_kind="bn_insufficient")
            return

        # ---- 阶段 schema_analysis（Schema 成功≠属性齐全） ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._store.set_item_stage(t, pid, "schema_analysis")
        so = ensure_schema(
            self._client, self._store, self._cache_root, pt_name=chosen,
            marketplace_id=ctx.marketplace_id, seller_id=self._seller_id,
            env=ctx.env)
        if so.status == OUT_FORBIDDEN:
            auth_evt.set()
            self._fail_retryable(t, pid, "auth", "Schema 授权不足（401/403）")
            return
        if so.status == OUT_UNAVAILABLE:
            self._fail_retryable(t, pid, "schema_unavailable",
                                  f"Schema 暂不可用：{so.detail}")
            return
        if so.status == OUT_HASH_MISMATCH:
            self._fail_retryable(
                t, pid, "schema_hash_mismatch",
                "官方 checksum 不符（旧缓存保留）——按有界策略重试")
            return
        if so.status == OUT_NO_LINK:
            self._store.set_item_state(
                t, pid, "needs_review",
                error="官方响应缺 schema.link——无 Schema 可下载",
                error_kind="schema_no_link")
            return
        if so.status != OUT_OK:
            detail = so.detail or {}
            if detail.get("kind") == "write_failed":
                self._fail_retryable(t, pid, "storage",
                                     f"Schema 落盘失败：{detail}")
            else:
                self._store.set_item_state(
                    t, pid, "permanent_failed",
                    error=f"Schema 获取被策略拒绝：{detail}",
                    error_kind="schema_error")
            return
        analysis = analyze_schema(so.schema)
        gap = attribute_gap_report(material, analysis, pt_name=chosen,
                                   marketplace_id=ctx.marketplace_id)

        # ---- 阶段 persist_result（缺口报告≠可刊登：incomplete 不拦完成）----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._store.set_item_stage(t, pid, "persist_result")
        # 属性缺口结论入审计链（check_kind=attribute；official=False——
        # 内部报告绝不冒充官方结论；complete_for_review≠可刊登）
        self._store.add_verification(
            store_id=ctx.store_id, marketplace_id=ctx.marketplace_id,
            pid=pid, pt_name=chosen, node_id=best_node,
            check_kind="attribute",
            status="insufficient" if gap.summary == "incomplete" else "passed",
            detail={"source": "internal", "official": False,
                    "summary": gap.summary, "counts": gap.counts,
                    "ai_semantic_pt": gap.ai_semantic_pt,
                    "note": "缺口报告只对照官方 Schema：incomplete≠不可完成，"
                            "complete_for_review≠可刊登"},
            verified_against=ctx.env)
        aid = self._store.add_assignment(
            store_id=ctx.store_id, marketplace_id=ctx.marketplace_id,
            pid=pid, pt_name=chosen, schema_cache_key=so.cache_key,
            chosen_by="ai", confidence=mr.confidence,
            reason="模块4匹配+模块5验证通过（PT+BN 官方证据齐全）；置信度 "
                   f"{mr.confidence}", env=ctx.env)
        self._store.set_item_state(t, pid, "completed",
                                   result_ref=f"assignment:{aid}")

    # ------------------------------------------------------------------
    # 内部：工具
    # ------------------------------------------------------------------

    def _phase_of(self, task_id: str) -> str | None:
        task = self._store.get_task(task_id)
        return task["phase"] if task else None

    def _checkpoint(self, task_id: str, pid: str,
                    auth_evt: threading.Event) -> bool:
        """阶段边界安全检查点（批复六）：pausing/cancelling/授权失效 →
        停止本商品后续一切处理。返回 True=已让位（调用方立即 return）。"""
        if auth_evt.is_set():
            # 授权失效：本商品回 pending（任务将自动暂停，重授权后续跑）
            self._store.set_item_state(task_id, pid, "pending")
            return True
        phase = self._phase_of(task_id)
        if phase == "pausing":
            self._store.set_item_state(task_id, pid, "pending")
            return True
        if phase in ("cancelling", "cancelled"):
            self._store.set_item_state(
                task_id, pid, "cancelled", error="用户取消",
                error_kind="cancelled")
            return True
        return False

    def _fail_retryable(self, task_id: str, pid: str, kind: str,
                        msg: str) -> None:
        """可恢复失败：attempt+1（store 负责）；预算内立即回 pending
        本轮续跑，预算耗尽停驻 retryable_failed 由收尾推导 with_issues。"""
        self._store.set_item_state(task_id, pid, "retryable_failed",
                                   error=str(msg)[:300], error_kind=kind)
        self._store.requeue_retryable(task_id, self._item_max_attempts)

    def _persist_candidates(self, ctx: "_Ctx", pid: str, cands) -> None:
        rows = []
        for c in cands.candidates:
            for strat, ev in (c.sources or {}).items():
                rows.append({"strategy": strat, "pt_name": c.pt_name,
                             "display_name": c.display_name,
                             "score": c.score, "evidence": ev})
        if rows:
            self._store.upsert_candidates(
                ctx.store_id, ctx.marketplace_id, pid, rows,
                task_id=ctx.task_id, env=ctx.env)

    @staticmethod
    def _strategy_blocker(cands):
        """三策略报告 → 阻断分类（批复七）：auth/forbidden→授权；全策略
        网络类故障→可重试；有任一策略出候选=不阻断（证据链够跑匹配）。"""
        reports = list((cands.reports or {}).values())
        if not reports:
            return None
        for r in reports:
            if r.status == "error" and r.kind in AUTH_KINDS:
                return r.kind, f"候选检索授权不足（{r.strategy}）: {r.detail}"
        # 全军覆没（无一策略出候选）才阻断；有任一策略出候选=证据够跑匹配
        if all(r.status in ("error", "skipped") for r in reports) \
                and any(r.status == "error" for r in reports):
            r0 = next(r for r in reports if r.status == "error")
            kinds = {r.kind for r in reports if r.status == "error"}
            if kinds <= RETRYABLE_KINDS:
                return (r0.kind or "network",
                        f"全部检索策略故障（{sorted(kinds)}）: {r0.detail}")
            if kinds <= {"bad_params", "environment"}:
                return ("bad_params",
                        f"检索策略参数/环境错误（{sorted(kinds)}）: "
                        f"{r0.detail}")
            return (r0.kind or "internal",
                    f"检索策略未知故障（{sorted(kinds)}）: {r0.detail}")
        return None

    @staticmethod
    def _bn_nodes_for(cands, chosen: str) -> list[str]:
        """BN 节点唯一来源=候选证据里的官方 classification_ids（S3），
        去重、上限 BN_NODE_CAP——绝不凭空猜节点。"""
        out: list[str] = []
        for c in cands.candidates:
            if c.pt_name != chosen:
                continue
            for strat in sorted(c.sources or {}):
                ev = c.sources[strat] or {}
                for nid in ev.get("classification_ids") or []:
                    nid = str(nid).strip()
                    if nid and nid not in out:
                        out.append(nid)
        return out[:BN_NODE_CAP]

    def _sku_of(self, pid: str) -> str | None:
        try:
            loaded = self._load_product_fn(pid)
        except Exception:  # noqa: BLE001 —— 只读补全，失败即 None
            return None
        if not loaded or loaded[0] is None:
            return None
        try:
            m = extract_material(pid, loaded[0], loaded[1])
            return m.sku or None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _wait_some(futures: set) -> tuple[list, set]:
        done, pending = wait(futures, return_when=FIRST_COMPLETED)
        for f in done:
            exc = f.exception()
            if exc is not None:      # _process_item_safe 已兜底，此为再保险
                _LOG.error("worker 未捕获异常: %r", exc)
        return list(done), set(pending)

    def _finalize(self, task_id: str,
                  auth_evt: threading.Event) -> dict:
        """收尾相位推导（批复二/六/九）：全部以条目真源计数。"""
        # 双 run 竞态：此刻仍有他方 run 的在途/待跑条目时不抢收尾，
        # 静默等待至全部结束（领取原子性已保证不重复处理）。
        for _ in range(FINALIZE_QUIESCE_TRIES):
            counts = self._store.task_item_counts(task_id)
            if not counts["running"] and not counts["pending"]:
                break
            time.sleep(FINALIZE_QUIESCE_SLEEP)
        task = self._store.get_task(task_id)
        phase = task["phase"]
        if auth_evt.is_set() and phase == "running":
            self._store.cas_task_phase(
                task_id, ("running",), "pausing",
                error="授权失效（401/403）——已停止相关请求，自动暂停等待"
                      "处理（重授权后 resume 续跑）")
            phase = self._phase_of(task_id)
        if phase == "pausing":
            # 在途已全到检查点；残留 running（异常路径）置回 pending
            for it in self._store.task_items(task_id, states=["running"]):
                self._store.set_item_state(task_id, it["pid"], "pending")
            cur = self._store.get_task(task_id)
            self._store.cas_task_phase(
                task_id, ("pausing",), "paused", error=cur["error"])
        elif phase == "cancelling":
            # 未执行条目记 cancelled；终态条目（含已完成结果）原样保留
            for it in self._store.task_items(
                    task_id, states=["pending", "running"]):
                self._store.set_item_state(
                    task_id, it["pid"], "cancelled", error="用户取消",
                    error_kind="cancelled")
            self._store.cas_task_phase(
                task_id, ("cancelling",), "cancelled", finished=True)
        elif phase == "running":
            counts = self._store.task_item_counts(task_id)
            issues = (counts["needs_review"] + counts["retryable_failed"]
                      + counts["permanent_failed"] + counts["cancelled"])
            final = "completed_with_issues" if issues else "completed"
            self._store.cas_task_phase(task_id, ("running",), final,
                                       finished=True)
        return self._store.get_task(task_id)


@dataclass
class _Ctx:
    """一次 run 的任务上下文（env 贯穿每笔写入）。"""
    task_id: str
    store_id: str
    marketplace_id: str
    env: str

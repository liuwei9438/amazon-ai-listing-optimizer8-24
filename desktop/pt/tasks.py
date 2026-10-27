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

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable

from pt.candidates import (
    Candidate, CandidatesResult, bn_missing_reason, retrieve_candidates,
)
from pt.material import extract_material
from pt.matcher import match_product
from pt.schema import (
    OUT_FORBIDDEN, OUT_HASH_MISMATCH, OUT_NO_LINK, OUT_OK, OUT_UNAVAILABLE,
    analyze_schema, attribute_gap_report, ensure_schema,
)
from pt.verifier import (
    BrowseNodeVerification, CheckResult, PtVerification, S_FORBIDDEN,
    S_UNAVAILABLE, save_browse_node_verification,
    save_pt_verification, verify_browse_node, verify_product_type,
)
from pt.store import ACTIVE_TASK_PHASES

_LOG = logging.getLogger("pt.tasks")

# ---- 常量（与商品无关；批复七/八的上限集中在此） ----
DEFAULT_MAX_WORKERS = 3          # D1.12.2：批量默认 3（AI 并发上限同 3；
                                 # 批复八的 1..8 帽与保守语义不变）
MAX_WORKERS_CAP = 8              # 可配并发上限帽
DEFAULT_AI_CONCURRENCY = 3       # D1.12.2：AI 决策并发初始上限（任务书④）
AI_CONCURRENCY_CAP = 5           # AI 并发硬帽（绝不无限提并发）
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



# ---- D1.12.2（任务书④）：SP-API 各接口独立限流（保护性上限） ----
# 按「方法名 → 接口类」分桶：PTD 检索 / PTD 定义 / 目录检索 / Schema 下载
# 各自独立令牌桶（rate rps + burst 突发）；默认值只防失控循环，不压制
# 正常批量节奏；429 的 Retry-After 语义仍归客户端 RetryConfig，互不越权。
_SPAPI_OP_CLASS = {
    "search_product_types": "ptd_search",
    "get_product_type": "ptd_get",
    "search_catalog_items": "catalog_search",
    "get_catalog_item": "catalog_get",
    "fetch_schema": "schema_fetch",
}


class _TokenBucket:
    """极简令牌桶（单接口类一个；锁内短临界区，不做持久化）。"""

    def __init__(self, rate: float, burst: float):
        self.rate = max(float(rate), 0.1)
        self.burst = max(float(burst), 1.0)
        self.tokens = self.burst
        self.t = time.monotonic()
        self.cv = threading.Condition()

    def acquire(self) -> None:
        with self.cv:
            while True:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens
                                  + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                self.cv.wait(timeout=0.05)


class _Inflight:
    """singleflight 在途槽：首个线程算，其余等结果（任务书④ PTD/Schema
    在途去重）。失败不广播——等待者各自重算（行为与无去重时一致）。"""

    __slots__ = ("event", "result", "done")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.done = False


class _RateLimitedClient:
    """透传包装：只拦 SP-API 只读动词计数+限流，其余属性/方法原样
    转发（env_mode 等元数据读取不受影响；测试假客户端同样适用）。"""

    def __init__(self, inner, *, rate: float, burst: float, perf: dict):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_buckets", {
            cls: _TokenBucket(rate, burst)
            for cls in set(_SPAPI_OP_CLASS.values()) | {"default"}
        })
        object.__setattr__(self, "_perf", perf)
        object.__setattr__(self, "_perf_lock", threading.Lock())

    def __getattr__(self, name):
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if not callable(attr) or name not in _SPAPI_OP_CLASS:
            return attr
        cls = _SPAPI_OP_CLASS[name]

        def wrapped(*a, **kw):
            object.__getattribute__(self, "_buckets")[cls].acquire()
            perf = object.__getattribute__(self, "_perf")
            with object.__getattribute__(self, "_perf_lock"):
                sp = perf.setdefault("spapi", {})
                sp[cls] = sp.get(cls, 0) + 1
            return attr(*a, **kw)
        return wrapped


def _material_fp(material) -> str:
    """资料指纹（任务书④：阶段复用须核对输入指纹）。覆盖参与 AI
    决策与候选检索的全部本体字段；指纹变=上游资料已改，已有阶段
    结论不得复用（宁可重跑，绝不拿旧结论配新资料）。"""
    core = {
        "pid": material.pid, "title": material.title,
        "title_raw": material.title_raw, "bullets": material.bullets,
        "description": material.description, "model": material.model,
        "product_identity": material.product_identity,
        "identity_source": material.identity_source,
        "compat": sorted(material.compat_values()),
        "language_guess": material.language_guess,
        "degraded": bool(material.degraded),
    }
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True)
        .encode("utf-8")).hexdigest()


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
    # D1.12.2 六态可见（条目行级，后续阶段失败也保留前序结论）：
    ai_pt: str | None = None                 # AI 已分类（所选 PT）
    ai_confidence: float | None = None
    schema_ok: bool = False                  # Schema 已获取（缓存命中含）
    attr_status: str | None = None           # 属性已校验（缺口结论）


class TaskEngine:
    """批量分类任务编排器（依赖全注入，无隐藏全局）。

    参数：
      store        pt.store.PtStore（模块 1，v3）
      client       pt.spapi_client.SpApiClient（模块 2，env 与任务一致）
      ai_decide    AI 决策器（模块 4 契约；引擎内串行调用）
      ai_semantic  P2 受限语义裁决器（可选；只在字面 0 分死局触发，
                   绝不替代 PTD/BN 官方验证）
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
                 http_retry_attempts: int = 1,
                 ai_semantic: Callable[[dict], dict] | None = None,
                 ai_concurrency: int = DEFAULT_AI_CONCURRENCY,
                 spapi_rate: float = 10.0, spapi_burst: float = 12.0):
        mw = int(max_workers)
        if mw < 1 or mw > MAX_WORKERS_CAP:
            raise ValueError(
                f"max_workers 须在 1..{MAX_WORKERS_CAP}（批复八：默认保守）")
        ac = int(ai_concurrency)
        if ac < 1 or ac > AI_CONCURRENCY_CAP:
            raise ValueError(
                f"ai_concurrency 须在 1..{AI_CONCURRENCY_CAP}"
                "（任务书④：AI 并发有界，绝不无限提）")
        ima = int(item_max_attempts)
        hra = max(1, int(http_retry_attempts))
        if ima < 1:
            raise ValueError("item_max_attempts 至少为 1")
        if ima * hra > MAX_STAGE_REQUEST_BUDGET:
            raise ValueError(
                f"两层重试预算超限：条目 {ima} × HTTP {hra} = {ima * hra} > "
                f"{MAX_STAGE_REQUEST_BUDGET}（批复七：统一预算防放大）")
        self._store = store
        # D1.12.2（任务书④）：阶段耗时/请求计数/跳过统计（审计用，
        # 不参与任何门控；收尾落 pt_task.perf_json）。
        self._perf = {"stages": {}, "spapi": {}, "ai_stage_runs": 0,
                      "stage_skips": {}}
        self._perf_lock = threading.Lock()
        self._client = client
        self._ai_decide = ai_decide
        # P2 受限语义裁决（可选；None=引擎在字面 0 分死局时也不问 GPT，
        # 行为与修复前一致）。与 ai_decide 共用 _ai_sem 限并发。
        self._ai_semantic = ai_semantic
        self._load_product_fn = load_product
        self._cache_root = str(cache_root)
        self._seller_id = str(seller_id or "")
        self._max_workers = mw
        self._item_max_attempts = ima
        # D1.12.2（任务书④）：AI 并发由信号量限界（默认 3）；注入的
        # AI 决策器无需线程安全——并发上限本身就是它的保护层。
        self._ai_sem = threading.BoundedSemaphore(ac)
        # SP-API 各接口独立限流（保护性上限：默认 10 rps/突发 12——
        # 只防失控循环，不压制正常批量节奏；429 语义仍由客户端
        # Retry-After 处理，两者互不越权）。
        self._client = _RateLimitedClient(
            client, rate=float(spapi_rate), burst=float(spapi_burst),
            perf=self._perf)
        # 批内去重（singleflight 结果复用，任务书④）：同 PT 的 PTD
        # 验证/同节点的目录验证在本轮内只做一次，其余条目复用结论。
        self._ptd_memo: dict[tuple, object] = {}
        self._bn_memo: dict[tuple, object] = {}
        self._schema_memo: dict[tuple, object] = {}
        self._ptd_inflight: dict[tuple, _Inflight] = {}
        self._bn_inflight: dict[tuple, _Inflight] = {}
        self._schema_inflight: dict[tuple, _Inflight] = {}
        self._memo_lock = threading.Lock()
        self._stage_t0: dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # 生命周期 API
    # ------------------------------------------------------------------

    def create_task(self, *, kind: str, store_id: str, marketplace_id: str,
                    pids: Iterable[str], env: str = "live", source: str = "",
                    idem_key: str | None = None, new_operation: bool = False,
                    task_id: str | None = None) -> dict:
        """创建任务（三种入口=三种 kind+source，编排层不区分特例）。
        幂等/重叠守卫由 store.create_task 保证（批复四）；终态同键
        拒绝复用/显式新一轮语义同在 store 层（S3-B2-F）。"""
        tid = task_id or f"pt-{uuid.uuid4().hex[:12]}"
        return self._store.create_task(
            tid, kind, store_id, marketplace_id, pids, env=env,
            source=source, idem_key=idem_key,
            new_operation=new_operation)

    def resume(self, task_id: str) -> bool:
        """恢复/起跑：created|paused|interrupted → running（CAS）。
        成功即清掉挂起的「继续」意图（意图已达成；防崩溃残留的陈旧
        意图在下个暂停周期被误消费成意外自动续跑）。"""
        ok = self._store.cas_task_phase(task_id, RESUMABLE_PHASES, "running")
        if ok:
            self._store.set_resume_pending(task_id, False)
        return ok

    def request_pause(self, task_id: str) -> bool:
        """请求暂停：在途到安全检查点后 paused（不领新、不强杀）。
        created/无在途时直接落 paused（无 run 循环可等）。
        暂停请求一律压掉挂起的「继续」意图（后到的指令说了算）。"""
        self._store.set_resume_pending(task_id, False)
        ok = self._store.cas_task_phase(
            task_id, ("created", "running"), "pausing")
        if ok and self._store.task_item_counts(task_id)["running"] == 0:
            self._store.cas_task_phase(task_id, ("pausing",), "paused")
        return ok

    def request_cancel(self, task_id: str) -> bool:
        """请求取消：任何活跃相位可请求；终态条目保留、审计不删。
        无在途时直接收尾（未执行条目记 cancelled）。取消同样压掉
        挂起的「继续」意图（取消是终局指令）。"""
        self._store.set_resume_pending(task_id, False)
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
                   marketplace_id=task["marketplace_id"], env=task["env"],
                   created_at=int(task.get("created_at") or 0))
        auth_evt = threading.Event()
        while True:
            # 外层循环：暂停落定时消费到持久化的「继续」意图（相位已回
            # running）就重建线程池直接续跑；其余相位由 _finalize 如实
            # 推导后经下方 return 返回。
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
                                # 领取层存储=任务级不可恢复（批复二）
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
                        break                   # 全部条目已到终态
                    if requeued == 0:
                        time.sleep(0.01)        # 有 pending 领不出=相位瞬变
            finally:
                pool.shutdown(wait=True)
            task = self._finalize(task_id, auth_evt)
            try:
                self._store.set_task_perf(
                    task_id, json.dumps(self._perf_snapshot(),
                                        ensure_ascii=False))
            except Exception:  # noqa: BLE001 —— perf 审计绝不影响任务态
                _LOG.exception("perf 落盘失败 task=%s（忽略）", task_id)
            if task["phase"] != "running" or auth_evt.is_set():
                return task
            # running=消费到了挂起的「继续」意图：回到外层循环顶部
            # 重建线程池续跑（无需任何再点击）。

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
                ai_chosen_pt=(asg or {}).get("pt_name")
                             or (it.get("ai_pt") or None),
                confidence=(asg or {}).get("confidence")
                            if (asg or {}).get("confidence") is not None
                            else it.get("ai_confidence"),
                official_candidates=cands, pt_verification=ptv,
                semantic_verification=semv, bn_verifications=bnvs,
                attribute_verification=attr, schema_status=schema_status,
                verified_against=env,
                ai_pt=(it.get("ai_pt") or None),
                ai_confidence=it.get("ai_confidence"),
                schema_ok=bool(it.get("schema_ok")),
                attr_status=(it.get("attr_status") or None)))
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

        # ---- D1.12.2：资料指纹 + 阶段级续跑判定（任务书④⑤）----
        # 同一任务内重试（自动预算内或用户显式重试）时，已成功阶段的
        # 结论不重跑：候选/AI/PTD/BN 各自按「指纹一致 + 已落库结论」
        # 复用；资料变了（指纹不符）=全部重跑，绝不旧结论配新资料。
        fp = _material_fp(material)
        prior = self._store.task_item_row(t, pid) or {}
        resumable = bool(prior.get("material_fp")
                         and prior["material_fp"] == fp)
        if not resumable and prior.get("material_fp") is not None:
            self._perf_skip("resume_reset_material_changed")
        self._store.update_item_progress(t, pid, material_fp=fp)

        # ---- 阶段 candidate_search ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._stage_mark(ctx, pid, "candidate_search")
        cands = self._candidates_for(ctx, pid, material,
                                     reuse=resumable)
        if getattr(cands, "_resumed", False):
            self._perf_skip("candidate_search")
        else:
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
        self._stage_mark(ctx, pid, "ai_matching")
        prior_pt = (prior.get("ai_pt") or "") if resumable else ""
        if prior_pt:
            # AI 已分类（本任务此前轮次结论+指纹一致）：不重问、不重耗
            # Token；置信度取当时落库值，证据链不动。
            self._perf_skip("ai_matching")
            class _Mr:  # noqa: N801 —— 复用路径的最小替身（只填下游用到的字段）
                pass
            mr = _Mr()
            mr.status, mr.needs_review, mr.chosen = "matched", False, prior_pt
            mr.confidence = prior.get("ai_confidence")
            mr.review_reasons = []
            chosen = prior_pt
        else:
            with self._ai_sem:
                mr = match_product(material, cands, self._ai_decide,
                                   marketplace_id=ctx.marketplace_id,
                                   ai_semantic=self._ai_semantic)
            chosen = None
        with self._perf_lock:
            self._perf["ai_stage_runs"] = self._perf.get(
                "ai_stage_runs", 0) + (0 if prior_pt else 1)
        if chosen is None:
            chosen = mr.chosen
        if mr.status != "matched" or mr.needs_review or not mr.chosen:
            kind = {
                "no_candidates": "no_candidates",
                "all_candidates_unsuitable": "all_candidates_unsuitable",
            }.get(mr.status, "review")
            self._store.set_item_state(
                t, pid, "needs_review",
                error="；".join(mr.review_reasons)[:300] or "匹配未放行",
                error_kind=kind)
            # P5：AI 建议（chosen/置信度/候选行）随复核快照落库
            self._save_review_evidence(ctx, t, pid, "ai_matching", kind,
                                       mr.review_reasons, mr=mr)
            return
        # D1.12.2 六态之一「AI 已分类」：后续阶段失败也保留（条目行真源）
        self._store.update_item_progress(t, pid, ai_pt=chosen,
                                         ai_confidence=mr.confidence)

        # ---- 阶段 pt_verification（AI 匹配成功≠官方验证） ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._stage_mark(ctx, pid, "pt_verification")
        cand_sources = {c.pt_name: sorted(c.sources)
                        for c in cands.candidates}
        pv = None
        # 复用闸过了=本 pid 的验证行已在库（memo 只是免网络）：
        # 无论 memo 命中还是行重建，都不重复追加证据链。
        pv_resumed = resumable and self._ptd_passed(ctx, pid, chosen)
        if pv_resumed:
            pv = self._ptd_memo.get((ctx.marketplace_id, chosen))
            if pv is None:
                pv = self._ptd_rebuild(ctx, pid, chosen)
            if pv is not None:
                self._perf_skip("pt_verification")
        if pv is None:
            pv = self._verify_pt_shared(
                material, chosen, ctx.marketplace_id, cand_sources)
        if not pv_resumed:   # 续跑复用=行已在库，不重复追加证据链
            save_pt_verification(self._store, pv,
                                 store_id=ctx.store_id, pid=pid)
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
            self._save_review_evidence(
                ctx, t, pid, "pt_verification", "pt_not_found",
                ["官方 404：AI 所选 PT 该站点不存在——不保存非法选择"],
                mr=mr)
            return
        sem = pv.semantic
        if sem.status in ("failed", "insufficient", "error", "skipped"):
            self._store.set_item_state(
                t, pid, "needs_review",
                error=f"PT 语义适配未通过（{sem.status}）："
                      f"{sem.detail.get('note', '')}",
                error_kind=f"semantic_{sem.status}")
            self._save_review_evidence(
                ctx, t, pid, "pt_verification", f"semantic_{sem.status}",
                [f"PT 语义适配未通过（{sem.status}）："
                 f"{sem.detail.get('note', '')}"], mr=mr)
            return
        if sem.status == S_UNAVAILABLE:
            self._fail_retryable(t, pid, "unavailable",
                                  f"语义佐证接口暂不可用：{sem.detail}")
            return

        # ---- 阶段 browse_node_verification（PT 存在≠BN 适配） ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._stage_mark(ctx, pid, "browse_node_verification")
        nodes = self._bn_nodes_for(cands, chosen)
        if not nodes:
            # D3 只读诊断：说明为什么没有节点证据（区分 请求失败/解析
            # 失败/未返回分类节点/证据不足）——门槛不变，仍交人工。
            self._store.set_item_state(
                t, pid, "needs_review",
                error="PT 已验证但候选证据无 Browse Node——"
                      f"{bn_missing_reason(cands)}——交人工选节点",
                error_kind="bn_no_nodes")
            self._save_review_evidence(
                ctx, t, pid, "browse_node_verification", "bn_no_nodes",
                ["PT 已验证但候选证据无 Browse Node——"
                 f"{bn_missing_reason(cands)}——交人工选节点"], mr=mr)
            return
        best_node: str | None = None
        for nid in nodes:
            if self._checkpoint(t, pid, auth_evt):
                return
            bnv = None
            bnv_resumed = resumable and self._bn_passed(ctx, pid, nid)
            if bnv_resumed:
                bnv = self._bn_memo.get((ctx.marketplace_id, nid))
                if bnv is None:
                    bnv = self._bn_rebuild(ctx, pid, nid)
                if bnv is not None:
                    self._perf_skip("browse_node_verification")
            if bnv is None:
                bnv = self._verify_bn_shared(material, nid,
                                             ctx.marketplace_id)
            if not bnv_resumed:   # 同上：复用不重复落行
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
            self._save_review_evidence(
                ctx, t, pid, "browse_node_verification", "bn_insufficient",
                [f"BN 证据不足（{len(nodes)} 个候选节点无一确认）——"
                 "绝不猜节点，交人工"], mr=mr)
            return

        # ---- 阶段 schema_analysis（Schema 成功≠属性齐全） ----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._stage_mark(ctx, pid, "schema_analysis")
        so = self._ensure_schema_shared(ctx, chosen)
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
            self._save_review_evidence(
                ctx, t, pid, "schema_analysis", "schema_no_link",
                ["官方响应缺 schema.link——无 Schema 可下载"], mr=mr)
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
        self._store.update_item_progress(t, pid, schema_ok=1,
                                         attr_status=gap.summary)

        # ---- 阶段 persist_result（缺口报告≠可刊登：incomplete 不拦完成）----
        if self._checkpoint(t, pid, auth_evt):
            return
        self._stage_mark(ctx, pid, "persist_result")
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
                    "entries": [{"name": e.name, "path": e.path,
                                 "required": e.requiredness,
                                 "category": e.category,
                                 "reason": str(e.reason)[:150]}
                                for e in gap.entries][:60],
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

    # ------------------------------------------------------------------
    # D1.12.2 内部：阶段打点 / 续跑复用 / 批内去重 / perf 聚合
    # ------------------------------------------------------------------

    def _stage_mark(self, ctx: "_Ctx", pid: str, stage: str) -> None:
        """阶段切换打点：落库（真源）+ 记上一阶段耗时（perf 审计）。"""
        now = time.monotonic()
        with self._perf_lock:
            prev = self._stage_t0.pop(pid, None)
            if prev is not None:
                name, t0 = prev
                st = self._perf["stages"].setdefault(
                    name, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
                dur = (now - t0) * 1000.0
                st["count"] += 1
                st["total_ms"] += dur
                st["max_ms"] = max(st["max_ms"], dur)
            self._stage_t0[pid] = (stage, now)
        self._store.set_item_stage(ctx.task_id, pid, stage)

    def _perf_skip(self, what: str) -> None:
        with self._perf_lock:
            skips = self._perf["stage_skips"]
            skips[what] = skips.get(what, 0) + 1

    def _perf_snapshot(self) -> dict:
        with self._perf_lock:
            out = json.loads(json.dumps(self._perf))   # 深拷贝聚合值
            for name, st in out.get("stages", {}).items():
                if st["count"]:
                    st["avg_ms"] = round(st["total_ms"] / st["count"], 1)
                st["total_ms"] = round(st["total_ms"], 1)
                st["max_ms"] = round(st["max_ms"], 1)
        return out

    def _candidates_for(self, ctx: "_Ctx", pid: str, material,
                        *, reuse: bool):
        """候选检索（可复用）：同任务同指纹此前已落库 → 零网络重建
        CandidatesResult；否则真检索（reports 全量保留在当轮对象里）。"""
        if reuse:
            rows = self._store.candidates(
                ctx.store_id, ctx.marketplace_id, pid, env=ctx.env)
            if rows:
                merged = {}
                for r in rows:
                    pt = str(r.get("pt_name") or "")
                    if not pt:
                        continue
                    c = merged.setdefault(
                        pt, Candidate(pt_name=pt,
                                      display_name=str(
                                          r.get("display_name") or "")))
                    ev = r.get("evidence")
                    strat = str(r.get("strategy") or "reused")
                    if isinstance(ev, dict):
                        c.sources[strat] = ev
                    if r.get("score") is not None:
                        c.score = max(c.score, float(r["score"]))
                if merged:
                    res = CandidatesResult(
                        pid=pid, marketplace_id=ctx.marketplace_id,
                        env_mode=ctx.env,
                        candidates=sorted(
                            merged.values(),
                            key=lambda c: -c.score))
                    setattr(res, "_resumed", True)
                    return res
        return retrieve_candidates(
            self._client, material, marketplace_id=ctx.marketplace_id)

    def _singleflight(self, memo: dict, inflight: dict, key,
                      keep, compute):
        """通用在途去重：结果命中直接回；同 key 在途就等首发；首发
        失败/超时不广播（等待者各自重算=与无去重时同语义，绝不放大
        也绝不吞错）。keep(result)=True 才进结果缓存（部分通过的结论
        不复用）。"""
        with self._memo_lock:
            if key in memo:
                return memo[key]
            fl = inflight.get(key)
            owner = fl is None
            if owner:
                fl = _Inflight()
                inflight[key] = fl
        if not owner:
            fl.event.wait(timeout=120.0)
            if fl.done:
                return fl.result
            return compute()          # 首发失败：自己再算
        try:
            result = compute()
        except BaseException:
            with self._memo_lock:
                inflight.pop(key, None)
            fl.event.set()
            raise
        fl.result = result
        fl.done = True
        with self._memo_lock:
            inflight.pop(key, None)
            if keep(result):
                memo[key] = result
        fl.event.set()
        return result

    def _verify_pt_shared(self, material, chosen: str, mid: str,
                          cand_sources: dict):
        """PTD 验证（singleflight）：同 (站点, PT) 并发条目只发一次真
        请求；全部通过才入结果缓存（部分通过不复用）。"""
        def keep(pv):
            return (pv.exists.status == "passed"
                    and pv.semantic is not None
                    and pv.semantic.status == "passed")
        if (mid, chosen) in self._ptd_memo:
            self._perf_skip("pt_verification_memo")
        return self._singleflight(
            self._ptd_memo, self._ptd_inflight, (mid, chosen), keep,
            lambda: verify_product_type(
                self._client, material, chosen, marketplace_id=mid,
                candidate_sources=cand_sources))

    def _verify_bn_shared(self, material, nid: str, mid: str):
        if (mid, nid) in self._bn_memo:
            self._perf_skip("browse_node_memo")
        return self._singleflight(
            self._bn_memo, self._bn_inflight, (mid, nid),
            lambda bnv: bnv.existence.status == "passed",
            lambda: verify_browse_node(self._client, material, nid,
                                       marketplace_id=mid))

    def _ensure_schema_shared(self, ctx: "_Ctx", pt: str):
        """Schema 获取（singleflight）：同 (站点, PT, 卖家, env) 并发
        条目只走一次 ensure_schema（含缓存命中判定——缓存本身也是共享
        的）。OUT_OK 才入结果缓存；失败各算各的（重试语义不变）。"""
        key = (ctx.marketplace_id, pt, self._seller_id, ctx.env)

        def keep(so):
            return so.status == OUT_OK
        if key in self._schema_memo:
            self._perf_skip("schema_memo")
        return self._singleflight(
            self._schema_memo, self._schema_inflight, key, keep,
            lambda: ensure_schema(
                self._client, self._store, self._cache_root, pt_name=pt,
                marketplace_id=ctx.marketplace_id,
                seller_id=self._seller_id, env=ctx.env))

    def _verif_rows(self, ctx: "_Ctx", pid: str) -> list:
        try:
            return self._store.verifications(
                ctx.store_id, ctx.marketplace_id, pid)
        except Exception:  # noqa: BLE001 —— 复用判定降级=不跳过（安全侧）
            return []

    def _ptd_passed(self, ctx: "_Ctx", pid: str, pt: str) -> bool:
        """续跑复用闸：本任务时间窗内 pt_exists+semantic 双通过且
        PT 与本轮 AI 结论一致（含批内 memo 命中路径的等价判定）。"""
        t0 = ctx.created_at or 0
        ok_pt = ok_sem = False
        for v in self._verif_rows(ctx, pid):
            if int(v.get("checked_at") or 0) < t0:
                continue
            if v.get("pt_name") != pt:
                continue
            if (v.get("check_kind") == "pt_exists"
                    and v.get("status") == "passed"):
                ok_pt = True
            if (v.get("check_kind") == "semantic"
                    and v.get("status") == "passed"):
                ok_sem = True
        return ok_pt and ok_sem

    def _ptd_rebuild(self, ctx: "_Ctx", pid: str, pt: str):
        """从落库行重建 PtVerification 替身（只填下游消费的字段）。
        无法重建（行缺失/形状异常）→ None=不跳过，真跑一遍。"""
        rows = self._verif_rows(ctx, pid)
        ex = next((v for v in reversed(rows)
                   if v.get("check_kind") == "pt_exists"
                   and v.get("pt_name") == pt
                   and v.get("status") == "passed"), None)
        sem = next((v for v in reversed(rows)
                    if v.get("check_kind") == "semantic"
                    and v.get("pt_name") == pt
                    and v.get("status") == "passed"), None)
        if ex is None or sem is None:
            return None
        try:
            src_e = str((ex.get("detail") or {}).get("source") or "spapi")
            src_s = str((sem.get("detail") or {}).get("source") or "spapi")
            pv = PtVerification(
                pt_name=pt, marketplace_id=ctx.marketplace_id, env=ctx.env,
                checked_at=int(ex.get("checked_at") or 0),
                exists=CheckResult(
                    check_kind="pt_exists", status="passed",
                    source=src_e, official=True,
                    detail=dict(ex.get("detail") or {})),
                semantic=CheckResult(
                    check_kind="semantic", status="passed",
                    source=src_s, official=True,
                    detail=dict(sem.get("detail") or {})))
            with self._memo_lock:
                self._ptd_memo.setdefault((ctx.marketplace_id, pt), pv)
            return pv
        except Exception:  # noqa: BLE001 —— 形状对不上=不复用
            return None

    def _bn_passed(self, ctx: "_Ctx", pid: str, nid: str) -> bool:
        t0 = ctx.created_at or 0
        for v in self._verif_rows(ctx, pid):
            if (v.get("check_kind") == "browse_node"
                    and v.get("node_id") == nid
                    and v.get("status") == "passed"
                    and int(v.get("checked_at") or 0) >= t0):
                return True
        return False

    def _bn_rebuild(self, ctx: "_Ctx", pid: str, nid: str):
        rows = self._verif_rows(ctx, pid)
        v = next((r for r in reversed(rows)
                  if r.get("check_kind") == "browse_node"
                  and r.get("node_id") == nid
                  and r.get("status") == "passed"), None)
        if v is None:
            return None
        try:
            src = str((v.get("detail") or {}).get("source") or "spapi")
            det = dict(v.get("detail") or {})
            ok = CheckResult(check_kind="browse_node", status="passed",
                             source=src, official=True, detail=det)
            bnv = BrowseNodeVerification(
                node_id=nid, marketplace_id=ctx.marketplace_id,
                env=ctx.env, checked_at=int(v.get("checked_at") or 0),
                existence=ok,
                catalog=CheckResult(check_kind="browse_node",
                                    status="passed", source=src,
                                    official=True, detail=det),
                fit=CheckResult(check_kind="browse_node", status="passed",
                                source=src, official=True, detail=det),
                usability=CheckResult(
                    check_kind="browse_node", status="not_assessable",
                    source="internal", official=False, detail={}))
            with self._memo_lock:
                self._bn_memo.setdefault((ctx.marketplace_id, nid), bnv)
            return bnv
        except Exception:  # noqa: BLE001
            return None

    def _save_review_evidence(self, ctx, task_id: str, pid: str,
                              stage: str, kind: str,
                              reasons: list | None,
                              mr: "MatchResult | None" = None) -> None:
        """P5：needs_review 时落 AI 建议结构化快照（追加式）。

        只写 pt_review_evidence（建议），绝不写 pt_assignment——待复核
        建议不得被标记为已确认分类（任务二.3）。mr=None 用于无匹配结果
        的早期分支（如资料缺失）——本引擎只在有 mr 的分支调用。证据链
        任何故障都不影响主流程（复核界面至少还有任务条目可看）。"""
        try:
            ai_chosen = None
            ai_conf = None
            ai_detail = None
            if mr is not None:
                ai_conf = mr.confidence
                for row in (mr.candidates or []):
                    if row.get("ai_picked"):
                        ai_chosen = row.get("pt_name")
                        break
                ai_detail = {
                    "status": mr.status,
                    "chosen": mr.chosen,
                    "needs_review": mr.needs_review,
                    "candidates": (mr.candidates or [])[:8],
                    "confidence_breakdown": mr.confidence_breakdown,
                    "official_verified": mr.official_verified,
                }
            official = []
            try:
                rows = self._store.verifications(ctx.store_id,
                                                 ctx.marketplace_id, pid)
                for v in rows[-6:]:
                    detail = v.get("detail") or {}
                    official.append({
                        "check_kind": v.get("check_kind"),
                        "status": v.get("status"),
                        "pt_name": v.get("pt_name"),
                        "node_id": v.get("node_id"),
                        "official": detail.get("official"),
                        "note": str(detail.get("note") or "")[:120],
                    })
            except Exception:              # noqa: BLE001 —— 快照尽力而为
                official = []
            self._store.add_review_evidence(
                store_id=ctx.store_id, marketplace_id=ctx.marketplace_id,
                pid=pid, task_id=task_id, env=ctx.env, stage=stage,
                error_kind=kind,
                review_reasons=[str(r)[:200] for r in (reasons or [])][:12],
                ai_chosen=ai_chosen, ai_confidence=ai_conf,
                ai_detail=ai_detail, official_status=official,
            )
        except Exception:                  # noqa: BLE001 —— 绝不影响主流程
            pass

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
            # 暂停刚落定：消费 pausing 窗口里持久化的「继续」意图——
            # 立即回 running，run() 外层循环会重建线程池续跑（多次
            # 点击只有一个消费方，绝不重复执行）
            if self._store.consume_resume_pending(task_id):
                self._store.cas_task_phase(task_id, ("paused",), "running")
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
    created_at: int = 0        # 任务创建时间（续跑复用判定窗口下界）

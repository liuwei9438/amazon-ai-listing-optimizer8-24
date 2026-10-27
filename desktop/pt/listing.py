# -*- coding: utf-8 -*-
"""pt.listing —— S2c 统一 Amazon 刊登任务引擎（Mock-only 第一阶段）。

三种任务来源（single/selected/category）共用同一套任务模型、状态机、
校验、错误处理与幂等（store.pt_listing_task / pt_listing_item，v7）。

铁律（2026-10-20 S2c 验收令）：
1. 本阶段只允许 Mock 模拟提交：引擎的提交通道 mock_submit 由适配层
   仅在 env='mock' 时装配；live 任务在创建入口即被拒（403），引擎层
   再设一道闸（run 时发现 env!=mock 直接失败——fail-closed，双保险）。
2. 五阶段：precheck（快照重检）→ attributes（准备度闸）→ ptd_check
   （结构校验）→ mock_submit（模拟提交）→ terminal（平台模拟终态）。
   completed/proposed/A/B/C/D 证据等级都绝不等于「官方刊登校验通过」；
   结构校验通过只作模拟结论标注，绝不冒充官方。
3. 快照固化与重检：商品内容指纹/assignment/schema/属性修订在任务创建
   时固化进条目；执行前逐一重检，不一致 → blocked（绝不静默续跑）。
4. 已受理提交（submission_id 落库）绝不盲目重试提交：claim 只领
   pending、retry 不动 completed、提交幂等令牌保证崩溃重放得到同一
   submissionId（不产生第二次提交）。
5. category 来源的商品集合在创建时定死——之后分类移动不改变任务范围。
6. 变体以实际 SKU 为单位处理，父子关系（parent_sku/variant_role）逐
   条目保留；单条失败只影响本条目，不污染整批。

与 pt.tasks（分类引擎）的关系：机制同构（CAS 相位/原子领取/检查点/
恢复/幂等键），但表、词表、阶段、守卫完全独立——绝不改动分类引擎
一行，反之亦然。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from pt.normalize import COL_PARENT_SKU, COL_SKU
from pt.schema import (
    ST_OK, SchemaAnalysis, _cache_filename, schemas_dir,
)
from pt.store import (
    ACTIVE_TASK_PHASES, LISTING_ITEM_FINISHED_STATES, PtStore,
    TaskConflictError, now,
)
from pt.validate import (
    EXC_CONTEXT_STALE, report_to_dict, validate_product,
)

_LOG = logging.getLogger("pt.listing")

DEFAULT_MAX_WORKERS = 2
MAX_WORKERS_CAP = 4            # 刊登提交比分类更重，并发上限更保守
DEFAULT_ITEM_MAX_ATTEMPTS = 2
# S3-B2：受理后轮询预算（秒）——预算尽=结果未知（unknown），绝不自动
# 视为失败重试；间隔默认 0.2s。租约 TTL 远大于单次 SQLite 写（毫秒级），
# renew 成功到写库之间不可能被接管。
DEFAULT_POLL_BUDGET = 8.0
DEFAULT_POLL_INTERVAL = 0.2
DEFAULT_LEASE_TTL = 30.0
RESUMABLE_PHASES = ("created", "paused", "interrupted")
FINALIZE_QUIESCE_TRIES = 200
FINALIZE_QUIESCE_SLEEP = 0.01


class ListingStateError(Exception):
    """任务相位不允许此操作（如对终态任务 run/resume）。"""


class ListingSubmitError(Exception):
    """提交通道错误（kind: auth|forbidden|badrequest|ratelimit|server|
    network|parse|environment；retryable=机械可重试类）。"""

    def __init__(self, message: str, *, kind: str, retryable: bool,
                 retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# 快照与展开（创建时与执行前重检共用同一函数——单一事实源）
# ---------------------------------------------------------------------------

def content_fingerprint(record: dict) -> str:
    """商品内容指纹：标准化记录里实际影响刊登内容的部分（标题/SKU/
    分类/采集行）。opt（AI 优化结果）不进指纹——优化文案变化不视为
    商品内容变更（S2d 引入文案快照时再收严）。"""
    core = {
        "title": str(record.get("title") or ""),
        "sku": str(record.get("sku") or ""),
        "cat": str(record.get("cat") or ""),
        "raw_rows": record.get("raw_rows") or [],
    }
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True)
        .encode("utf-8")).hexdigest()[:24]


def attr_revision(store: PtStore, sid: str, mid: str, env: str,
                  pid: str) -> str:
    """属性修订指纹：现行 active 行的 (路径, 值, 复核态, 更新时间) 排序
    串——确认/编辑/改值任何一步都会改变修订号。stale 行不算（已失效
    的历史不在提交范围内）。"""
    rows = store.get_attributes(sid, mid, env, pid)
    parts = sorted(
        f"{r.get('attribute_path')}|{r.get('value_json')}|"
        f"{r.get('review_status')}|{r.get('updated_at')}"
        for r in rows if r.get("lifecycle") == "active")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


def expand_skus(record: dict, pid: str) -> list[dict]:
    """按实际 SKU 展开（变体以 SKU 为单位）：采集行 SKU 为主，回退商品
    顶层 sku/pid；父子关系=行的 父SKU 列。role：被其他行引用为父=parent，
    自身带父SKU=child，其余=standalone（同 SKU 去重保首个）。"""
    rows = record.get("raw_rows") or []
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        sku = str((r or {}).get(COL_SKU) or "").strip()
        if not sku:
            sku = str(record.get("sku") or "").strip() or str(pid)
        if sku in seen:
            continue
        seen.add(sku)
        out.append({"sku": sku,
                    "parent_sku": str((r or {}).get(COL_PARENT_SKU)
                                      or "").strip(),
                    "variant_role": "child"})
    if not out:                       # 无采集行：整品一件
        out.append({"sku": str(record.get("sku") or pid),
                    "parent_sku": "", "variant_role": "standalone"})
    child_parents = {x["parent_sku"] for x in out if x["parent_sku"]}
    for x in out:
        if x["parent_sku"]:
            x["variant_role"] = "child"
        elif x["sku"] in child_parents:
            x["variant_role"] = "parent"
        else:
            x["variant_role"] = "standalone"
    return out


def item_snapshot(store: PtStore, record: dict, sid: str, mid: str,
                  env: str, pid: str) -> dict:
    """创建时的上下文快照（分类分配/Schema/属性修订/内容指纹）。"""
    asg = store.current_assignment(sid, mid, pid, env=env) or {}
    key = str(asg.get("schema_cache_key") or "")
    meta = (store.get_schema_meta(key) or {}) if key else {}
    return {
        "content_sha": content_fingerprint(record),
        "assignment_id": asg.get("id"),
        "pt_name": str(asg.get("pt_name") or ""),
        "schema_cache_key": key,
        "schema_version": str(meta.get("product_type_version") or ""),
        "attr_revision": attr_revision(store, sid, mid, env, pid),
    }


def readiness_blockers(store: PtStore, cache_root: str, sid: str, mid: str,
                       env: str, pid: str) -> list[str]:
    """准备度闸（与 S2b 工作台 can_prepare 同一判定规则，独立实现以免
    改动工作台；test_pt_listing 交叉一致性测试守卫两处不漂移）：
    分类分配在 + 属性候选>0 + 全部 confirmed/edited + 无冲突行 +
    Schema 缓存可用。返回逐条真实原因（空=通过）。"""
    asg = store.current_assignment(sid, mid, pid, env=env) or {}
    rows = store.get_attributes(sid, mid, env, pid)
    blockers: list[str] = []
    if not (asg and str(asg.get("pt_name") or "")):
        blockers.append("分类未完成（无现行 Product Type 分配）")
    n_active = len(rows)
    n_ok = sum(1 for r in rows
               if r.get("review_status") in ("confirmed", "edited"))
    if n_active == 0:
        blockers.append("属性候选还没生成")
    elif n_ok < n_active:
        blockers.append(f"属性未全部确认（{n_ok}/{n_active} 已确认）")
    if any(r.get("source_type") == "gap.conflict" for r in rows):
        blockers.append("存在冲突属性（须人工裁决）")
    if asg:
        key = str(asg.get("schema_cache_key") or "")
        meta = (store.get_schema_meta(key) or {}) if key else {}
        present = bool(key) and (
            schemas_dir(cache_root) / _cache_filename(key)).is_file()
        version = str(meta.get("product_type_version") or "")
        mismatch = sum(1 for r in rows
                       if version and str(r.get("schema_version") or "")
                       != version)
        if not (present and meta.get("status") == ST_OK):
            blockers.append("Schema 缓存不可用（元数据/文件缺失或状态异常）")
        elif mismatch:
            blockers.append(f"属性行 Schema 版本与现行不符（{mismatch} 行）")
    return blockers


def build_payload(record: dict, attr_rows: list[dict], pt_name: str,
                  pid: str) -> dict:
    """提交载荷（Mock）：PT + 已确认/编辑属性值 + 商品基础字段。只收
    confirmed/edited 行——proposed/rejected 值绝不进载荷（服务端闸）。"""
    attrs: dict = {}
    for r in attr_rows:
        if r.get("review_status") not in ("confirmed", "edited"):
            continue
        try:
            attrs[str(r.get("attribute_path"))] = json.loads(
                r.get("value_json") or "null")
        except (TypeError, ValueError):
            continue
    title = str(record.get("title") or "")
    if title and "item_name" not in attrs:
        attrs["item_name"] = [{"value": title, "language_tag": "en_GB"}]
    fpt = str(pt_name or "")
    if fpt and "feed_product_type" not in attrs:
        attrs["feed_product_type"] = fpt
    return {"productType": pt_name, "attributes": attrs,
            "_wz": {"pid": pid, "mock": True}}


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

@dataclass
class _LCtx:
    """一次 run 的任务上下文（env 贯穿每笔写入）。"""
    task_id: str
    store_id: str
    marketplace_id: str
    env: str
    owner: str = ""      # S3-B2：执行租约持有者（空=不校验租约，测试直调）


class ListingEngine:
    """统一刊登任务编排器（依赖全注入，无隐藏全局）。

    参数：
      store         pt.store.PtStore（v7）
      mock_submit   提交通道：fn(seller_id, marketplace_id, sku, payload,
                    idem_token) -> {submission_id, status, issues?}；
                    仅 env='mock' 时由适配层装配（live 一律不装配）
      mock_status   终态轮询：fn(seller_id, marketplace_id, sku,
                    submission_id) -> {status, issues?}（同上）
      load_record   pid -> 标准化商品记录 | None（不存在/读不到）
      schema_analysis  cache_key -> pt.schema.SchemaAnalysis（缓存
                    fail-closed：不可用即抛——ptd_check 据此 blocked）
      cache_root    Schema 缓存根目录（准备度闸用）
      seller_id     卖家上下文（Mock 提交寻址用）
      max_workers   并发 SKU 数（默认 2，上限 4）
      item_max_attempts  条目失败预算（retryable_failed 计数上限）
    """

    def __init__(self, *, store: PtStore,
                 mock_submit: Callable[..., dict],
                 mock_status: Callable[..., dict],
                 load_record: Callable[[str], dict | None],
                 schema_analysis: Callable[[str], SchemaAnalysis],
                 cache_root: str, seller_id: str = "",
                 max_workers: int = DEFAULT_MAX_WORKERS,
                 item_max_attempts: int = DEFAULT_ITEM_MAX_ATTEMPTS,
                 poll_budget: float = DEFAULT_POLL_BUDGET,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 lease_ttl: float = DEFAULT_LEASE_TTL):
        mw = int(max_workers)
        if mw < 1 or mw > MAX_WORKERS_CAP:
            raise ValueError(
                f"max_workers 须在 1..{MAX_WORKERS_CAP}（刊登提交保守并发）")
        if int(item_max_attempts) < 1:
            raise ValueError("item_max_attempts 至少为 1")
        self._store = store
        self._mock_submit = mock_submit
        self._mock_status = mock_status
        self._load_record = load_record
        self._schema_analysis = schema_analysis
        self._cache_root = str(cache_root)
        self._seller_id = str(seller_id or "")
        self._max_workers = mw
        self._item_max_attempts = int(item_max_attempts)
        self._poll_budget = float(poll_budget)      # 受理后轮询总预算（秒）
        self._poll_interval = float(poll_interval)  # 轮询间隔（秒）
        self._lease_ttl = float(lease_ttl)          # 执行租约 TTL（秒）

    # ------------------------------------------------------------------
    # 生命周期 API（与分类 TaskEngine 同构）
    # ------------------------------------------------------------------

    def create_task(self, *, user_id: str, store_id: str,
                    marketplace_id: str, env: str, source_mode: str,
                    items: Iterable[dict], source: str = "",
                    source_ref: str = "", idem_key: str | None = None,
                    task_id: str | None = None, idem_fp: str = "",
                    new_operation: bool = False) -> dict:
        """创建任务（幂等/SKU 重叠守卫由 store.create_listing_task 保证；
        快照已在 items 里固化——category 集合此刻定死）。idem_fp=内容
        指纹（命中判定新鲜度用）；new_operation=True=用户显式声明新一
        轮刊登（键加代数后缀，不吞旧任务历史）。"""
        tid = task_id or f"pt-l-{uuid.uuid4().hex[:12]}"
        return self._store.create_listing_task(
            tid, user_id, store_id, marketplace_id, env, source_mode,
            items, source=source, source_ref=source_ref, idem_key=idem_key,
            idem_fp=idem_fp, new_operation=new_operation)

    def resume(self, task_id: str) -> bool:
        """恢复/起跑：created|paused|interrupted → running（CAS）。"""
        ok = self._store.cas_listing_phase(
            task_id, RESUMABLE_PHASES, "running")
        if ok:
            self._store.set_listing_resume_pending(task_id, False)
        return ok

    def request_pause(self, task_id: str) -> bool:
        """请求暂停：在途到安全检查点后 paused（不领新、不强杀）。"""
        self._store.set_listing_resume_pending(task_id, False)
        ok = self._store.cas_listing_phase(
            task_id, ("created", "running"), "pausing")
        if ok and self._store.listing_item_counts(task_id)["running"] == 0:
            self._store.cas_listing_phase(task_id, ("pausing",), "paused")
        return ok

    def request_cancel(self, task_id: str) -> bool:
        """请求取消：终态条目保留、审计不删；未执行条目记 cancelled。"""
        self._store.set_listing_resume_pending(task_id, False)
        ok = self._store.cas_listing_phase(
            task_id, ACTIVE_TASK_PHASES, "cancelling")
        if ok and self._store.listing_item_counts(task_id)["running"] == 0:
            for it in self._store.listing_task_items(
                    task_id, states=["pending"]):
                self._store.update_listing_item(
                    task_id, it["pid"], it["sku"], state="cancelled",
                    error="用户取消", error_kind="cancelled")
            self._store.cas_listing_phase(
                task_id, ("cancelling",), "cancelled", finished=True)
        return ok

    def recover(self) -> list[str]:
        """进程异常退出后的启动恢复（残留 → interrupted）。"""
        return self._store.recover_listing_interrupted()

    # ------------------------------------------------------------------
    # 主循环（结构沿用分类引擎：外层消费「继续」意图、内层领取-处理）
    # ------------------------------------------------------------------

    def run(self, task_id: str) -> dict:
        """同步推进一个刊登任务到当前可达的稳定相位，返回任务行。

        - Mock-only 硬闸：任务 env!='mock' 或提交通道未装配 → 直接
          failed（fail-closed，绝不带 live 任务碰任何 Listings 写）。
        - 暂停/取消只经检查点生效：绝不强杀线程、绝不打断 SQLite 写
          事务（与分类批复六同纪律）。
        """
        task = self._store.get_listing_task(task_id)
        if task is None:
            raise KeyError(f"刊登任务不存在: {task_id}")
        if task["env"] != "mock" or not self._mock_submit \
                or not self._mock_status:
            self._store.cas_listing_phase(
                task_id, ("created", "running"), "failed", finished=True,
                error="提交通道未装配或 env 非 mock——S2c 第一阶段仅支持"
                      " Mock 刊登任务（真实 Listings 写通道未接入）")
            raise ListingStateError(
                f"刊登任务 {task_id} env={task['env']} 不可运行"
                f"（S2c 第一阶段 Mock-only）")
        if not self._store.cas_listing_phase(
                task_id, ("created", "running"), "running"):
            raise ListingStateError(
                f"刊登任务 {task_id} 处于 {task['phase']}，不可运行"
                f"（resume 后再 run；暂停/取消由 request_* 发起）")
        # S3-B2 P1-2：执行租约——跨实例互斥的唯一权威。take 失败=别的
        # 实例正持有有效租约在跑：本实例不跑也不写任何状态（返回当前
        # 任务行即可，持有者负责推进与收尾）。接管只能发生在租约过期
        # 后（原子判定），take 成功即顺带回收孤儿 running 条目。owner
        # 内嵌 pid：recover 对未过期租约可查 owner 进程死活——死进程
        # 立刻接管恢复，活进程绝不抢断（lease_holds）。
        owner = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        if not self._store.take_listing_lease(
                task_id, owner, self._lease_ttl):
            return self._store.get_listing_task(task_id)
        try:
            return self._run_owned(task_id, owner, task)
        finally:
            self._store.release_listing_lease(task_id, owner)

    def _run_owned(self, task_id: str, owner: str, task: dict) -> dict:
        """持租约执行（renew-then-write 协议：每次写库前先续租成功；
        续租失败=已被接管/过期，立即停止一切写入静默退出）。"""
        def _still_mine() -> bool:
            return self._store.renew_listing_lease(
                task_id, owner, self._lease_ttl)

        ctx = _LCtx(task_id=task_id, store_id=task["store_id"],
                    marketplace_id=task["marketplace_id"], env="mock",
                    owner=owner)
        while True:
            if not _still_mine():
                return self._store.get_listing_task(task_id)
            inflight: set = set()
            pool = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix=f"pt-l-{str(task_id)[:16]}")
            try:
                while True:
                    phase = self._phase_of(task_id)
                    stop = phase != "running" or not _still_mine()
                    if not stop:
                        while len(inflight) < self._max_workers:
                            it = self._store.claim_listing_item(task_id)
                            if it is None:
                                break
                            inflight.add(pool.submit(
                                self._process_item_safe, ctx, it))
                    if inflight:
                        _, inflight = self._wait_some(inflight)
                        continue
                    if stop:
                        break
                    requeued = self._store.requeue_listing_retryable(
                        task_id, self._item_max_attempts)
                    counts = self._store.listing_item_counts(task_id)
                    if counts["pending"] == 0:
                        break                    # 全部条目已到终态
                    if requeued == 0:
                        time.sleep(0.01)         # 相位瞬变，稍候
            finally:
                pool.shutdown(wait=True)
            if not _still_mine():
                # 收尾（_finalize 是写操作）同样受租约保护
                return self._store.get_listing_task(task_id)
            task = self._finalize(task_id)
            if task["phase"] != "running":
                return task
            # running=消费到了挂起的「继续」意图：回外层循环顶部
            # 重建线程池续跑（无需任何再点击）。

    def _phase_of(self, task_id: str) -> str | None:
        task = self._store.get_listing_task(task_id)
        return task["phase"] if task else None

    @staticmethod
    def _wait_some(futures: set) -> tuple[list, set]:
        done, pending = wait(futures, return_when=FIRST_COMPLETED)
        for f in done:
            exc = f.exception()
            if exc is not None:      # _process_item_safe 已兜底，此为再保险
                _LOG.error("listing worker 未捕获异常: %r", exc)
        return list(done), set(pending)

    def _finalize(self, task_id: str) -> dict:
        """收尾相位推导：全部以条目真源计数。blocked/失败/取消算
        issues（completed_with_issues），纯机械/存储故障才 failed。"""
        for _ in range(FINALIZE_QUIESCE_TRIES):
            counts = self._store.listing_item_counts(task_id)
            if not counts["running"] and not counts["pending"]:
                break
            time.sleep(FINALIZE_QUIESCE_SLEEP)
        task = self._store.get_listing_task(task_id)
        phase = task["phase"]
        if phase == "pausing":
            for it in self._store.listing_task_items(
                    task_id, states=["running"]):
                self._store.update_listing_item(
                    task_id, it["pid"], it["sku"], state="pending")
            cur = self._store.get_listing_task(task_id)
            self._store.cas_listing_phase(
                task_id, ("pausing",), "paused", error=cur["error"])
            if self._store.consume_listing_resume_pending(task_id):
                self._store.cas_listing_phase(
                    task_id, ("paused",), "running")
        elif phase == "cancelling":
            for it in self._store.listing_task_items(
                    task_id, states=["pending", "running"]):
                self._store.update_listing_item(
                    task_id, it["pid"], it["sku"], state="cancelled",
                    error="用户取消", error_kind="cancelled")
            self._store.cas_listing_phase(
                task_id, ("cancelling",), "cancelled", finished=True)
        elif phase == "running":
            counts = self._store.listing_item_counts(task_id)
            issues = (counts["blocked"] + counts["retryable_failed"]
                      + counts["permanent_failed"] + counts["cancelled"])
            final = "completed_with_issues" if issues else "completed"
            self._store.cas_listing_phase(
                task_id, ("running",), final, finished=True)
        return self._store.get_listing_task(task_id)

    # ------------------------------------------------------------------
    # 单条目五阶段处理
    # ------------------------------------------------------------------

    def _process_item_safe(self, ctx: _LCtx, it: dict) -> None:
        try:
            self._process_item(ctx, it)
        except sqlite3.Error as e:
            _LOG.exception("listing 条目存储故障，任务失败")
            self._store.cas_listing_phase(
                ctx.task_id, ("running",), "failed",
                error=f"存储故障: {e}")
        except Exception:  # noqa: BLE001 —— 条目级异常绝不带崩任务
            _LOG.exception("listing 条目处理异常 task=%s pid=%s sku=%s",
                           ctx.task_id, it["pid"], it["sku"])
            try:
                self._store.update_listing_item(
                    ctx.task_id, it["pid"], it["sku"],
                    state="retryable_failed", error_kind="internal",
                    error="处理异常（详见日志）")
                self._store.requeue_listing_retryable(
                    ctx.task_id, self._item_max_attempts)
            except Exception:  # noqa: BLE001
                _LOG.exception("listing 条目失败落库也失败（保留现场）")

    def _process_item(self, ctx: _LCtx, it: dict) -> None:
        if ctx.owner and not self._store.renew_listing_lease(
                ctx.task_id, ctx.owner, self._lease_ttl):
            return                # 租约已被接管：不写任何状态静默退出
        task_id, sid, mid = ctx.task_id, ctx.store_id, ctx.marketplace_id
        pid, sku = it["pid"], it["sku"]
        row = next((x for x in self._store.listing_task_items(task_id)
                    if x["pid"] == pid and x["sku"] == sku), None)
        if row is None:
            return
        # 已受理条目兜底（claim 只领 pending，正常到不了这；轮询中断的
        # 重试除外）——验收令硬约束：submitted 绝不盲目重试提交，只允许
        # 续跑终态轮询（幂等令牌保证重放不产生第二次受理）。
        if row.get("submission_id"):
            self._poll_terminal(ctx, pid, sku, row)
            return

        # —— 阶段 1：precheck（快照重检）——
        self._store.update_listing_item(task_id, pid, sku, stage="precheck")
        if self._checkpoint(task_id, pid, sku):
            return
        record = self._load_record(pid)
        if record is None:
            self._blocked(task_id, pid, sku, "product_unavailable",
                          "商品资料读不到（不存在/失权/服务器不可达）"
                          "——快照无法重检，人工处理后再重试")
            return
        cur = item_snapshot(self._store, record, sid, mid, "mock", pid)
        changes = []
        if cur["content_sha"] != str(row.get("content_sha") or ""):
            changes.append("商品内容已变更（标题/采集行/SKU/分类）")
        if cur["assignment_id"] != row.get("assignment_id"):
            changes.append(
                f"分类分配已变更（快照 assignment#{row.get('assignment_id')}"
                f" → 现行 #{cur['assignment_id']}）")
        if cur["schema_cache_key"] != str(row.get("schema_cache_key") or "") \
                or cur["schema_version"] != str(
                    row.get("schema_version") or ""):
            changes.append("Schema 缓存已变更（分类重跑或版本更新）")
        if cur["attr_revision"] != str(row.get("attr_revision") or ""):
            changes.append("属性修订已变更（确认/编辑/改值）")
        if changes:
            # S2d：快照失效=第七类异常（商品或 Schema 已变化）开工单
            # （商品级 sku=''，与校验报告同键；prune=False——局部陈述
            # 不清其它票）
            self._store.upsert_review_exceptions(
                sid, mid, pid, "mock", prune=False,
                exceptions=[{
                    "type": EXC_CONTEXT_STALE, "path": "snapshot",
                    "entries": [{
                        "path": "snapshot", "rule": "snapshot",
                        "verdict": "failed",
                        "reason": "；".join(changes),
                        "source": f"listing:{task_id}",
                        "suggestion": "确认现状后重试（重检通过自动关单）"
                                      "或重建任务"}],
                    "summary": "商品或 Schema 已变化，任务快照失效"}],
                source=f"listing:{task_id}")
            self._blocked(task_id, pid, sku, "snapshot_stale",
                          "；".join(changes) + "——快照失效，须重新检查"
                          "（确认现状后重建任务或重试）",
                          check={"changes": changes})
            return

        # —— 阶段 2：attributes（准备度闸，规则=工作台 can_prepare）——
        self._store.update_listing_item(task_id, pid, sku, stage="attributes")
        if self._checkpoint(task_id, pid, sku):
            return
        blockers = readiness_blockers(
            self._store, self._cache_root, sid, mid, "mock", pid)
        if blockers:
            self._blocked(task_id, pid, sku, "not_ready",
                          "属性准备未过闸：" + "；".join(blockers),
                          check={"blockers": blockers})
            return

        # —— 阶段 3：ptd_check（S2d 完整校验引擎——**此刻重跑**，不信
        # 行内历史校验状态；必填/类型/枚举/范围/格式/条件必填/冲突/兼容
        # 品牌/变体完整性/官方验证记录全查，任何阻断类别在即 blocked；
        # validated 只是本地准备度结论，绝不冒充官方 Listings 校验通过）——
        self._store.update_listing_item(task_id, pid, sku, stage="ptd_check")
        if self._checkpoint(task_id, pid, sku):
            return
        rows = self._store.get_attributes(sid, mid, "mock", pid)
        try:
            analysis = self._schema_analysis(
                str(row.get("schema_cache_key") or ""))
        except Exception as e:  # noqa: BLE001 —— 缓存 fail-closed 即阻断
            self._blocked(task_id, pid, sku, "schema_unavailable",
                          f"Schema 缓存不可用，无法执行结构校验：{e}")
            return
        vers = self._store.verifications(sid, mid, pid, "pt_exists")
        ver = next((v for v in reversed(vers)
                    if v.get("verified_against") == "mock"), {})
        vstat = ver.get("status") or None
        rep = validate_product(
            analysis, rows,
            variant_roles=expand_skus(record, pid),
            assignment=self._store.current_assignment(
                sid, mid, pid, env="mock"),
            pt_verified=(True if vstat == "passed"
                         else (False if vstat else None)))
        # 报告与异常队列随每次 ptd_check 落库（商品级 sku=''：S1/S2d 的
        # 属性集是 pid 级，与手动校验 API 同键——同店同品的票只开一份）：
        # is_current 单活（旧报告 superseded 留历史）、消失的异常自动
        # 关闭、复现的异常刷新不堆积——引擎只开工单，处置只能走人工
        # 复核 API（human 授权）
        self._store.save_validation(
            sid, mid, pid, "mock",
            fingerprint={
                "content_sha": row.get("content_sha"),
                "assignment_id": row.get("assignment_id"),
                "pt_name": row.get("pt_name"),
                "schema_cache_key": row.get("schema_cache_key"),
                "schema_version": row.get("schema_version"),
                "attr_revision": row.get("attr_revision")},
            report=report_to_dict(rep), source=f"listing:{task_id}")
        check = {
            "validation": "passed" if rep.validated else
                          f"blocked({'、'.join(rep.blocking)})",
            "counts": rep.counts,
            "pt_exists": vstat or "none",
            "note": "S2d 本地校验引擎（全部适用规则实跑）+官方验证记录"
                    "核对；validated 只是本地结论，绝不等于官方 Listings"
                    "校验通过",
        }
        if not rep.validated:
            why = [f"{e.path}：{e.reason}" for e in rep.entries
                   if e.category]
            kind = "variant_incomplete" \
                if rep.blocking == ["variant_incomplete"] else "ptd_not_passed"
            self._blocked(
                task_id, pid, sku, kind,
                "PTD 校验未过：" + "；".join(why[:5])
                + (f"（共 {len(why)} 项，详见复核队列）" if len(why) > 5
                   else "（详见复核队列）"),
                check=check)
            return

        # —— 阶段 4：mock_submit ——
        self._store.update_listing_item(task_id, pid, sku, stage="mock_submit")
        if self._checkpoint(task_id, pid, sku):
            return
        payload = build_payload(record, rows, str(row.get("pt_name")), pid)
        idem = hashlib.sha256(
            f"{task_id}|{pid}|{sku}|{row.get('content_sha')}|"
            f"{row.get('assignment_id')}|{row.get('schema_version')}|"
            f"{row.get('attr_revision')}".encode("utf-8")).hexdigest()[:32]
        # S3-B2：提交前再续租（写 submitting 状态与提交结果都受保护）
        if ctx.owner and not self._store.renew_listing_lease(
                ctx.task_id, ctx.owner, self._lease_ttl):
            return
        self._store.update_listing_item(
            task_id, pid, sku, platform_status="submitting",
            platform={"source": "mock", "request_id": idem, "issues": [],
                      "mock": True})
        try:
            res = self._mock_submit(self._seller_id, mid, sku, payload, idem)
        except ListingSubmitError as e:
            if e.retryable:
                self._store.update_listing_item(
                    task_id, pid, sku, state="retryable_failed",
                    error_kind=f"submit_{e.kind}", error=str(e),
                    platform_status="not_submitted")
                self._store.requeue_listing_retryable(
                    task_id, self._item_max_attempts)
            else:
                self._store.update_listing_item(
                    task_id, pid, sku, state="permanent_failed",
                    error_kind=f"submit_{e.kind}", error=str(e),
                    platform_status="not_submitted")
            return
        submission_id = (str(res["submissionId"])   # 官方形状 camelCase
                         if res.get("submissionId") else "")
        # 受理即写 acknowledged（提交结果回写前先续租——接管者可凭
        # 幂等令牌重放同一受理，绝不产生第二次提交）
        if ctx.owner and not self._store.renew_listing_lease(
                ctx.task_id, ctx.owner, self._lease_ttl):
            return
        self._store.update_listing_item(
            task_id, pid, sku,
            submission_id=(submission_id or None),
            submitted_at=now(),
            platform_status="acknowledged",
            platform={"source": "mock", "request_id": idem,
                      "response_id": submission_id,
                      "submitted_at": now(), "issues": [], "mock": True},
            submit_result={"status": str(res.get("status") or ""),
                           "mock": True,
                           "note": "模拟提交（Mock），非官方 Listings 结果"})

        # —— 阶段 5：terminal（平台模拟终态轮询）——
        self._store.update_listing_item(task_id, pid, sku, stage="terminal")
        if self._checkpoint(task_id, pid, sku):
            return
        self._poll_terminal(ctx, pid, sku, {
            "submission_id": submission_id, "request_id": idem})

    def _poll_terminal(self, ctx: _LCtx, pid: str, sku: str,
                       row: dict) -> None:
        """终态轮询（受理后唯一允许的后续动作——绝不二次提交）。

        S3-B2 平台结果契约：在时间预算内轮询（每 tick 先续租、后查态）；
        PROCESSED→accepted、REJECTED→rejected（issues 全量透传）、
        PROCESSING→processing 继续等。预算尽或持续断线 → blocked+
        result_unknown+platform unknown——已受理的提交结果未知时绝不
        自动当失败重试；人工「重试」=幂等令牌重放，核对同一受理号，
        不会产生第二次提交。"""
        task_id = ctx.task_id
        sid = str(row.get("submission_id") or "")
        rid = str(row.get("request_id") or "") or self._request_id_of(row)
        mid = self._store.get_listing_task(task_id)["marketplace_id"]
        note = "平台模拟终态（Mock），非官方结论"
        deadline = time.monotonic() + self._poll_budget
        last_err = ""
        wrote_processing = False
        while True:
            if ctx.owner and not self._store.renew_listing_lease(
                    task_id, ctx.owner, self._lease_ttl):
                return              # 租约被接管：静默退出，接管者续跑
            if self._checkpoint(task_id, pid, sku):
                return
            try:
                st = self._mock_status(self._seller_id, mid, sku, sid)
            except ListingSubmitError as e:
                # 断线/超限可能是暂态：预算内继续重试，预算尽=未知
                last_err = str(e)
                st = None
            if st is not None:
                status = str(st.get("status") or "")
                issues = st.get("issues") or []
                base = {"source": "mock", "request_id": rid,
                        "response_id": sid,
                        "submitted_at": (row.get("submitted_at") or now()),
                        "issues": issues, "mock": True}
                if status == "PROCESSED":
                    self._store.update_listing_item(
                        task_id, pid, sku, state="completed",
                        stage="terminal", error=None, error_kind=None,
                        platform_status="accepted",
                        platform={**base, "status": "accepted",
                                  "checked_at": now()},
                        submit_result={"status": status, "issues": issues,
                                       "mock": True, "note": note})
                    return
                if status == "REJECTED":
                    self._store.update_listing_item(
                        task_id, pid, sku, state="permanent_failed",
                        error_kind="mock_rejected",
                        error="模拟平台拒绝：" + json.dumps(
                            issues, ensure_ascii=False)[:300],
                        platform_status="rejected",
                        platform={**base, "status": "rejected",
                                  "checked_at": now()},
                        submit_result={"status": status, "issues": issues,
                                       "mock": True, "note": note})
                    return
                # PROCESSING（或未知中间态）：如实记 processing 后继续等
                if not wrote_processing:
                    wrote_processing = True
                    self._store.update_listing_item(
                        task_id, pid, sku,
                        platform_status="processing",
                        platform={**base, "status": "processing",
                                  "checked_at": now()})
            if time.monotonic() >= deadline:
                break
            time.sleep(self._poll_interval)
        # —— 结果未知：不得自动当失败、不得盲目重发 ——
        self._store.update_listing_item(
            task_id, pid, sku, state="blocked",
            error_kind="result_unknown",
            error="受理后结果未知（平台仍在处理/查询超时："
                  f"{last_err or '未得终态'}）——已受理不会重发；"
                  "点「重试」将以同一幂等令牌重放核对受理号，"
                  "不产生第二次提交",
            platform_status="unknown",
            platform={"source": "mock", "request_id": rid,
                      "response_id": sid,
                      "submitted_at": row.get("submitted_at"),
                      "status": "unknown", "issues": [],
                      "mock": True, "checked_at": now()})

    def _request_id_of(self, row: dict) -> str:
        """从行里取 request_id（新行=platform_json，旧行=空——受理号兜底）。"""
        try:
            pj = json.loads(str(row.get("platform_json") or "") or "{}")
        except Exception:  # noqa: BLE001 —— 脏数据按空处理
            pj = {}
        return str(pj.get("request_id") or "")

    def _blocked(self, task_id: str, pid: str, sku: str, kind: str,
                 msg: str, check: dict | None = None) -> None:
        """前置条件不满足 → blocked（人工修复后可 retry 重检）。"""
        self._store.update_listing_item(
            task_id, pid, sku, state="blocked", error_kind=kind,
            error=msg, check=check)

    def _checkpoint(self, task_id: str, pid: str, sku: str) -> bool:
        """阶段边界安全检查点：pausing → 回 pending；cancelling →
        cancelled。返回 True=已让位（调用方立即 return）。"""
        phase = self._phase_of(task_id)
        if phase == "pausing":
            self._store.update_listing_item(
                task_id, pid, sku, state="pending")
            return True
        if phase in ("cancelling", "cancelled"):
            self._store.update_listing_item(
                task_id, pid, sku, state="cancelled",
                error="用户取消", error_kind="cancelled")
            return True
        return False

# -*- coding: utf-8 -*-
"""pt.store —— 第一阶段分类引擎的 SQLite 存储层（纯标准库）。

设计约束（修订版 v2 + 四项最终修正，2026-10-10 批复）：
1. 三概念分离：ProductType / BrowseNode / ProductTypeSchema 各自独立表，
   产品维度只存引用（pt_name / node_id / schema cache_key），不内嵌定义体。
2. Schema 缓存键 = 全部实际影响响应的请求参数 + 卖家上下文
   （marketplace / productType / sellerId / locale / requirements /
   requirementsEnforced / parentageLevel / productTypeVersion）——
   跨站点、跨店铺、跨版本绝不复用（修正 3）。
3. Browse Node 证据分级（修正 1）：evidence_status ∈
   pending|confirmed|missing|insufficient。Catalog 搜空 = insufficient
   （证据不足），绝不等同于 missing；存在性(evidence_status)与商品适配
   判断(fit_status)分开记录。
4. 任务恢复以 pt_task_item 逐商品状态为准（修正 2）；pt_task.last_pid
   只是便利字段，恢复逻辑不读它。
5. 产品维度记录按 (store_id, marketplace_id) 双重隔离（修正 6）；
   官方公共数据（pt_type_dict、无卖家上下文的 pt_schema_meta）可跨店共享。
6. 本模块不 import main（工程约束 5）；SP-API 能力经适配层注入。
7. 第一阶段不产生任何「可刊登」状态：状态枚举里没有它，测试守卫
   test_pt_store T14（修正 4；正式刊登属第二阶段）。
8. Mock/Live 环境隔离贯穿候选、匹配、验证及缓存全过程
   （2026-10-12 批复修正 3）：v2 起六张数据表带 env 列
   （mock|live，缺省 live）——pt_type_dict / pt_schema_meta /
   pt_browse_node / pt_candidate / pt_assignment / pt_task 全部按 env
   分行分读；pt_verification 沿用既有 verified_against 列（同一枚举）。
   mock 环境检索的字典/候选/schema 缓存/分配/任务对 live 读取一律不可
   见，反之亦然。v1→v2 最小迁移：三张带唯一键的表重建
   （pt_type_dict/pt_browse_node/pt_candidate），三张直接 ALTER 加列
   （pt_schema_meta/pt_assignment/pt_task）。
9. 历史数据不得自动获得官方资格（2026-10-12 第二轮批复修正 1）：
   v1→v2 迁移的旧行一律落 env='legacy_unverified' 隔离区——来源无法
   证明的数据不得冒充 live 官方数据；live/mock 读写永远看不见 legacy
   分区，显式传 env="legacy_unverified" 可审计；pt_verification 的
   历史行 verified_against='live' 同步降级 'legacy_unverified'
   （mock 行保持 mock——mock 声称本来就是诚实的）。新写入不走
   legacy 值：业务代码仍只写 mock|live。
10. 模块 7 批量任务（2026-10-12 模块 7 批复）v2→v3：
   - 任务整体状态扩为 created/running/pausing/paused/completed/
     completed_with_issues/failed/interrupted/cancelling/cancelled
     （控制请求即相位：pausing/cancelling 本身是相位而非旁路标志）；
   - 逐商品状态扩为 pending/running/completed/needs_review/
     retryable_failed/permanent_failed/cancelled，另记处理阶段 stage
     （material→…→persist_result）、error_kind、result_ref；attempt=
     失败次数（claim 不计，暂停恢复不烧重试预算）；
   - create_task 带 source（操作来源）与 idem_key（幂等请求键，唯一
     索引：重复提交返回已有任务）+ 活跃重叠守卫（同店铺×站点×env 下
     同一 PID 不得同时挂两个活跃任务——单写者事务内判定，多进程下由
     BEGIN IMMEDIATE 串行化）；
   - claim_item/set_item_stage/cas_task_phase/requeue_retryable/
     task_item_counts 供编排层（模块 7 tasks.py）原子驱动；
   - v2→v3 迁移：done→completed（有 failed/skipped 条目者
     completed_with_issues）、failed→permanent_failed、skipped→cancelled；
     新列全部 ALTER 追加，旧行 stage/error_kind/result_ref 为 NULL。
     回滚策略：本迁移只加列改值不删表——回滚到 v2 程序仍可读（新列
     被忽略，新枚举值对旧程序未知但 SQLite 不校验枚举）；列值重映射
     不可逆，但旧程序只在写入时校验，读出的未知值只影响展示。
11. 属性值层（2026-10-19 S1）v5→v6：新增 pt_attribute 表（纯新增，
   不动既有表结构）。模型/状态机见 pt.attribute；铁律：A/B/C/D 是
   证据分级≠官方校验状态；confirmed 只经人工路径（set_review_status
   显式 human=True）；assignment 被顶替时同事务自动把其 active 属性
   置 stale（历史保留不删除，stale_reason 记原因）；Schema 缓存失效
   的属性失效走显式 invalidate_attributes_for_schema（模块 6 逻辑
   本轮不动）。回滚策略：v6 库被 v5 程序打开会因 user_version=6>5
   被拒（fail-closed，绝不误读）——回滚前先备份 pt.sqlite3 或换
   WZD_PT_DATA 新目录；v6 迁移幂等可重跑（全部 IF NOT EXISTS）。
12. 刊登任务层（2026-10-20 S2c）v6→v7：新增 pt_listing_task /
   pt_listing_item 两张表（纯新增，不动既有表结构——沿用 v4→v5、
   v5→v6 的纯新增迁移先例）。与 pt_task/pt_task_item（分类任务）
   严格分表：生命周期、权限闸、活跃重叠守卫（分类=同 pid，刊登=同
   SKU）、条目粒度（分类=pid，刊登=实际 SKU 含父子变体角色）互不
   干扰。铁律（S2c 验收令）：本阶段刊登任务只允许 env='mock'（建
   任务层由适配层硬闸，live 一律 403 真实通道未接入）；条目快照
   （商品内容指纹/assignment/schema/属性修订）在创建时固化，执行前
   重检，不一致 → blocked（绝不静默续跑）；已受理提交
   （submission_id 已落库）绝不盲目重试提交；Mock 提交结果只代表
   模拟，绝不冒充官方 Listings 结论。回滚策略：v7 库被 v6 程序打开
   会因 user_version=7>6 被拒（fail-closed）——回滚前先备份
   pt.sqlite3 或换 WZD_PT_DATA 新目录；v7 迁移幂等可重跑（全部
   IF NOT EXISTS）。
13. 校验与复核层（2026-10-20 S2d）v7→v8：新增 pt_validation（校验
   报告快照，is_current 单活、旧报告 superseded 保留历史）/
   pt_review_exception（七类异常复核队列，open→resolved|
   auto_superseded）两张表（纯新增，不动既有表）。铁律（S2d 验收令）：
   引擎只开异常绝不 resolve——resolved 行必须带操作人/时间/原值/新值
   审计四件套，只能经人工授权 API 写入（resolve_review_exception 是
   唯一入口，引擎路径无权触碰）；异常按 (pid,sku,env,type,path) 键
   upsert——重复校验刷新 detail 不堆积，消失的类型自动 auto_superseded
   关闭（resolved 是历史事实，永不被引擎改写）；validated 只是本地
   结论，绝不等于亚马逊平台最终接受。回滚策略：v8 库被 v7 程序打开
   会因 user_version=8>7 被拒（fail-closed）——回滚前先备份
   pt.sqlite3 或换 WZD_PT_DATA 新目录；v8 迁移幂等可重跑。

数据库位置：默认 %LOCALAPPDATA%\\WZDesktop\\pt\\pt.sqlite3（代码更新/
robocopy 部署不会覆盖），环境变量 WZD_PT_DATA 可改根目录；测试直接
传显式路径。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from pt.attribute import (
    MAX_ATTRIBUTES_PER_CALL, MAX_SOURCE_REFERENCE_LEN,
    check_attribute_path, check_review_transition, check_sku,
    check_source, check_stale_reason, check_validation,
    check_value_json,
)
# S2d：异常词表以校验引擎（pt.validate）为唯一真源——store 只引用不复制
from pt.validate import EXCEPTION_TYPES, RESOLUTION_ACTIONS

_LOG = logging.getLogger("pt.store")   # 模块 2 要求 11：异常必须可诊断

SCHEMA_VERSION = 10

# ---- 合法枚举（集中定义，写入前一律校验） ----
VALID_EVIDENCE = {"pending", "confirmed", "missing", "insufficient"}
VALID_FIT = {"pending", "fit", "mismatch", "unknown"}
VALID_CHECK = {"pt_exists", "semantic", "browse_node", "attribute"}
VALID_VSTATUS = {"pending", "passed", "failed", "insufficient",
                 "skipped", "error",
                 # 模块 5（2026-10-12 批复）：验证失败/证据不足/接口不可用/
                 # 权限不足必须四态可分——unavailable=网络/5xx/429/解析失败，
                 # forbidden=401/403（拿不到官方结论 ≠ 官方否定）
                 "unavailable", "forbidden"}
# Mock 验证 ≠ 真实官方验证（铁律）；修正 3：数据行环境隔离。
# 修正 1（2026-10-12 第二轮批复）：v1 历史数据的隔离区取值 legacy_unverified
# ——来源无法证明，不得自动获得 live 官方验证资格。仅迁移写入与显式
# 审计读取使用（业务写入仍只有 mock|live）。
LEGACY_ENV = "legacy_unverified"
VALID_AGAINST = {"mock", "live", LEGACY_ENV}
VALID_ENV = {"mock", "live", LEGACY_ENV}


def _check_env(env: str) -> str:
    if env not in VALID_ENV:
        raise ValueError(f"非法 env: {env!r}（{sorted(VALID_ENV)}）")
    return env
# 模块 7（v3）：任务整体状态（控制请求即相位：pausing/cancelling 是
# 「已请求、等在途商品到检查点」的中间相位；completed*/failed/cancelled
# 为终态）。活跃相位集合=尚未终态且仍拥有未完成条目的任务。
VALID_TASK_PHASE = {"created", "running", "pausing", "paused",
                    "completed", "completed_with_issues", "failed",
                    "interrupted", "cancelling", "cancelled"}
ACTIVE_TASK_PHASES = ("created", "running", "pausing", "paused",
                      "interrupted", "cancelling")
# 模块 7（v3）：逐商品状态；attempt=失败次数（retryable_failed 时 +1）。
VALID_ITEM_STATE = {"pending", "running", "completed", "needs_review",
                    "retryable_failed", "permanent_failed", "cancelled"}
# 逐商品处理阶段（与状态两个维度，模块 7 批复二·C）
VALID_ITEM_STAGE = {"material", "candidate_search", "ai_matching",
                    "pt_verification", "browse_node_verification",
                    "schema_analysis", "persist_result"}
# 进度分子=已结束本轮处理的商品（全部非 pending/running 状态）
ITEM_FINISHED_STATES = ("completed", "needs_review", "retryable_failed",
                        "permanent_failed", "cancelled")
VALID_CHOSEN_BY = {"ai", "human"}

# ---- S2c（v7）：刊登任务枚举（与分类任务分表分词表，绝不混用）----
# 任务相位沿用分类的十态词表（CAS/暂停/取消/恢复机制同构），但写在
# 刊登表里、由刊登专用方法驱动。
VALID_LISTING_SOURCE_MODE = {"single", "selected", "category"}
# 条目状态：与分类七态的差异=去掉 needs_review（那是 AI 复核位），
# 增加 blocked（前置条件不满足：属性未确认/冲突/缺失、快照失效——
# 人工修复后可经 retry 重检，不是机械失败）。
VALID_LISTING_ITEM_STATE = {"pending", "running", "blocked", "completed",
                            "retryable_failed", "permanent_failed",
                            "cancelled"}
LISTING_ITEM_FINISHED_STATES = ("completed", "blocked", "retryable_failed",
                                "permanent_failed", "cancelled")
# 条目处理阶段（验收令五阶段；ptd_check=结构校验+官方验证记录核对，
# 绝不冒充官方 Listings 校验通过）。
VALID_LISTING_ITEM_STAGE = {"precheck", "attributes", "ptd_check",
                            "mock_submit", "terminal"}
VALID_VARIANT_ROLE = {"standalone", "parent", "child"}

# ---- S3-B2（v9）：平台结果契约（与本地执行状态严格两维）----
# platform_status 只描述「平台侧回执处于什么阶段」，与条目 state
# （本地执行/校验状态机）互不覆盖：blocked(result_unknown) 的条目
# platform_status=unknown=已受理但结果未知，绝不自动视为失败重试。
# 语义边界（真接入时不得混用）：
#   listings_items（同步受理）：acknowledged→processing→accepted|rejected
#     |partially_accepted（一次提交覆盖多 SKU 时才可能出现）；
#   feeds（异步回执）：acknowledged→processing（feed 处理中）→
#     accepted|rejected|partially_accepted（回执汇总级）。
# source 标记回执来源；mock 来源在任何展示层都必须带模拟标注。
VALID_PLATFORM_STATUS = ("not_submitted", "submitting", "acknowledged",
                         "processing", "accepted", "rejected",
                         "partially_accepted", "unknown")
VALID_PLATFORM_SOURCE = {"mock", "listings_items", "feeds"}

# ---- S2d（v8）：异常复核队列枚举（词表真源=pt.validate，见上）----
# 异常行生命周期：open（引擎/人工开出）→ resolved（人工处置，审计
# 四件套齐）| auto_superseded（后续校验不再出现该异常，引擎关闭——
# 只关 open 行，resolved 是历史事实永不改写）。
VALID_EXCEPTION_STATUS = {"open", "resolved", "auto_superseded"}
VALID_EXCEPTION_ORIGIN = {"engine", "human"}   # 谁开的异常（不是谁解决的）
VALID_EXCEPTION_TYPES = set(EXCEPTION_TYPES)
VALID_RESOLUTION_ACTION = set(RESOLUTION_ACTIONS)


class TaskConflictError(Exception):
    """活跃任务重叠/幂等冲突（模块 7 批复四）：同店铺×站点×env 下
    同一 PID 已属于另一个活跃任务，或任务创建与并发约束冲突。
    编排层据此区分「用户可见的冲突」与「存储层故障」。"""

class StaleTaskError(Exception):
    """幂等命中但内容已变化（S3-B2 P1-1）：同请求体重建任务时，旧
    任务已终态且商品内容/属性/assignment/Schema/集合指纹与当前不同
    ——不得静默复用旧任务（旧快照是历史事实）。调用方应提示用户显式
    发起新一轮（new_operation=True）。"""

class LeaseLostError(Exception):
    """任务执行租约失效（S3-B2 P1-2）：执行者续租失败（已被接管或
    租约过期），此后不得再写该任务的任何状态或提交结果。"""


def pid_alive(pid: int) -> bool:
    """owner 进程是否仍活着（租约未过期时的崩溃恢复快速通道）。

    执行租约 owner 形如 "<pid>-<uuid>"：recover 对「租约仍有效」的
    任务先查 owner 进程死活——死进程立刻可恢复（不等 TTL），活进程
    保留任务（多实例互斥）。误判方向安全：误判「活」只会多等 TTL
    过期再接管，绝不产生双执行；误判「死」也无害，因为 renew-then-
    write 协议下旧 owner 续租失败即静默退出、绝不再写。
    Windows：OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)+
    GetExitCodeProcess（拒绝访问=进程在，保守按活）；其余平台
    os.kill(pid, 0)。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFO
        if not h:
            # ERROR_ACCESS_DENIED(5)=进程存在但权限不足 → 按活（保守）
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True                 # 查不到退出码：按活（保守）
            return int(code.value) == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True                 # 权限不足等：按活（保守）


def lease_holds(owner: str, expires: float, ts: float) -> bool:
    """此刻租约是否仍构成持有（owner 非空 + 未过期 + owner 进程活着；
    owner 解析不出 pid 的历史串按持有处理——保守等 TTL 过期）。"""
    if not owner:
        return False
    try:
        exp = float(expires or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if exp <= ts:
        return False
    head = str(owner).split("-", 1)[0]
    if not head.isdigit():
        return True                   # 无法判定：保守按持有
    return pid_alive(int(head))


def now() -> int:
    return int(time.time())


def data_dir_default() -> Path:
    """持久化数据目录：绝不在代码目录里（robocopy 更新会覆盖）。"""
    env = str(os.environ.get("WZD_PT_DATA") or "").strip()
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "WZDesktop" / "pt"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pt_meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 概念①：ProductType 字典（官方公共数据；站点维度隔离，跨店共享；
-- env 隔离：mock 检索结果绝不混进 live 字典）
CREATE TABLE IF NOT EXISTS pt_type_dict(
  marketplace_id TEXT NOT NULL,
  pt_name        TEXT NOT NULL,
  display_name   TEXT,
  locale         TEXT,
  source         TEXT NOT NULL DEFAULT 'search',
  env            TEXT NOT NULL DEFAULT 'live',
  first_seen_at  INTEGER NOT NULL,
  last_seen_at   INTEGER NOT NULL,
  PRIMARY KEY(marketplace_id, pt_name, env)
);

-- 概念③：ProductTypeSchema 元数据（payload 存 pt/schemas/ 文件，库里
-- 只放元数据+哈希；cache_key 覆盖全部影响响应的参数与卖家上下文
-- + env——mock schema 绝不给 live 复用）
CREATE TABLE IF NOT EXISTS pt_schema_meta(
  cache_key              TEXT PRIMARY KEY,
  marketplace_id         TEXT NOT NULL,
  pt_name                TEXT NOT NULL,
  seller_id              TEXT NOT NULL DEFAULT '',
  locale                 TEXT NOT NULL DEFAULT '',
  requirements           TEXT NOT NULL DEFAULT '',
  requirements_enforced  TEXT NOT NULL DEFAULT '',
  parentage_level        TEXT NOT NULL DEFAULT '',
  product_type_version   TEXT NOT NULL DEFAULT '',
  payload_sha256         TEXT,
  payload_path           TEXT,
  status                 TEXT NOT NULL DEFAULT 'pending',
  env                    TEXT NOT NULL DEFAULT 'live',
  fetched_at             INTEGER
);

-- 概念②：BrowseNode（与 PT 完全分表；证据分级，见文件头；env 分行：
-- mock catalog 证据不冒充官方证据）
CREATE TABLE IF NOT EXISTS pt_browse_node(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  marketplace_id   TEXT NOT NULL,
  node_id          TEXT NOT NULL,
  source           TEXT NOT NULL DEFAULT 'btmap',
  evidence_status  TEXT NOT NULL DEFAULT 'pending',
  evidence_kind    TEXT,
  fit_status       TEXT NOT NULL DEFAULT 'pending',
  display_name     TEXT,
  context_path     TEXT,
  evidence_json    TEXT,
  env              TEXT NOT NULL DEFAULT 'live',
  checked_at       INTEGER,
  UNIQUE(marketplace_id, node_id, source, env)
);

-- 产品维度（store_id × marketplace_id × env 三重隔离）----
CREATE TABLE IF NOT EXISTS pt_candidate(
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id       TEXT NOT NULL DEFAULT '',
  marketplace_id TEXT NOT NULL,
  pid            TEXT NOT NULL,
  task_id        TEXT NOT NULL DEFAULT '',
  strategy       TEXT NOT NULL,
  pt_name        TEXT NOT NULL,
  display_name   TEXT,
  score          REAL,
  evidence_json  TEXT,
  env            TEXT NOT NULL DEFAULT 'live',
  retrieved_at   INTEGER NOT NULL,
  UNIQUE(store_id, marketplace_id, pid, pt_name, strategy, env)
);

CREATE TABLE IF NOT EXISTS pt_assignment(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id         TEXT NOT NULL DEFAULT '',
  marketplace_id   TEXT NOT NULL,
  pid              TEXT NOT NULL,
  pt_name          TEXT NOT NULL,
  schema_cache_key TEXT,
  chosen_by        TEXT NOT NULL,
  confidence       REAL,
  reason           TEXT,
  env              TEXT NOT NULL DEFAULT 'live',
  is_current       INTEGER NOT NULL DEFAULT 0,
  superseded_by    INTEGER,
  created_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assignment_current
  ON pt_assignment(store_id, marketplace_id, pid, env, is_current);

CREATE TABLE IF NOT EXISTS pt_verification(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id         TEXT NOT NULL DEFAULT '',
  marketplace_id   TEXT NOT NULL,
  pid              TEXT NOT NULL,
  pt_name          TEXT,
  node_id          TEXT,
  check_kind       TEXT NOT NULL,
  status           TEXT NOT NULL,
  detail_json      TEXT,
  verified_against TEXT NOT NULL,
  checked_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verification
  ON pt_verification(store_id, marketplace_id, pid, check_kind, id);

-- v5（P5 人工复核证据链）：needs_review 时保存结构化复核证据（AI
-- 建议/置信度/逐候选评分明细/复核原因/当时官方验证快照）。它只是
-- **建议记录**，与 pt_assignment（正式分配，completed 才写）严格
-- 分离——绝不把待复核建议冒充已确认分类；复核 UI 读这张表，不解析
-- gpt_evidence.jsonl。
CREATE TABLE IF NOT EXISTS pt_review_evidence(
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id           TEXT NOT NULL DEFAULT '',
  marketplace_id     TEXT NOT NULL,
  pid                TEXT NOT NULL,
  task_id            TEXT NOT NULL DEFAULT '',
  env                TEXT NOT NULL DEFAULT 'live',
  stage              TEXT NOT NULL DEFAULT '',
  error_kind         TEXT NOT NULL DEFAULT '',
  review_reasons_json TEXT,
  ai_chosen          TEXT,
  ai_confidence      REAL,
  ai_detail_json     TEXT,
  official_status_json TEXT,
  created_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_evidence
  ON pt_review_evidence(store_id, marketplace_id, pid, env, id);

CREATE TABLE IF NOT EXISTS pt_correction(
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id            TEXT NOT NULL DEFAULT '',
  marketplace_id      TEXT NOT NULL,
  pid                 TEXT NOT NULL,
  from_pt             TEXT,
  to_pt               TEXT NOT NULL,
  from_assignment_id  INTEGER,
  by_user             TEXT NOT NULL DEFAULT '',
  note                TEXT,
  created_at          INTEGER NOT NULL
);

-- 任务（修正 2：逐商品条目；last_pid 仅便利字段，恢复不依赖它；
-- env：任务在哪个环境跑的，mock 任务与 live 任务互不可见混淆；
-- 模块 7：source=操作来源，idem_key=幂等请求键（重复提交返回已有任务））
CREATE TABLE IF NOT EXISTS pt_task(
  id             TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,
  store_id       TEXT NOT NULL DEFAULT '',
  marketplace_id TEXT NOT NULL,
  env            TEXT NOT NULL DEFAULT 'live',
  phase          TEXT NOT NULL DEFAULT 'created',
  progress_done  INTEGER NOT NULL DEFAULT 0,
  progress_total INTEGER NOT NULL DEFAULT 0,
  last_pid       TEXT,
  error          TEXT,
  source         TEXT NOT NULL DEFAULT '',
  idem_key       TEXT,
  resume_pending INTEGER NOT NULL DEFAULT 0,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  finished_at    INTEGER,
  perf_json      TEXT
);
CREATE TABLE IF NOT EXISTS pt_task_item(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  pid        TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'pending',
  attempt    INTEGER NOT NULL DEFAULT 0,
  stage      TEXT,
  error_kind TEXT,
  error      TEXT,
  result_ref TEXT,
  updated_at INTEGER NOT NULL,
  material_fp  TEXT,
  ai_pt        TEXT,
  ai_confidence REAL,
  schema_ok    INTEGER NOT NULL DEFAULT 0,
  attr_status  TEXT,
  UNIQUE(task_id, pid)
);
CREATE INDEX IF NOT EXISTS idx_task_item
  ON pt_task_item(task_id, state);
"""

# 概念⑨（v6/S1）：PTD 属性值表（模型/状态机见 pt.attribute）。
# 每行=一个属性路径在某个 (store,marketplace,env,pid) 作用域下的一次
# 修订；is_current=1 且 lifecycle='active' 的行是唯一「当前有效值」
# （uq_attr_current 部分唯一索引强制），其余行全量保留为历史链
# （superseded_by 串修订；stale=上下文失效，终态但绝不删除）。
# 值绑定分类与 Schema 上下文：assignment_id / pt_name / schema_cache_key /
# schema_version 在写入时快照，上下文变化由 lifecycle 失效表达，不改写历史。
_ATTRIBUTE_SQL = """
CREATE TABLE IF NOT EXISTS pt_attribute(
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            TEXT NOT NULL,
  store_id           TEXT NOT NULL,
  marketplace_id     TEXT NOT NULL,
  env                TEXT NOT NULL,
  pid                TEXT NOT NULL,
  sku                TEXT NOT NULL DEFAULT '',
  assignment_id      INTEGER NOT NULL,
  pt_name            TEXT NOT NULL DEFAULT '',
  schema_cache_key   TEXT NOT NULL DEFAULT '',
  schema_version     TEXT NOT NULL DEFAULT '',
  attribute_path     TEXT NOT NULL,
  value_json         TEXT NOT NULL,
  source_grade       TEXT NOT NULL,
  source_type        TEXT NOT NULL,
  source_reference   TEXT NOT NULL DEFAULT '',
  confidence         REAL NOT NULL DEFAULT 0,
  validation_status  TEXT NOT NULL DEFAULT 'pending',
  validation_errors_json TEXT NOT NULL DEFAULT '[]',
  review_status      TEXT NOT NULL DEFAULT 'proposed',
  lifecycle          TEXT NOT NULL DEFAULT 'active',
  stale_reason       TEXT NOT NULL DEFAULT '',
  superseded_by      INTEGER,
  is_current         INTEGER NOT NULL DEFAULT 1,
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_scope_current
  ON pt_attribute(store_id, marketplace_id, env, pid, is_current,
                  lifecycle);
CREATE INDEX IF NOT EXISTS idx_attr_assignment
  ON pt_attribute(assignment_id, is_current);
CREATE UNIQUE INDEX IF NOT EXISTS uq_attr_current
  ON pt_attribute(store_id, marketplace_id, env, pid, attribute_path)
  WHERE is_current=1 AND lifecycle='active';
"""

# 概念⑩（v7/S2c）：刊登任务表（引擎见 pt.listing）。
# pt_listing_task=一次统一刊登任务（三种来源 single/selected/category
# 共用一套模型；category 的商品集合在创建时固化进条目——snapshot_hash
# 锁定，之后分类移动绝不悄悄加商品）。pt_listing_item=以实际 SKU 为
# 单位的条目：父子变体关系（parent_sku/variant_role）逐 SKU 展开；
# 创建时固化上下文快照（content_sha 商品内容指纹 / assignment_id /
# schema_cache_key+schema_version / attr_revision 属性修订指纹），
# 执行前重检不一致 → blocked。submission_id 落库=已受理，绝不盲目
# 重试（claim 只领 pending，retry 不动 completed）。
_LISTING_SQL = """
CREATE TABLE IF NOT EXISTS pt_listing_task(
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL,
  store_id       TEXT NOT NULL DEFAULT '',
  marketplace_id TEXT NOT NULL,
  env            TEXT NOT NULL DEFAULT 'mock',
  source_mode    TEXT NOT NULL,
  source_ref     TEXT NOT NULL DEFAULT '',
  snapshot_hash  TEXT NOT NULL DEFAULT '',
  snapshot_n     INTEGER NOT NULL DEFAULT 0,
  phase          TEXT NOT NULL DEFAULT 'created',
  last_pid       TEXT,
  error          TEXT,
  source         TEXT NOT NULL DEFAULT '',
  idem_key       TEXT,
  idem_fp        TEXT NOT NULL DEFAULT '',   -- v9：创建时内容指纹（幂等新鲜度判定）
  resume_pending INTEGER NOT NULL DEFAULT 0,
  lease_owner    TEXT NOT NULL DEFAULT '',   -- v9：执行租约持有者（跨实例互斥）
  lease_expires  REAL NOT NULL DEFAULT 0,    -- v9：租约到期（epoch 秒；0=无主）
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  finished_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listing_task_scope
  ON pt_listing_task(store_id, marketplace_id, env, phase);
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_task_idem
  ON pt_listing_task(idem_key) WHERE idem_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS pt_listing_item(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id          TEXT NOT NULL,
  pid              TEXT NOT NULL,
  sku              TEXT NOT NULL,
  parent_sku       TEXT NOT NULL DEFAULT '',
  variant_role     TEXT NOT NULL DEFAULT 'standalone',
  content_sha      TEXT NOT NULL DEFAULT '',
  assignment_id    INTEGER,
  pt_name          TEXT NOT NULL DEFAULT '',
  schema_cache_key TEXT NOT NULL DEFAULT '',
  schema_version   TEXT NOT NULL DEFAULT '',
  attr_revision    TEXT NOT NULL DEFAULT '',
  stage            TEXT,
  state            TEXT NOT NULL DEFAULT 'pending',
  attempt          INTEGER NOT NULL DEFAULT 0,
  error_kind       TEXT,
  error            TEXT,
  check_json       TEXT NOT NULL DEFAULT '',
  submission_id    TEXT,
  submitted_at     INTEGER,
  submit_result_json TEXT,
  platform_status  TEXT NOT NULL DEFAULT 'not_submitted',  -- v9 平台回执阶段
  platform_checked_at INTEGER,                             -- v9 最后核对时间
  platform_json    TEXT,       -- v9 契约载荷：{source,request_id,response_id,submitted_at,issues[]…}
  updated_at       INTEGER NOT NULL,
  UNIQUE(task_id, pid, sku)
);
CREATE INDEX IF NOT EXISTS idx_listing_item
  ON pt_listing_item(task_id, state);
CREATE INDEX IF NOT EXISTS idx_listing_item_scope
  ON pt_listing_item(task_id, pid);
"""

# S2d（v8）：校验报告快照 + 异常复核队列。pt_validation 按商品×SKU
# 存「最后一次完整校验」的报告全文（is_current 单活；指纹列与
# pt_listing_item 快照同源——内容/分配/Schema/属性修订任一变化都该
# 触发重校验并开新报告）。pt_review_exception=七类异常的工单化：
# 键=(store,marketplace,pid,sku,env,type,path)，引擎 upsert open 行，
# 人工 resolve 落审计四件套（resolved_by/resolved_at/original_value/
# resolved_value + note）——引擎路径无权写 resolved。
_VALIDATION_SQL = """
CREATE TABLE IF NOT EXISTS pt_validation(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id         TEXT NOT NULL DEFAULT '',
  marketplace_id   TEXT NOT NULL,
  pid              TEXT NOT NULL,
  sku              TEXT NOT NULL DEFAULT '',
  env              TEXT NOT NULL DEFAULT 'mock',
  content_sha      TEXT NOT NULL DEFAULT '',
  assignment_id    INTEGER,
  pt_name          TEXT NOT NULL DEFAULT '',
  schema_cache_key TEXT NOT NULL DEFAULT '',
  schema_version   TEXT NOT NULL DEFAULT '',
  attr_revision    TEXT NOT NULL DEFAULT '',
  validated        INTEGER NOT NULL DEFAULT 0,
  exception_types  TEXT NOT NULL DEFAULT '',
  report_json      TEXT NOT NULL,
  is_current       INTEGER NOT NULL DEFAULT 1,
  superseded_by    INTEGER,
  source           TEXT NOT NULL DEFAULT '',
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validation_current
  ON pt_validation(store_id, marketplace_id, pid, sku, env, is_current);

CREATE TABLE IF NOT EXISTS pt_review_exception(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id         TEXT NOT NULL DEFAULT '',
  marketplace_id   TEXT NOT NULL,
  pid              TEXT NOT NULL,
  sku              TEXT NOT NULL DEFAULT '',
  env              TEXT NOT NULL DEFAULT 'mock',
  exception_type   TEXT NOT NULL,
  attribute_path   TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'open',
  origin           TEXT NOT NULL DEFAULT 'engine',
  detail_json      TEXT NOT NULL,
  source           TEXT NOT NULL DEFAULT '',
  resolution_action TEXT,
  original_value   TEXT,
  resolved_value   TEXT,
  resolved_by      TEXT NOT NULL DEFAULT '',
  resolved_at      INTEGER,
  resolution_note  TEXT,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_exc_queue
  ON pt_review_exception(store_id, marketplace_id, env, status,
                         exception_type);
CREATE INDEX IF NOT EXISTS idx_review_exc_pid
  ON pt_review_exception(pid, sku, env, status);
"""

_SCHEMA_SQL = _SCHEMA_SQL + _ATTRIBUTE_SQL + _LISTING_SQL \
    + _VALIDATION_SQL

# idx_task_idem 单独建（不在 _SCHEMA_SQL 里）：它引用 v3 才加的 idem_key
# 列——v1→v2 的收尾 executescript 跑在 v3 ALTER 之前，脚本里带它会在
# 真实老库上 OperationalError（no such column）。列就位后统一执行
# _ensure_idem_index()（幂等）。
_TASK_IDEM_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_idem"
    " ON pt_task(idem_key) WHERE idem_key IS NOT NULL")


def _ensure_idem_index(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(pt_task)")}
    if "idem_key" in cols:
        db.execute(_TASK_IDEM_INDEX_SQL)


# 索引除 idx_task_idem 外全部在 _SCHEMA_SQL 里（CREATE INDEX IF NOT
# EXISTS）。v1→v2 迁移必须**先**给旧表补 env 列/重建，**最后**再跑
# executescript 建索引——索引引用 env 列，v1 旧表（pt_assignment 等）
# 还没有它，先跑脚本会直接 OperationalError（真实老库的坑，
# 2026-10-12 二轮修正）。


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


class PtStore:
    """单写者 SQLite 存储：模块内唯一连接 + RLock，WAL + 短事务。

    注意：公开方法之间不得在事务内互调（_tx 不可嵌套）。
    """

    def __init__(self, db_path: str | os.PathLike):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=10.0,
        )
        self._db.row_factory = sqlite3.Row
        self._db.isolation_level = None        # 纯自动提交 + 手动事务
        # busy_timeout 必须先行：journal_mode 切换要独占锁，多进程/多连接
        # 同时打开同一库时切换会撞锁（且该路径不吃 connect 的 busy 超时）
        self._db.execute("PRAGMA busy_timeout=5000")
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as e:
            # 并发打开竞态：若库已是 WAL（另一连接刚切完），本次打开照常
            # 可用；确实拿不到 WAL 才是故障。
            mode = self._db.execute(
                "PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                raise
            _LOG.warning(
                "pt.store init: journal_mode=WAL 并发竞态（库已是 WAL，"
                "继续）db=%s err=%r", self.path, e)
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # ---- 基础设施 ----

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as e:
                # 模块 2 要求 11：checkpoint 失败必须留下可诊断日志（数据
                # 仍在 WAL，重开库会自动恢复，所以只告警不阻断关闭）。
                _LOG.warning(
                    "pt.store close: wal_checkpoint 失败 db=%s err=%r（数据"
                    "保留在 WAL，下次打开自动恢复）", self.path, e,
                    exc_info=True)
            self._db.close()

    def __enter__(self) -> "PtStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            v = self._db.execute("PRAGMA user_version").fetchone()[0]
            if v > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema v{v} 比程序支持的 v{SCHEMA_VERSION} 新，"
                    "拒绝打开（先用新版程序或换数据目录）"
                )
            if v == SCHEMA_VERSION:
                return
            if v == 0:
                self._db.executescript(_SCHEMA_SQL)  # 全 IF NOT EXISTS 可重跑
                _ensure_idem_index(self._db)         # 新库：列已带 idem_key
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
            if v == 1:
                # v1→v2（修正 3）：env 隔离列。三张带唯一键的表重建
                # （唯一键必须含 env），三张 ALTER 加列；旧行一律落
                # legacy_unverified 隔离区（修正 1：历史来源无法证明，
                # 不得自动获得 live 官方资格）。
                self._migrate_1_to_2()
                self._db.execute("PRAGMA user_version=2")
            if v in (1, 2):
                # v2→v3（模块 7）：任务词表/新列/幂等键（见文件头 10）。
                self._migrate_2_to_3()
                self._db.execute("PRAGMA user_version=3")
            if v in (1, 2, 3):
                # v3→v4（集成加固）：pausing 窗口 resume 的持久化意图列。
                self._migrate_3_to_4()
            if v in (1, 2, 3, 4):
                # v4→v5（P5）：人工复核证据链表（纯新增，无列变更）。
                self._migrate_4_to_5()
            if v in (1, 2, 3, 4, 5):
                # v5→v6（S1）：pt_attribute 属性值表（纯新增，无列变更）。
                self._migrate_5_to_6()
            if v in (1, 2, 3, 4, 5, 6):
                # v6→v7（S2c）：刊登任务表（纯新增，无列变更）。
                self._migrate_6_to_7()
                self._db.execute("PRAGMA user_version=7")
            if v in (1, 2, 3, 4, 5, 6, 7):
                # v7→v8（S2d）：校验报告+异常复核表（纯新增，无列变更）。
                self._migrate_7_to_8()
                self._db.execute("PRAGMA user_version=8")
            if v in (1, 2, 3, 4, 5, 6, 7, 8):
                # v8→v9（S3-B2）：幂等内容指纹+执行租约+平台结果契约列。
                # 纯加列（旧行取默认值，历史事实不动）；建列与 user_version
                # 分两步，中途崩溃重开按缺列补齐（幂等可重跑）。
                self._migrate_8_to_9()
                self._db.execute("PRAGMA user_version=9")
            if v in (1, 2, 3, 4, 5, 6, 7, 8, 9):
                # v9→v10（D1.12.2）：分类条目阶段状态列（material_fp/
                # ai_pt/ai_confidence/schema_ok/attr_status——六态可见与
                # 阶段级续跑依据）+ pt_task.perf_json（阶段耗时/请求计数
                # 审计，纯加列旧行默认，历史零改动；缺哪补哪可重跑）。
                self._migrate_9_to_10()
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
            raise RuntimeError(f"未知 schema 版本 v{v}")

    def _migrate_5_to_6(self) -> None:
        """v5→v6（S1）：pt_attribute 属性值表（概念⑨）。

        纯新增（CREATE IF NOT EXISTS 幂等，可重跑/崩溃恢复：建表与
        user_version 分两步，中途崩溃重开再跑一遍即可）。不动既有表。
        """
        self._db.executescript(_ATTRIBUTE_SQL)

    def _migrate_6_to_7(self) -> None:
        """v6→v7（S2c）：pt_listing_task / pt_listing_item（概念⑩）。

        纯新增（CREATE IF NOT EXISTS 幂等，可重跑/崩溃恢复：建表与
        user_version 分两步，中途崩溃重开再跑一遍即可）。不动既有表
        ——分类任务、属性值、验证记录零改动。
        """
        self._db.executescript(_LISTING_SQL)

    def _migrate_7_to_8(self) -> None:
        """v7→v8（S2d）：pt_validation / pt_review_exception。

        纯新增（CREATE IF NOT EXISTS 幂等，可重跑/崩溃恢复：建表与
        user_version 分两步，中途崩溃重开再跑一遍即可）。不动既有表
        ——刊登任务、属性值、验证记录零改动；既有库没有任何存量校验
        报告，不开迁移异常（下次校验自然落行）。
        """
        self._db.executescript(_VALIDATION_SQL)

    def _migrate_8_to_9(self) -> None:
        """v8→v9（S3-B2）：刊登表加列（幂等内容指纹/执行租约/平台
        结果契约）。纯新增列、旧行取默认值——历史快照与 Mock 提交
        记录零改动；读取层对旧 platform_status='not_submitted' 行按
        state/submit_result 推导展示（见 platform_view），不回填。
        列按 PRAGMA table_info 判断缺哪补哪（崩溃重跑幂等）。
        """
        task_cols = {"idem_fp": "TEXT NOT NULL DEFAULT ''",
                     "lease_owner": "TEXT NOT NULL DEFAULT ''",
                     "lease_expires": "REAL NOT NULL DEFAULT 0"}
        item_cols = {"platform_status":
                         "TEXT NOT NULL DEFAULT 'not_submitted'",
                     "platform_checked_at": "INTEGER",
                     "platform_json": "TEXT"}
        for table, cols in (("pt_listing_task", task_cols),
                            ("pt_listing_item", item_cols)):
            have = {r[1] for r in self._db.execute(
                f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in cols.items():
                if name not in have:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_9_to_10(self) -> None:
        """v9→v10（D1.12.2）：分类任务阶段状态列（见 _migrate 阶梯注释）。
        纯加列、旧行取默认值；按 PRAGMA table_info 缺哪补哪（幂等）。"""
        item_cols = {"material_fp": "TEXT",
                     "ai_pt": "TEXT",
                     "ai_confidence": "REAL",
                     "schema_ok": "INTEGER NOT NULL DEFAULT 0",
                     "attr_status": "TEXT"}
        task_cols = {"perf_json": "TEXT"}
        for table, cols in (("pt_task_item", item_cols),
                            ("pt_task", task_cols)):
            have = {r[1] for r in self._db.execute(
                f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in cols.items():
                if name not in have:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_4_to_5(self) -> None:
        """v4→v5：pt_review_evidence（needs_review 结构化证据）。
        纯新增表（CREATE IF NOT EXISTS 幂等），不动任何既有表。"""
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS pt_review_evidence(\n"
            "  id                 INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "  store_id           TEXT NOT NULL DEFAULT '',\n"
            "  marketplace_id     TEXT NOT NULL,\n"
            "  pid                TEXT NOT NULL,\n"
            "  task_id            TEXT NOT NULL DEFAULT '',\n"
            "  env                TEXT NOT NULL DEFAULT 'live',\n"
            "  stage              TEXT NOT NULL DEFAULT '',\n"
            "  error_kind         TEXT NOT NULL DEFAULT '',\n"
            "  review_reasons_json TEXT,\n"
            "  ai_chosen          TEXT,\n"
            "  ai_confidence      REAL,\n"
            "  ai_detail_json     TEXT,\n"
            "  official_status_json TEXT,\n"
            "  created_at         INTEGER NOT NULL\n"
            ");\n"
            "CREATE INDEX IF NOT EXISTS idx_review_evidence\n"
            "  ON pt_review_evidence(store_id, marketplace_id, pid,"
            " env, id);")

    def _migrate_3_to_4(self) -> None:
        """v3→v4：pt_task 加 resume_pending（pausing 窗口里收到 resume
        请求时置位；暂停落定即消费并自动续跑——意图持久化在库，进程
        重启也不丢语义）。老库 ALTER 补列，全新库由 _SCHEMA_SQL 直接带。
        消费语义=原子（consume 只有置位方拿得到 True），多次点击不会
        产生重复续跑。"""
        cols = {r[1] for r in self._db.execute(
            "PRAGMA table_info(pt_task)")}
        if "resume_pending" not in cols:
            self._db.execute(
                "ALTER TABLE pt_task ADD COLUMN"
                " resume_pending INTEGER NOT NULL DEFAULT 0")

    def _migrate_1_to_2(self) -> None:
        rebuilds = (
            # (表名, v2 建表 SQL 不含 IF NOT EXISTS, 旧列清单按 v1 顺序)
            ("pt_type_dict",
             "CREATE TABLE pt_type_dict("
             "marketplace_id TEXT NOT NULL, pt_name TEXT NOT NULL,"
             " display_name TEXT, locale TEXT, source TEXT NOT NULL"
             " DEFAULT 'search', env TEXT NOT NULL DEFAULT 'live',"
             " first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,"
             " PRIMARY KEY(marketplace_id, pt_name, env))",
             "marketplace_id,pt_name,display_name,locale,source,"
             "first_seen_at,last_seen_at"),
            ("pt_browse_node",
             "CREATE TABLE pt_browse_node("
             "id INTEGER PRIMARY KEY AUTOINCREMENT, marketplace_id TEXT"
             " NOT NULL, node_id TEXT NOT NULL, source TEXT NOT NULL"
             " DEFAULT 'btmap', evidence_status TEXT NOT NULL DEFAULT"
             " 'pending', evidence_kind TEXT, fit_status TEXT NOT NULL"
             " DEFAULT 'pending', display_name TEXT, context_path TEXT,"
             " evidence_json TEXT, env TEXT NOT NULL DEFAULT 'live',"
             " checked_at INTEGER, UNIQUE(marketplace_id,node_id,source,env))",
             "id,marketplace_id,node_id,source,evidence_status,evidence_kind,"
             "fit_status,display_name,context_path,evidence_json,checked_at"),
            ("pt_candidate",
             "CREATE TABLE pt_candidate("
             "id INTEGER PRIMARY KEY AUTOINCREMENT, store_id TEXT NOT NULL"
             " DEFAULT '', marketplace_id TEXT NOT NULL, pid TEXT NOT NULL,"
             " task_id TEXT NOT NULL DEFAULT '', strategy TEXT NOT NULL,"
             " pt_name TEXT NOT NULL, display_name TEXT, score REAL,"
             " evidence_json TEXT, env TEXT NOT NULL DEFAULT 'live',"
             " retrieved_at INTEGER NOT NULL, UNIQUE(store_id,marketplace_id,"
             "pid,pt_name,strategy,env))",
             "id,store_id,marketplace_id,pid,task_id,strategy,pt_name,"
             "display_name,score,evidence_json,retrieved_at"),
        )
        alters = ("pt_schema_meta", "pt_assignment", "pt_task")
        with self._tx():
            # ① 三张 ALTER 表先补 env 列（存量行=加列默认值 legacy）。
            #    必须在 executescript 之前：脚本里的 idx_assignment_current
            #    引用 env，v1 旧表没有它会直接 OperationalError。
            for name in alters:
                has = self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                    " name=?", (name,)).fetchone()
                if has is None:
                    continue          # v1 库缺表（异常旧库）→ 脚本稍后补
                cols = {r[1] for r in self._db.execute(
                    f"PRAGMA table_info({name})").fetchall()}
                if "env" not in cols:
                    # 加列默认值只作用于「加列当时的存量行」=历史行，
                    # 全部落 legacy 隔离区（修正 1）；此后新写入走方法层
                    # env 参数（缺省 live），不受表级默认影响
                    self._db.execute(
                        f"ALTER TABLE {name} ADD COLUMN env TEXT NOT NULL"
                        f" DEFAULT '{LEGACY_ENV}'")
            # ② 三张唯一键表重建（唯一键必须含 env；旧行落 legacy）
            for name, ddl, old_cols in rebuilds:
                has = self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                    " name=?", (name,)).fetchone()
                if has is None:
                    continue
                self._db.execute(f"ALTER TABLE {name} RENAME TO {name}__v1")
                self._db.execute(ddl)
                self._db.execute(
                    f"INSERT INTO {name}({old_cols},env) SELECT {old_cols},"
                    f" '{LEGACY_ENV}' FROM {name}__v1")
                self._db.execute(f"DROP TABLE {name}__v1")
            # ③ 修正 1（续）：v1 时代的验证记录声称 live 的同样不可信——
            #    降级 legacy_unverified；mock 声称保持 mock（本就诚实）。
            #    异常缺表库无行可降，跳过（脚本稍后补空表）。
            has_v = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                " name='pt_verification'").fetchone()
            if has_v is not None:
                self._db.execute(
                    "UPDATE pt_verification SET verified_against=? WHERE"
                    " verified_against='live'", (LEGACY_ENV,))
        # ④ 建库脚本幂等补齐缺表 + 索引（此刻所有已存在的表都带 env，
        #    索引安全；重建表丢弃的旧索引在这里重建）。idx_task_idem 引用
        #    v3 列 idem_key——列不存在时 _ensure_idem_index 自动跳过
        #    （紧随其后的 v2→v3 迁移补列后会补建）。
        self._db.executescript(_SCHEMA_SQL)
        _ensure_idem_index(self._db)
        _LOG.info("pt.store v1→v2 迁移完成（env 隔离列；旧行与历史"
                  " live 验证记录一律降级 %s，不自动获得官方资格）"
                  " db=%s", LEGACY_ENV, self.path)

    def _migrate_2_to_3(self) -> None:
        """v2→v3（模块 7，文件头 10）：任务词表 + 新列 + 幂等键。
        只 ALTER 加列与改值，绝不删表删列（回滚策略见文件头）。"""
        with self._tx():
            for name, cols in (
                ("pt_task", (("source", "TEXT NOT NULL DEFAULT ''"),
                             ("idem_key", "TEXT"))),
                ("pt_task_item", (("stage", "TEXT"), ("error_kind", "TEXT"),
                                  ("result_ref", "TEXT"))),
            ):
                has = self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND"
                    " name=?", (name,)).fetchone()
                if has is None:
                    continue          # 异常缺表库 → 建库脚本稍后补
                existing = {r[1] for r in self._db.execute(
                    f"PRAGMA table_info({name})").fetchall()}
                for col, decl in cols:
                    if col not in existing:
                        self._db.execute(
                            f"ALTER TABLE {name} ADD COLUMN {col} {decl}")
            # 逐商品旧词表 → v3（attempt 语义改为失败次数：旧 failed 行的
            # attempt 本就按旧 claim 语义累计，迁移按失败次数保留原值）
            self._db.execute(
                "UPDATE pt_task_item SET state='completed'"
                " WHERE state='done'")
            self._db.execute(
                "UPDATE pt_task_item SET state='permanent_failed'"
                " WHERE state='failed'")
            self._db.execute(
                "UPDATE pt_task_item SET state='cancelled'"
                " WHERE state='skipped'")
            # 任务 done → completed / completed_with_issues（按条目真实
            # 状态推导，绝不含糊放行）
            self._db.execute(
                "UPDATE pt_task SET phase='completed' WHERE phase='done'")
            self._db.execute(
                "UPDATE pt_task SET phase='completed_with_issues'"
                " WHERE phase='completed' AND id IN ("
                "  SELECT task_id FROM pt_task_item WHERE task_id IN"
                "  (SELECT id FROM pt_task WHERE phase='completed')"
                "  AND state IN ('needs_review','retryable_failed',"
                "                'permanent_failed','cancelled'))")
        # 幂等键唯一索引 + 缺表补齐（幂等，可重跑；此刻 idem_key 列
        # 已 ALTER 就位）
        self._db.executescript(_SCHEMA_SQL)
        _ensure_idem_index(self._db)
        _LOG.info("pt.store v2→v3 迁移完成（模块 7 任务词表/stage/"
                  "error_kind/result_ref/idem_key；done→completed*、"
                  "failed→permanent_failed、skipped→cancelled）db=%s",
                  self.path)

    def integrity_ok(self) -> bool:
        with self._lock:
            return self._db.execute(
                "PRAGMA integrity_check").fetchone()[0] == "ok"

    # ---- meta ----

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._db.execute(
                "SELECT value FROM pt_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r is not None else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO pt_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # ---- 概念①：ProductType 字典（公共数据，跨店共享） ----

    def upsert_types(
        self, marketplace_id: str, types: Iterable[dict],
        source: str = "search", now_ts: int | None = None,
        env: str = "live",
    ) -> int:
        """types: [{pt_name, display_name?, locale?}]，返回写入条数。
        env（mock|live）：mock 检索的字典与 live 严格分行（修正 3）。"""
        _check_env(env)
        t = now_ts or now()
        n = 0
        with self._tx():
            for r in types or []:
                pt = str(r.get("pt_name") or "").strip()
                if not pt:
                    continue
                self._db.execute(
                    "INSERT INTO pt_type_dict(marketplace_id,pt_name,"
                    "display_name,locale,source,env,first_seen_at,"
                    "last_seen_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT("
                    "marketplace_id,pt_name,env) DO UPDATE SET display_name="
                    "excluded.display_name, locale=excluded.locale, source="
                    "excluded.source, last_seen_at=excluded.last_seen_at",
                    (marketplace_id, pt, r.get("display_name"),
                     r.get("locale"), source, env, t, t),
                )
                n += 1
        return n

    def types_for(self, marketplace_id: str, env: str = "live") -> list[dict]:
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pt_type_dict WHERE marketplace_id=? AND env=?"
                " ORDER BY pt_name", (marketplace_id, env),
            ).fetchall()
        return [dict(r) for r in rows]

    def stale_type_names(
        self, marketplace_id: str, ttl_seconds: int, env: str = "live",
    ) -> list[str]:
        _check_env(env)
        cutoff = now() - int(ttl_seconds)
        with self._lock:
            rows = self._db.execute(
                "SELECT pt_name FROM pt_type_dict WHERE marketplace_id=?"
                " AND env=? AND last_seen_at<?",
                (marketplace_id, env, cutoff),
            ).fetchall()
        return [r[0] for r in rows]

    # ---- 概念③：ProductTypeSchema 元数据 ----

    @staticmethod
    def schema_cache_key(
        marketplace_id: str, pt_name: str, seller_id: str = "",
        locale: str = "", requirements: str = "",
        requirements_enforced: str = "", parentage_level: str = "",
        product_type_version: str = "", env: str = "live",
    ) -> str:
        """缓存键必须覆盖全部影响响应的参数 + 卖家上下文 + env（修正 3：
        mock schema 与 live schema 绝不同键互取）。"""
        _check_env(env)
        parts = (
            marketplace_id, pt_name, seller_id, locale, requirements,
            requirements_enforced, parentage_level, product_type_version,
            "env=" + env,
        )
        return "|".join(str(p or "").strip() for p in parts)

    def upsert_schema_meta(
        self, *, marketplace_id: str, pt_name: str, seller_id: str = "",
        locale: str = "", requirements: str = "",
        requirements_enforced: str = "", parentage_level: str = "",
        product_type_version: str = "", payload_sha256: str | None = None,
        payload_path: str | None = None, status: str = "ok",
        fetched_at: int | None = None, env: str = "live",
        cache_key: str | None = None,
    ) -> str:
        """cache_key（模块 6）：显式指定主键——「最新版」语义的键版本段为空，
        而 product_type_version 列记录实际拉到的官方版本；不传则按参数
        重算（既有调用行为不变）。"""
        _check_env(env)
        key = cache_key or self.schema_cache_key(
            marketplace_id, pt_name, seller_id, locale, requirements,
            requirements_enforced, parentage_level, product_type_version,
            env,
        )
        t = fetched_at or now()
        with self._lock:
            # ON CONFLICT 连 product_type_version 一并更新（模块 6 要求 ⑥：
            # 元数据与实际文件一致——同键下官方版本推进时旧版本号不得残留）
            self._db.execute(
                "INSERT INTO pt_schema_meta(cache_key,marketplace_id,pt_name,"
                "seller_id,locale,requirements,requirements_enforced,"
                "parentage_level,product_type_version,payload_sha256,"
                "payload_path,status,env,fetched_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(cache_key) DO UPDATE SET "
                "payload_sha256=excluded.payload_sha256, payload_path="
                "excluded.payload_path, status=excluded.status, fetched_at="
                "excluded.fetched_at, product_type_version=excluded."
                "product_type_version",
                (key, marketplace_id, pt_name, seller_id, locale,
                 requirements, requirements_enforced, parentage_level,
                 product_type_version, payload_sha256, payload_path,
                 status, env, t),
            )
        return key

    def get_schema_meta(self, cache_key: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_schema_meta WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        return _row(r)

    def schema_metas_for(
        self, marketplace_id: str, pt_name: str | None = None,
        seller_id: str | None = None, env: str = "live",
    ) -> list[dict]:
        _check_env(env)
        q = ("SELECT * FROM pt_schema_meta WHERE marketplace_id=?"
             " AND env=?")
        args: list[Any] = [marketplace_id, env]
        if pt_name is not None:
            q += " AND pt_name=?"
            args.append(pt_name)
        if seller_id is not None:
            q += " AND seller_id=?"
            args.append(seller_id)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # ---- 概念②：BrowseNode ----

    def upsert_browse_node(
        self, *, marketplace_id: str, node_id: str, source: str = "btmap",
        evidence_status: str | None = None, evidence_kind: str | None = None,
        fit_status: str | None = None, display_name: str | None = None,
        context_path: str | None = None, evidence: dict | None = None,
        checked_at: int | None = None, env: str = "live",
    ) -> None:
        """None 的可选字段=不改动（首次插入用默认 pending）。
        env：mock catalog 证据与 live 官方证据分行（修正 3）。"""
        _check_env(env)
        if evidence_status is not None and evidence_status not in VALID_EVIDENCE:
            raise ValueError(f"非法 evidence_status: {evidence_status}")
        if fit_status is not None and fit_status not in VALID_FIT:
            raise ValueError(f"非法 fit_status: {fit_status}")
        ev = (json.dumps(evidence, ensure_ascii=False)
              if evidence is not None else None)
        with self._lock:
            cur = self._db.execute(
                "SELECT id FROM pt_browse_node WHERE marketplace_id=? AND"
                " node_id=? AND source=? AND env=?",
                (marketplace_id, node_id, source, env),
            ).fetchone()
            if cur is None:
                self._db.execute(
                    "INSERT INTO pt_browse_node(marketplace_id,node_id,"
                    "source,evidence_status,evidence_kind,fit_status,"
                    "display_name,context_path,evidence_json,env,checked_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (marketplace_id, node_id, source,
                     evidence_status or "pending", evidence_kind,
                     fit_status or "pending", display_name, context_path,
                     ev, env, checked_at or now()),
                )
                return
            sets, args = [], []
            for col, val in (
                ("evidence_status", evidence_status),
                ("evidence_kind", evidence_kind),
                ("fit_status", fit_status),
                ("display_name", display_name),
                ("context_path", context_path),
                ("evidence_json", ev),
                ("checked_at", checked_at or now()),
            ):
                if val is not None:
                    sets.append(f"{col}=?")
                    args.append(val)
            if not sets:
                return
            args.extend([cur["id"]])
            self._db.execute(
                f"UPDATE pt_browse_node SET {', '.join(sets)} WHERE id=?",
                args,
            )

    def browse_nodes(self, marketplace_id: str, env: str = "live"
                     ) -> list[dict]:
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pt_browse_node WHERE marketplace_id=?"
                " AND env=? ORDER BY node_id", (marketplace_id, env),
            ).fetchall()
        return [dict(r) for r in rows]

    def browse_node(
        self, marketplace_id: str, node_id: str,
        source: str | None = None, env: str = "live",
    ) -> dict | None:
        _check_env(env)
        q = ("SELECT * FROM pt_browse_node WHERE marketplace_id=? AND"
             " node_id=? AND env=?")
        args: list[Any] = [marketplace_id, node_id, env]
        if source is not None:
            q += " AND source=?"
            args.append(source)
        q += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            return _row(self._db.execute(q, args).fetchone())

    # ---- 候选（产品维度，店铺×站点隔离） ----

    def upsert_candidates(
        self, store_id: str, marketplace_id: str, pid: str,
        rows: Iterable[dict], task_id: str = "", now_ts: int | None = None,
        env: str = "live",
    ) -> int:
        """rows: [{strategy, pt_name, display_name?, score?, evidence?}]。
        env：mock 环境检索的候选与 live 候选分行（修正 3）。"""
        _check_env(env)
        t = now_ts or now()
        n = 0
        with self._tx():
            for r in rows or []:
                pt = str(r.get("pt_name") or "").strip()
                st = str(r.get("strategy") or "").strip()
                if not pt or not st:
                    continue
                ev = r.get("evidence")
                self._db.execute(
                    "INSERT INTO pt_candidate(store_id,marketplace_id,pid,"
                    "task_id,strategy,pt_name,display_name,score,"
                    "evidence_json,env,retrieved_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(store_id,marketplace_id,pid,pt_name,"
                    "strategy,env) DO UPDATE SET task_id=excluded.task_id, "
                    "display_name=excluded.display_name, score=excluded.score"
                    ", evidence_json=excluded.evidence_json, retrieved_at="
                    "excluded.retrieved_at",
                    (store_id, marketplace_id, pid, task_id, st, pt,
                     r.get("display_name"), r.get("score"),
                     json.dumps(ev, ensure_ascii=False) if ev is not None
                     else None, env, t),
                )
                n += 1
        return n

    def candidates(
        self, store_id: str, marketplace_id: str, pid: str,
        env: str = "live",
    ) -> list[dict]:
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pt_candidate WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND env=?"
                " ORDER BY score IS NULL, score DESC, id ASC",
                (store_id, marketplace_id, pid, env),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = (json.loads(d.pop("evidence_json") or "null")
                             if d.get("evidence_json") else None)
            out.append(d)
        return out

    # ---- 分配（追加式历史，is_current 单活；env 内单活） ----

    def add_assignment(
        self, *, store_id: str, marketplace_id: str, pid: str,
        pt_name: str, schema_cache_key: str | None = None,
        chosen_by: str, confidence: float | None = None,
        reason: str | None = None, now_ts: int | None = None,
        env: str = "live",
    ) -> int:
        if chosen_by not in VALID_CHOSEN_BY:
            raise ValueError(f"非法 chosen_by: {chosen_by}")
        _check_env(env)
        t = now_ts or now()
        with self._tx():
            old = self._db.execute(
                "SELECT id FROM pt_assignment WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND env=? AND is_current=1",
                (store_id, marketplace_id, pid, env),
            ).fetchone()
            if old is not None:
                self._db.execute(
                    "UPDATE pt_assignment SET is_current=0 WHERE id=?",
                    (old["id"],),
                )
            cur = self._db.execute(
                "INSERT INTO pt_assignment(store_id,marketplace_id,pid,"
                "pt_name,schema_cache_key,chosen_by,confidence,reason,env,"
                "is_current,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                (store_id, marketplace_id, pid, pt_name, schema_cache_key,
                 chosen_by, confidence, reason, env, t),
            )
            new_id = int(cur.lastrowid)
            if old is not None:
                self._db.execute(
                    "UPDATE pt_assignment SET superseded_by=? WHERE id=?",
                    (new_id, old["id"]),
                )
                # v6/S1：分类变更即旧属性失效（同事务原子；行保留为
                # 历史，lifecycle=stale + 原因——概念⑨铁律「失效不删除」）
                self._db.execute(
                    "UPDATE pt_attribute SET lifecycle='stale',"
                    " stale_reason=?, updated_at=?"
                    " WHERE assignment_id=? AND is_current=1"
                    " AND lifecycle='active'",
                    (f"assignment 顶替 #{old['id']}→#{new_id}", t,
                     int(old["id"])),
                )
        return new_id

    def current_assignment(
        self, store_id: str, marketplace_id: str, pid: str,
        env: str = "live",
    ) -> dict | None:
        _check_env(env)
        with self._lock:
            return _row(self._db.execute(
                "SELECT * FROM pt_assignment WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND env=? AND is_current=1",
                (store_id, marketplace_id, pid, env),
            ).fetchone())

    def assignment_history(
        self, store_id: str, marketplace_id: str, pid: str,
        env: str = "live",
    ) -> list[dict]:
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pt_assignment WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND env=? ORDER BY id DESC",
                (store_id, marketplace_id, pid, env),
            ).fetchall()
        return [dict(r) for r in rows]

    def assignments_all(self, store_id: str, marketplace_id: str,
                        env: str = "live") -> dict:
        """模块 8：一次取全部当前分配 {pid: 行}（UI 商品列表状态用）。"""
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pt_assignment WHERE store_id=? AND"
                " marketplace_id=? AND env=? AND is_current=1",
                (store_id, marketplace_id, env),
            ).fetchall()
        return {str(r["pid"]): dict(r) for r in rows}

    def latest_item_states(self, store_id: str, marketplace_id: str,
                           env: str = "live") -> dict:
        """模块 8：每个商品最近一次任务条目状态 {pid: {task_id,state,
        error_kind}}（同一商品多次任务取最新任务里的条目）。"""
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT i.pid AS pid, i.task_id AS task_id, i.state AS"
                " state, i.error_kind AS error_kind, t.created_at AS"
                " t_created, i.id AS item_id FROM pt_task_item i JOIN"
                " pt_task t ON t.id = i.task_id WHERE t.store_id=? AND"
                " t.marketplace_id=? AND t.env=?"
                " ORDER BY t.created_at DESC, i.id DESC",
                (store_id, marketplace_id, env),
            ).fetchall()
        out: dict = {}
        for r in rows:                     # 已按新→旧排，首个即最新
            out.setdefault(str(r["pid"]), {
                "task_id": str(r["task_id"]), "state": str(r["state"]),
                "error_kind": r["error_kind"]})
        return out

    # ---- 验证记录（四类独立成行；Mock/Live 严格分开） ----

    def add_verification(
        self, *, store_id: str, marketplace_id: str, pid: str,
        check_kind: str, status: str, pt_name: str | None = None,
        node_id: str | None = None, detail: dict | None = None,
        verified_against: str, now_ts: int | None = None,
    ) -> int:
        if check_kind not in VALID_CHECK:
            raise ValueError(f"非法 check_kind: {check_kind}")
        if status not in VALID_VSTATUS:
            raise ValueError(f"非法验证状态: {status}")
        if verified_against not in VALID_AGAINST:
            raise ValueError(f"非法 verified_against: {verified_against}")
        t = now_ts or now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO pt_verification(store_id,marketplace_id,pid,"
                "pt_name,node_id,check_kind,status,detail_json,"
                "verified_against,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (store_id, marketplace_id, pid, pt_name, node_id,
                 check_kind, status,
                 json.dumps(detail, ensure_ascii=False)
                 if detail is not None else None,
                 verified_against, t),
            )
            return int(cur.lastrowid)

    def verifications(
        self, store_id: str, marketplace_id: str, pid: str,
        check_kind: str | None = None,
    ) -> list[dict]:
        q = ("SELECT * FROM pt_verification WHERE store_id=? AND"
             " marketplace_id=? AND pid=?")
        args: list[Any] = [store_id, marketplace_id, pid]
        if check_kind is not None:
            q += " AND check_kind=?"
            args.append(check_kind)
        q += " ORDER BY id"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = (json.loads(d.pop("detail_json") or "null")
                           if d.get("detail_json") else None)
            out.append(d)
        return out

    def latest_verification(
        self, store_id: str, marketplace_id: str, pid: str,
        check_kind: str,
    ) -> dict | None:
        rows = self.verifications(store_id, marketplace_id, pid, check_kind)
        return rows[-1] if rows else None

    # ---- P5 人工复核证据链（AI 建议快照；与正式 assignment 严格分离，
    #      绝不代表「已确认分类」——确认只来自人工 assignment_confirm） ----

    # P5 集成验收（任务二.7）：同 (店铺,站点,pid,env) 只保留最近
    # REVIEW_EVIDENCE_KEEP 条建议快照——复核建议按次追加、历史可审计，
    # 但同一商品反复重跑不得无限累积（写入时原子裁剪，UI 永远取最新）。
    REVIEW_EVIDENCE_KEEP = 20

    def add_review_evidence(
        self, *, store_id: str, marketplace_id: str, pid: str,
        task_id: str = "", env: str = "live", stage: str = "",
        error_kind: str = "", review_reasons: list | None = None,
        ai_chosen: str | None = None, ai_confidence: float | None = None,
        ai_detail: dict | None = None, official_status: list | None = None,
        now_ts: int | None = None,
    ) -> int:
        """needs_review 时落一条结构化建议快照（追加式，不覆盖历史；
        超出保留窗口的旧行同事务裁剪）。"""
        _check_env(env)
        t = now_ts or now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO pt_review_evidence(store_id,marketplace_id,"
                "pid,task_id,env,stage,error_kind,review_reasons_json,"
                "ai_chosen,ai_confidence,ai_detail_json,"
                "official_status_json,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (store_id, marketplace_id, pid, task_id, env, stage,
                 error_kind,
                 json.dumps([str(r)[:200] for r in (review_reasons or [])]
                            [:12], ensure_ascii=False),
                 ai_chosen, ai_confidence,
                 json.dumps(ai_detail, ensure_ascii=False)
                 if ai_detail is not None else None,
                 json.dumps(official_status or [], ensure_ascii=False)
                 if official_status is not None else None,
                 t),
            )
            new_id = int(cur.lastrowid)
            # 保留策略：同键只留最近 N 条（任务二.7 防无限累积）
            self._db.execute(
                "DELETE FROM pt_review_evidence WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND env=? AND id NOT IN ("
                " SELECT id FROM pt_review_evidence WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND env=?"
                " ORDER BY id DESC LIMIT ?)",
                (store_id, marketplace_id, pid, env,
                 store_id, marketplace_id, pid, env,
                 self.REVIEW_EVIDENCE_KEEP),
            )
            return new_id

    def latest_review_evidence(
        self, store_id: str, marketplace_id: str, pid: str,
        env: str = "live",
    ) -> dict | None:
        """复核界面读取口（结构化；不必解析 gpt_evidence.jsonl）。"""
        _check_env(env)
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_review_evidence WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND env=?"
                " ORDER BY id DESC LIMIT 1",
                (store_id, marketplace_id, pid, env),
            ).fetchone()
        if r is None:
            return None
        d = dict(r)
        for jk, dk in (("review_reasons_json", "review_reasons"),
                       ("ai_detail_json", "ai_detail"),
                       ("official_status_json", "official_status")):
            raw = d.pop(jk, None)
            try:
                d[dk] = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                d[dk] = None          # 坏 JSON 不崩复核界面
        return d

    # ---- 人工纠错 ----

    def add_correction(
        self, *, store_id: str, marketplace_id: str, pid: str,
        from_pt: str | None, to_pt: str,
        from_assignment_id: int | None = None, by_user: str = "",
        note: str | None = None, now_ts: int | None = None,
    ) -> int:
        t = now_ts or now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO pt_correction(store_id,marketplace_id,pid,"
                "from_pt,to_pt,from_assignment_id,by_user,note,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (store_id, marketplace_id, pid, from_pt, to_pt,
                 from_assignment_id, by_user, note, t),
            )
            return int(cur.lastrowid)

    def corrections(
        self, store_id: str, marketplace_id: str,
        pid: str | None = None,
    ) -> list[dict]:
        q = ("SELECT * FROM pt_correction WHERE store_id=? AND"
             " marketplace_id=?")
        args: list[Any] = [store_id, marketplace_id]
        if pid is not None:
            q += " AND pid=?"
            args.append(pid)
        q += " ORDER BY id DESC"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # ---- 属性值（概念⑨ v6/S1；模型与状态机见 pt.attribute） ----

    def assignment_by_id(self, assignment_id: int) -> dict | None:
        """按主键读分配行（API 校验 assignment 归属用；只读）。"""
        with self._lock:
            return _row(self._db.execute(
                "SELECT * FROM pt_assignment WHERE id=?",
                (int(assignment_id),),
            ).fetchone())

    def put_attributes(
        self, *, user_id: str, store_id: str, marketplace_id: str,
        env: str, pid: str, sku: str = "", assignment_id: int,
        pt_name: str = "", schema_cache_key: str = "",
        schema_version: str = "", attributes: list[dict],
        now_ts: int | None = None,
    ) -> dict:
        """批量写入属性值（单事务原子；幂等；并发安全）。

        attributes 元素：{attribute_path, value, source_grade,
        source_type, source_reference?, confidence?, validation_status?,
        validation_errors?}。写入规则：
          * 作用域内已有 active 当前行且内容完全一致 → 幂等跳过
            （unchanged 计数，不产生新修订）；
          * 内容不同（含换绑 assignment）→ 旧行 is_current=0 +
            superseded_by 指向新行，新行 review_status 一律 proposed
            （引擎没有路径直接写 confirmed——人工确认走
            set_review_status(human=True)）；
          * 批内 attribute_path 重复 → ValueError（整批拒绝）。
        返回 {created, unchanged, rows:[当前行…]}。
        """
        _check_env(env)
        if not user_id:
            raise ValueError("user_id 不能为空（属性写入必须可审计）")
        if not attributes:
            raise ValueError("attributes 不能为空")
        if len(attributes) > MAX_ATTRIBUTES_PER_CALL:
            raise ValueError(f"单次最多 {MAX_ATTRIBUTES_PER_CALL} 个属性")
        clean_sku = check_sku(sku)
        t = now_ts or now()
        seen: set[str] = set()
        rows_out: list[dict] = []
        n_created = n_same = 0
        with self._tx():
            for a in attributes:
                if not isinstance(a, dict):
                    raise ValueError("attributes 元素必须是 dict")
                path = check_attribute_path(a.get("attribute_path"))
                if path in seen:
                    raise ValueError(f"批内 attribute_path 重复：{path}")
                seen.add(path)
                grade, stype, sref, conf = check_source(
                    a.get("source_grade"), a.get("source_type"),
                    a.get("source_reference"), a.get("confidence"))
                vtext = check_value_json(a.get("value"), grade=grade)
                vstat, verrs = check_validation(
                    a.get("validation_status"),
                    a.get("validation_errors"))
                cur = self._db.execute(
                    "SELECT * FROM pt_attribute WHERE store_id=? AND"
                    " marketplace_id=? AND env=? AND pid=? AND"
                    " attribute_path=? AND is_current=1"
                    " AND lifecycle='active'",
                    (store_id, marketplace_id, env, pid, path),
                ).fetchone()
                same = cur is not None and all(
                    str(cur[c]) == str(v) for c, v in (
                        ("assignment_id", int(assignment_id)),
                        ("schema_cache_key", schema_cache_key),
                        ("schema_version", schema_version),
                        ("pt_name", pt_name),
                        ("value_json", vtext),
                        ("source_grade", grade),
                        ("source_type", stype),
                        ("source_reference", sref),
                        ("validation_status", vstat),
                        ("validation_errors_json", verrs),
                    )
                ) and abs(float(cur["confidence"]) - conf) < 1e-9
                if same:
                    n_same += 1
                    rows_out.append(dict(cur))
                    continue
                if cur is not None:
                    self._db.execute(
                        "UPDATE pt_attribute SET is_current=0, "
                        "superseded_by=NULL WHERE id=?", (cur["id"],),
                    )
                c = self._db.execute(
                    "INSERT INTO pt_attribute(user_id,store_id,"
                    "marketplace_id,env,pid,sku,assignment_id,pt_name,"
                    "schema_cache_key,schema_version,attribute_path,"
                    "value_json,source_grade,source_type,source_reference,"
                    "confidence,validation_status,validation_errors_json,"
                    "review_status,lifecycle,is_current,created_at,"
                    "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"
                    ",?,'active',1,?,?)",
                    (user_id, store_id, marketplace_id, env, pid,
                     clean_sku, int(assignment_id), pt_name,
                     schema_cache_key, schema_version, path, vtext,
                     grade, stype, sref, conf, vstat, verrs,
                     "proposed", t, t),
                )
                new_id = int(c.lastrowid)
                if cur is not None:
                    self._db.execute(
                        "UPDATE pt_attribute SET superseded_by=? "
                        "WHERE id=?", (new_id, cur["id"]),
                    )
                rows_out.append(dict(
                    self._db.execute(
                        "SELECT * FROM pt_attribute WHERE id=?",
                        (new_id,)).fetchone()))
                n_created += 1
        return {"created": n_created, "unchanged": n_same, "rows": rows_out}

    def get_attributes(
        self, store_id: str, marketplace_id: str, env: str, pid: str,
        *, include_history: bool = False,
    ) -> list[dict]:
        """当前有效属性值（include_history=True 时带全部历史修订）。"""
        _check_env(env)
        q = ("SELECT * FROM pt_attribute WHERE store_id=? AND"
             " marketplace_id=? AND env=? AND pid=?")
        if not include_history:
            q += " AND is_current=1 AND lifecycle='active'"
        q += " ORDER BY attribute_path, id"
        with self._lock:
            rows = self._db.execute(
                q, (store_id, marketplace_id, env, pid)).fetchall()
        return [dict(r) for r in rows]

    def get_attribute_history(
        self, store_id: str, marketplace_id: str, env: str, pid: str,
        attribute_path: str,
    ) -> list[dict]:
        """单属性修订链（新→旧；含 stale/superseded 全部历史行）。"""
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pt_attribute WHERE store_id=? AND"
                " marketplace_id=? AND env=? AND pid=? AND attribute_path=?"
                " ORDER BY id DESC",
                (store_id, marketplace_id, env, pid,
                 check_attribute_path(attribute_path)),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_review_status(
        self, *, user_id: str, store_id: str, marketplace_id: str,
        env: str, pid: str, attribute_path: str, new_status: str,
        note: str = "", human: bool = False, now_ts: int | None = None,
    ) -> dict:
        """人工复核状态转移（confirmed 只经 human=True 显式路径）。

        stale 行拒绝转移（上下文已失效，须在新上下文重新产出）。
        转移合法性由 pt.attribute.REVIEW_TRANSITIONS 校验。
        """
        _check_env(env)
        if not user_id:
            raise ValueError("user_id 不能为空（复核必须可审计）")
        nxt = str(new_status or "").strip()
        if nxt == "confirmed" and not human:
            raise ValueError("confirmed 只能由人工操作写入"
                             "（human=True）——程序不自动确认属性")
        t = now_ts or now()
        with self._tx():
            cur = self._db.execute(
                "SELECT * FROM pt_attribute WHERE store_id=? AND"
                " marketplace_id=? AND env=? AND pid=? AND"
                " attribute_path=? AND is_current=1",
                (store_id, marketplace_id, env, pid,
                 check_attribute_path(attribute_path)),
            ).fetchone()
            if cur is None:
                raise ValueError(f"没有该属性的当前值：{attribute_path}")
            if cur["lifecycle"] == "stale":
                raise ValueError(
                    f"属性 {attribute_path} 已随上下文失效"
                    f"（{cur['stale_reason']}）——不能复核，须在新分类/"
                    "Schema 上下文下重新产出")
            if cur["review_status"] == nxt \
                    and nxt in ("confirmed", "edited", "rejected"):
                # S2e 幂等：人工动作重复打到同一目标状态（重复点击/网络
                # 重试）= 无变化，直接回当前行（不写库不刷审计——重复的
                # 人工操作不得重复执行）。proposed 不在其列：它不是人工
                # 动作的合法目标，同态回放仍按非法跳转拒绝（C7f）。
                return dict(cur)
            check_review_transition(cur["review_status"], nxt)
            ref = str(note or "").strip()[:MAX_SOURCE_REFERENCE_LEN]
            self._db.execute(
                "UPDATE pt_attribute SET review_status=?, updated_at=?,"
                " user_id=?, source_reference=CASE WHEN ?='' THEN"
                " source_reference ELSE ? END WHERE id=?",
                (nxt, t, user_id, ref,
                 f"{ref} | 复核→{nxt}", cur["id"]),
            )
            return dict(self._db.execute(
                "SELECT * FROM pt_attribute WHERE id=?",
                (cur["id"],)).fetchone())

    def edit_attribute(
        self, *, user_id: str, store_id: str, marketplace_id: str,
        env: str, pid: str, attribute_path: str, value,
        assignment_id: int, pt_name: str = "",
        schema_cache_key: str = "", schema_version: str = "",
        sku: str = "", note: str = "", now_ts: int | None = None,
    ) -> dict:
        """人工编辑值 → 新修订行（review_status=edited；旧行进历史链）。

        人工录入=新原始事实：source_grade=A / source_type=human /
        confidence=1.0。stale 行拒绝编辑（同复核规则）。
        """
        _check_env(env)
        if not user_id:
            raise ValueError("user_id 不能为空（编辑必须可审计）")
        path = check_attribute_path(attribute_path)
        clean_sku = check_sku(sku)
        t = now_ts or now()
        with self._tx():
            cur = self._db.execute(
                "SELECT * FROM pt_attribute WHERE store_id=? AND"
                " marketplace_id=? AND env=? AND pid=? AND"
                " attribute_path=? AND is_current=1",
                (store_id, marketplace_id, env, pid, path),
            ).fetchone()
            if cur is not None and cur["lifecycle"] == "stale":
                raise ValueError(
                    f"属性 {path} 已随上下文失效"
                    f"（{cur['stale_reason']}）——请在新分类/Schema 上下文"
                    "下重新产出后再编辑")
            vtext = check_value_json(value, grade="A")
            # S2e 幂等：现行行已是人工编辑且值完全一致（网络重试/重复
            # 点击）→ 不再叠新修订行，直接回当前行。proposed→edited 的
            # 真实首次编辑不受影响（那是一次状态变化，照常建修订）。
            if (cur is not None and cur["review_status"] == "edited"
                    and cur["source_type"] == "human"
                    and cur["value_json"] == vtext):
                return dict(cur)
            ref = "human:" + (str(note or "").strip() or "人工编辑")
            if cur is not None:
                self._db.execute(
                    "UPDATE pt_attribute SET is_current=0, superseded_by="
                    "NULL WHERE id=?", (cur["id"],))
            c = self._db.execute(
                "INSERT INTO pt_attribute(user_id,store_id,marketplace_id,"
                "env,pid,sku,assignment_id,pt_name,schema_cache_key,"
                "schema_version,attribute_path,value_json,source_grade,"
                "source_type,source_reference,confidence,validation_status,"
                "validation_errors_json,review_status,lifecycle,is_current,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"
                ",?,?,'[]','edited','active',1,?,?)",
                (user_id, store_id, marketplace_id, env, pid, clean_sku,
                 int(assignment_id), pt_name, schema_cache_key,
                 schema_version, path, vtext, "A", "human", ref[:500],
                 1.0, "pending", t, t),
            )
            new_id = int(c.lastrowid)
            if cur is not None:
                self._db.execute(
                    "UPDATE pt_attribute SET superseded_by=? WHERE id=?",
                    (new_id, cur["id"]))
            return dict(self._db.execute(
                "SELECT * FROM pt_attribute WHERE id=?",
                (new_id,)).fetchone())

    def invalidate_attributes_for_assignment(
        self, assignment_id: int, reason: str,
    ) -> int:
        """把某分配下的 active 当前属性全部置 stale（显式失效；返回行数）。"""
        r = check_stale_reason(reason)
        t = now()
        with self._tx():
            cur = self._db.execute(
                "UPDATE pt_attribute SET lifecycle='stale', stale_reason=?,"
                " updated_at=? WHERE assignment_id=? AND is_current=1"
                " AND lifecycle='active'",
                (r, t, int(assignment_id)),
            )
            return int(cur.rowcount)

    def invalidate_attributes_for_schema(
        self, schema_cache_key: str, reason: str,
    ) -> int:
        """把某 Schema 缓存上下文下的 active 当前属性全部置 stale。

        （Schema meta 标记 corrupt/missing/版本更新时由维护流程调用；
        模块 6 本轮不动——S2 再接自动钩子。）
        """
        if not str(schema_cache_key or "").strip():
            raise ValueError("schema_cache_key 不能为空")
        r = check_stale_reason(reason)
        t = now()
        with self._tx():
            cur = self._db.execute(
                "UPDATE pt_attribute SET lifecycle='stale', stale_reason=?,"
                " updated_at=? WHERE schema_cache_key=? AND is_current=1"
                " AND lifecycle='active'",
                (r, t, str(schema_cache_key)),
            )
            return int(cur.rowcount)

    # ---- 任务（逐商品条目 = 恢复的真源；模块 7 v3 词表与原子驱动） ----

    def create_task(
        self, task_id: str, kind: str, store_id: str,
        marketplace_id: str, pids: Iterable[str],
        now_ts: int | None = None, env: str = "live", source: str = "",
        idem_key: str | None = None, new_operation: bool = False,
    ) -> dict:
        """创建批量任务（v3：幂等 + 活跃重叠守卫）。

        - 同一 PID 集合内去重；空集合 ValueError。
        - idem_key：幂等请求键——同键任务**活跃**（ACTIVE_TASK_PHASES，
          含 interrupted 可恢复）时直接返回该任务（dict 带
          "_created": False），同一请求重试/并发去重，绝不重复创建。
        - 幂等新鲜度（S3-B2-F）：同键任务已**终态**（completed/failed/
          cancelled）→ StaleTaskError——分类无内容快照可比对（引擎逐次
          现读商品），无法安全判定「同一业务操作」，不得静默复用旧任务
          （商品可能已改动）；调用方须带 new_operation=true 显式发起新
          一轮（键加代数后缀 base#N，与刊登任务同一并发安全模型；旧任
          务行零改动）。
        - 活跃重叠守卫：同 (store_id, marketplace_id, env) 下，若已有
          活跃任务（ACTIVE_TASK_PHASES）的条目与新 PID 有交集 →
          TaskConflictError（不得两个活跃任务相互覆盖同一商品）。
          判定在 BEGIN IMMEDIATE 事务内，多进程并发创建亦被写锁串行化。
        返回任务行（含 "_created": True/False）。
        """
        _check_env(env)
        t = now_ts or now()
        seen: list[str] = []
        for p in pids or []:
            p = str(p).strip()
            if p and p not in seen:
                seen.append(p)
        if not seen:
            raise ValueError("任务至少需要一个 PID（去重后为空）")
        with self._tx():
            if idem_key:
                if new_operation:
                    # 显式新一轮：代数后缀保证多轮可分（同 base 事务内
                    # 计数，BEGIN IMMEDIATE 并发安全；base 行不计数）
                    n = self._db.execute(
                        "SELECT COUNT(*) FROM pt_task WHERE"
                        " idem_key LIKE ?", (idem_key + "#%",)).fetchone()[0]
                    idem_key = f"{idem_key}#{n + 1}"
                row = self._db.execute(
                    "SELECT * FROM pt_task WHERE idem_key=?",
                    (idem_key,)).fetchone()
                if row is not None:
                    d = dict(row)
                    d["_created"] = False
                    if row["phase"] in ACTIVE_TASK_PHASES:
                        return d      # 活跃期重试/并发去重（原语义）
                    # 终态命中：分类结果基于当时的商品内容且引擎现读无
                    # 快照可比对——无法安全判定同一业务操作，拒绝复用
                    raise StaleTaskError(
                        f"同参数任务 {row['id']} 已结束"
                        f"（{row['phase']}），商品内容可能已改动，不得静默"
                        "复用旧任务；确认发起新一轮请传 new_operation="
                        "true（旧任务与结果原样保留）")
            marks = ",".join("?" for _ in seen)
            clash = self._db.execute(
                f"SELECT i.pid, t.id FROM pt_task_item i JOIN pt_task t"
                f" ON t.id=i.task_id WHERE t.store_id=? AND"
                f" t.marketplace_id=? AND t.env=? AND t.phase IN"
                f" ({','.join('?' for _ in ACTIVE_TASK_PHASES)})"
                f" AND i.pid IN ({marks}) LIMIT 1",
                [store_id, marketplace_id, env, *ACTIVE_TASK_PHASES,
                 *seen],
            ).fetchone()
            if clash is not None:
                raise TaskConflictError(
                    f"PID {clash['pid']} 已属于活跃任务 {clash['id']}"
                    f"（同店铺×站点×env 不得并行两个活跃分类任务）")
            self._db.execute(
                "INSERT INTO pt_task(id,kind,store_id,marketplace_id,env,"
                "phase,progress_done,progress_total,source,idem_key,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?,?,?,?)",
                (task_id, kind, store_id, marketplace_id, env, "created",
                 len(seen), source, idem_key, t, t),
            )
            self._db.executemany(
                "INSERT INTO pt_task_item(task_id,pid,state,attempt,"
                "updated_at) VALUES(?,?,'pending',0,?)",
                [(task_id, p, t) for p in seen],
            )
            row = self._db.execute(
                "SELECT * FROM pt_task WHERE id=?", (task_id,)).fetchone()
            d = dict(row)
            d["_created"] = True
            return d

    def set_task_phase(
        self, task_id: str, phase: str, error: str | None = None,
        finished: bool = False, now_ts: int | None = None,
    ) -> None:
        if phase not in VALID_TASK_PHASE:
            raise ValueError(f"非法任务状态: {phase}")
        t = now_ts or now()
        with self._lock:
            self._db.execute(
                "UPDATE pt_task SET phase=?, error=?, updated_at=?"
                + (", finished_at=?" if finished else "")
                + " WHERE id=?",
                (phase, error, t, t, task_id) if finished
                else (phase, error, t, task_id),
            )

    def cas_task_phase(
        self, task_id: str, from_phases: Iterable[str], to_phase: str,
        error: str | None = None, finished: bool = False,
        now_ts: int | None = None,
    ) -> bool:
        """条件相变（compare-and-set）：当前相位 ∈ from_phases 才改成
        to_phase。返回是否改成功——并发控制请求（暂停/取消/完成推导）
        互相竞争时的唯一裁决手段，绝不出现相位回退。
        非终态目标一并清 finished_at（同一条 UPDATE=事务一致）：终态
        重跑（retry/resume）后 running 不得残留旧完成时间；历史完成
        时间由操作日志（ops.jsonl 的 prev_finished_at）审计保留。"""
        if to_phase not in VALID_TASK_PHASE:
            raise ValueError(f"非法任务状态: {to_phase}")
        t = now_ts or now()
        frm = tuple(from_phases)
        marks = ",".join("?" for _ in frm)
        clears_finish = (
            not finished and to_phase not in
            ("completed", "completed_with_issues", "failed", "cancelled"))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE pt_task SET phase=?, error=?, updated_at=?"
                + (", finished_at=?" if finished else "")
                + (", finished_at=NULL" if clears_finish else "")
                + f" WHERE id=? AND phase IN ({marks})",
                (to_phase, error, t, t, task_id, *frm) if finished
                else (to_phase, error, t, task_id, *frm),
            )
            return int(cur.rowcount) == 1

    def set_resume_pending(self, task_id: str, on: bool = True) -> None:
        """置位/复位「继续」意图（幂等：重复置位不叠加语义）。"""
        with self._lock:
            self._db.execute(
                "UPDATE pt_task SET resume_pending=?, updated_at=?"
                " WHERE id=?", (1 if on else 0, now(), task_id))

    def consume_resume_pending(self, task_id: str) -> bool:
        """原子消费「继续」意图：只有一个调用方拿到 True（重复点击/
        并发续跑方都不会二次消费）。"""
        with self._lock:
            cur = self._db.execute(
                "UPDATE pt_task SET resume_pending=0, updated_at=?"
                " WHERE id=? AND resume_pending=1", (now(), task_id))
            return int(cur.rowcount) == 1

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_task WHERE id=?", (task_id,)).fetchone()
            if r is None:
                return None
            d = dict(r)
            done = self._db.execute(
                f"SELECT COUNT(*) FROM pt_task_item WHERE task_id=? AND"
                f" state IN ({','.join('?' for _ in ITEM_FINISHED_STATES)})",
                (task_id, *ITEM_FINISHED_STATES),
            ).fetchone()[0]
            d["progress_done"] = int(done)
            return d

    def tasks(self, phase: str | None = None, *, store_id: str | None = None,
              marketplace_id: str | None = None,
              env: str | None = None) -> list[dict]:
        q = "SELECT * FROM pt_task WHERE 1=1"
        args: list[Any] = []
        if phase is not None:
            q += " AND phase=?"
            args.append(phase)
        if store_id is not None:
            q += " AND store_id=?"
            args.append(store_id)
        if marketplace_id is not None:
            q += " AND marketplace_id=?"
            args.append(marketplace_id)
        if env is not None:
            _check_env(env)
            q += " AND env=?"
            args.append(env)
        q += " ORDER BY created_at DESC, id"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def claim_item(self, task_id: str,
                   now_ts: int | None = None) -> str | None:
        """原子领取（模块 7 批复八·5）：取一个 pending 条目置 running，
        防两个 Worker 重复领同一商品。只在任务相位=running 时领取
        （pausing/cancelling/终态一律不再放新商品）。attempt 不变
        （attempt=失败次数，claim 不烧重试预算）。返回 pid 或 None。"""
        t = now_ts or now()
        with self._tx():
            phase = self._db.execute(
                "SELECT phase FROM pt_task WHERE id=?", (task_id,),
            ).fetchone()
            if phase is None or phase["phase"] != "running":
                return None
            row = self._db.execute(
                "SELECT id, pid FROM pt_task_item WHERE task_id=? AND"
                " state='pending' ORDER BY id LIMIT 1", (task_id,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE pt_task_item SET state='running', stage='material',"
                " error=NULL, error_kind=NULL, updated_at=? WHERE id=?",
                (t, row["id"]),
            )
            return str(row["pid"])

    def set_item_state(
        self, task_id: str, pid: str, state: str,
        error: str | None = None, error_kind: str | None = None,
        result_ref: str | None = None, now_ts: int | None = None,
    ) -> None:
        """置条目终态/中间态（v3）。attempt 仅在 retryable_failed 时 +1
        （失败计数）；stage 不在此改（走 set_item_stage）。"""
        if state not in VALID_ITEM_STATE:
            raise ValueError(f"非法条目状态: {state}")
        t = now_ts or now()
        with self._lock:
            row = self._db.execute(
                "SELECT id, attempt FROM pt_task_item WHERE task_id=? AND"
                " pid=?", (task_id, pid),
            ).fetchone()
            if row is None:
                raise KeyError(f"任务 {task_id} 没有条目 {pid}")
            attempt = row["attempt"] + (1 if state == "retryable_failed"
                                        else 0)
            self._db.execute(
                "UPDATE pt_task_item SET state=?, error=?, error_kind=?,"
                " attempt=?, result_ref=COALESCE(?, result_ref),"
                " updated_at=? WHERE id=?",
                (state, error, error_kind, attempt, result_ref, t,
                 row["id"]),
            )
            if state in ITEM_FINISHED_STATES:
                self._db.execute(
                    "UPDATE pt_task SET last_pid=?, updated_at=? WHERE"
                    " id=?", (pid, t, task_id),
                )
            else:
                self._db.execute(
                    "UPDATE pt_task SET updated_at=? WHERE id=?",
                    (t, task_id),
                )

    def set_item_stage(self, task_id: str, pid: str, stage: str,
                       now_ts: int | None = None) -> None:
        """处理阶段推进（与状态两个维度；失败时 error_kind 另记）。"""
        if stage not in VALID_ITEM_STAGE:
            raise ValueError(f"非法处理阶段: {stage}")
        t = now_ts or now()
        with self._lock:
            self._db.execute(
                "UPDATE pt_task_item SET stage=?, updated_at=? WHERE"
                " task_id=? AND pid=?", (stage, t, task_id, pid),
            )

    def task_item_row(self, task_id: str, pid: str) -> dict | None:
        """单条目整行读取（D1.12.2 阶段级续跑/六态展示依据）。"""
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_task_item WHERE task_id=? AND pid=?",
                (task_id, pid)).fetchone()
        return dict(r) if r is not None else None

    def update_item_progress(self, task_id: str, pid: str,
                             **fields) -> None:
        """条目阶段状态列更新（D1.12.2）。只收白名单列——material_fp
        （资料指纹：变了=上游资料已改，阶段结果不得复用）、ai_pt/
        ai_confidence（AI 已分类：后续阶段失败也保留）、schema_ok
        （Schema 已获取）、attr_status（属性缺口结论）。"""
        allow = ("material_fp", "ai_pt", "ai_confidence", "schema_ok",
                 "attr_status")
        bad = set(fields) - set(allow)
        if bad:
            raise ValueError(f"非法条目进度列: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        args = [*fields.values(), now(), task_id, pid]
        with self._tx():
            self._db.execute(
                f"UPDATE pt_task_item SET {sets}, updated_at=? "
                "WHERE task_id=? AND pid=?", args)

    def set_task_perf(self, task_id: str, perf_json: str) -> None:
        """任务收尾时落阶段耗时/请求计数审计（perf_json 由引擎聚合，
        纯展示与报告用途，不参与任何门控判定）。"""
        with self._tx():
            self._db.execute(
                "UPDATE pt_task SET perf_json=?, updated_at=? WHERE id=?",
                (perf_json[:20000], now(), task_id))

    def task_items(
        self, task_id: str, states: Iterable[str] | None = None,
    ) -> list[dict]:
        q = "SELECT * FROM pt_task_item WHERE task_id=?"
        args: list[Any] = [task_id]
        if states:
            ss = [s for s in states if s]
            if ss:
                q += (" AND state IN ("
                      + ",".join("?" for _ in ss) + ")")
                args.extend(ss)
        q += " ORDER BY id"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def task_item_counts(self, task_id: str) -> dict:
        """按状态计数（任务进度/终态推导的真源——不依赖内存计数器）。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) AS n FROM pt_task_item WHERE"
                " task_id=? GROUP BY state", (task_id,),
            ).fetchall()
        counts = {s: 0 for s in VALID_ITEM_STATE}
        total = 0
        for r in rows:
            counts[r["state"]] = int(r["n"])
            total += int(r["n"])
        counts["total"] = total
        return counts

    def requeue_retryable(self, task_id: str, max_attempts: int,
                          now_ts: int | None = None) -> int:
        """自动重试策略（模块 7 批复六）：retryable_failed 且失败次数
        < max_attempts 的条目拉回 pending。attempt/error 保留（失败史
        不抹），下一轮 claim 只在成功时清 error。"""
        t = now_ts or now()
        with self._lock:
            cur = self._db.execute(
                "UPDATE pt_task_item SET state='pending', updated_at=?"
                " WHERE task_id=? AND state='retryable_failed' AND"
                " attempt<?", (t, task_id, int(max_attempts)),
            )
            return int(cur.rowcount)

    def retry_failed(self, task_id: str) -> int:
        """显式重试（用户操作，非自动策略）：retryable_failed 与
        permanent_failed 一并拉回 pending。needs_review 不动——那是
        人工判断位，重跑检索不等于复核（模块 7 批复六）。"""
        t = now()
        with self._lock:
            cur = self._db.execute(
                "UPDATE pt_task_item SET state='pending', updated_at=?"
                " WHERE task_id=? AND state IN ('retryable_failed',"
                " 'permanent_failed')", (t, task_id),
            )
            return int(cur.rowcount)

    def recover_interrupted(self, now_ts: int | None = None) -> list[str]:
        """启动恢复（模块 7 批复六·进程异常退出）：running/pausing/
        cancelling 任务→interrupted；其中 running 条目→pending（终态
        条目原样保留——已完成商品绝不重跑）。paused 是稳定相位、created
        从未起跑，都不动。恢复以 pt_task_item 为准，不读 last_pid
        （修正 2）。"""
        t = now_ts or now()
        with self._tx():
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM pt_task WHERE phase IN"
                " ('running','pausing','cancelling')").fetchall()]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            self._db.execute(
                f"UPDATE pt_task SET phase='interrupted', updated_at=?,"
                f" resume_pending=0 WHERE id IN ({marks})", [t, *ids],
            )
            self._db.execute(
                f"UPDATE pt_task_item SET state='pending',"
                f" updated_at=? WHERE task_id IN ({marks})"
                " AND state='running'", [t, *ids],
            )
        return ids

    # ==================================================================
    # 刊登任务（概念⑩ v7/S2c；引擎见 pt.listing）——与分类任务分表，
    # 机制同构（CAS 相变/原子领取/恢复/幂等键），词表独立。
    # ==================================================================

    def create_listing_task(
        self, task_id: str, user_id: str, store_id: str,
        marketplace_id: str, env: str, source_mode: str,
        items: Iterable[dict], source: str = "", source_ref: str = "",
        idem_key: str | None = None, idem_fp: str = "",
        new_operation: bool = False, now_ts: int | None = None,
    ) -> dict:
        """创建刊登任务（v7 幂等 + 守卫 + 快照；v9 幂等新鲜度）。

        - items=[{pid, sku, parent_sku, variant_role, content_sha,
          assignment_id, pt_name, schema_cache_key, schema_version,
          attr_revision}]：以 (pid, sku) 去重，空集合 ValueError；
          快照字段随条目固化——category 来源的商品集合在此刻定死，
          之后分类移动不会改变本任务范围。
        - idem_key：同键任务直接返回已有行（"_created": False）。
        - 幂等新鲜度（S3-B2 P1-1）：
          * 命中任务仍活跃（ACTIVE_TASK_PHASES）→ 返回该任务——同一
            请求重试/并发去重，绝不重复创建或提交；
          * 命中任务已终态：idem_fp（内容指纹=集合×content_sha×
            assignment×schema×attr_revision）与该任务创建时一致 →
            返回该任务（"_verdict": "replay"，真·同一业务操作重放）；
            不一致或历史行无指纹 → StaleTaskError（409：不得静默复用
            旧任务，调用方须带 new_operation 显式发起新一轮）；
          * new_operation=True → 键加代数后缀（base#N，N=同 base 历史
            任务数，事务内计数并发安全），新建任务走全新快照与守卫；
            旧任务行与快照零改动（历史事实不可覆盖）。
        - 活跃重叠守卫：同 (store_id, marketplace_id, env) 下已有活跃
          刊登任务（ACTIVE_TASK_PHASES）的条目 SKU 与新集合有交集 →
          TaskConflictError。判定在 BEGIN IMMEDIATE 事务内，多进程并发
          创建亦被写锁串行化（与分类 create_task 同一并发模型）。
        - snapshot_hash=去重后 pid:sku 排序串的 sha256（集合指纹）。
        """
        _check_env(env)
        if source_mode not in VALID_LISTING_SOURCE_MODE:
            raise ValueError(f"非法任务来源: {source_mode}")
        t = now_ts or now()
        seen: list[tuple[str, str]] = []
        rows: list[dict] = []
        for it in items or []:
            pid = str(it.get("pid") or "").strip()
            sku = str(it.get("sku") or "").strip()
            if not pid or not sku or (pid, sku) in seen:
                continue
            seen.append((pid, sku))
            rows.append({
                "pid": pid, "sku": sku,
                "parent_sku": str(it.get("parent_sku") or "").strip(),
                "variant_role": str(it.get("variant_role") or "standalone"),
                "content_sha": str(it.get("content_sha") or ""),
                "assignment_id": it.get("assignment_id"),
                "pt_name": str(it.get("pt_name") or ""),
                "schema_cache_key": str(it.get("schema_cache_key") or ""),
                "schema_version": str(it.get("schema_version") or ""),
                "attr_revision": str(it.get("attr_revision") or ""),
            })
        if not rows:
            raise ValueError("刊登任务至少需要一个 SKU 条目（去重后为空）")
        for r in rows:
            if r["variant_role"] not in VALID_VARIANT_ROLE:
                raise ValueError(f"非法变体角色: {r['variant_role']}")
        snap = hashlib.sha256(
            "|".join(sorted(f"{p}:{s}" for p, s in seen))
            .encode("utf-8")).hexdigest()[:24]
        with self._tx():
            if idem_key:
                if new_operation:
                    # 显式新一轮：代数后缀保证多轮可分（同 base 计数，
                    # BEGIN IMMEDIATE 内并发安全）；新一轮同内容重放
                    # 仍幂等到本轮键。base 行本身=第 0 轮不计数。
                    n = self._db.execute(
                        "SELECT COUNT(*) FROM pt_listing_task WHERE"
                        " idem_key LIKE ?", (idem_key + "#%",)).fetchone()[0]
                    idem_key = f"{idem_key}#{n + 1}"
                row = self._db.execute(
                    "SELECT * FROM pt_listing_task WHERE idem_key=?",
                    (idem_key,)).fetchone()
                if row is not None:
                    d = dict(row)
                    d["_created"] = False
                    if row["phase"] in ACTIVE_TASK_PHASES:
                        return d          # 活跃期重试/并发去重（原语义）
                    # 终态命中：内容指纹判定（无法安全判定=拒绝）
                    old_fp = str(row["idem_fp"] or "")
                    if idem_fp and old_fp == idem_fp:
                        d["_verdict"] = "replay"
                        return d
                    raise StaleTaskError(
                        f"同参数任务 {row['id']} 已结束"
                        f"（{row['phase']}），且商品内容/属性/Schema/"
                        f"集合指纹与当时{'不一致' if old_fp else '无法判定'}"
                        "——不得静默复用旧任务；确认发起新一轮请传"
                        " new_operation=true（旧任务与快照原样保留）")
            # 守卫按 SKU 判（验收令：store+marketplace+env+SKU）：同店内
            # SKU 唯一对应一条 Amazon 刊登——不同商品撞同一 SKU 同样冲突。
            skus = sorted({s for _, s in seen})
            clash = self._db.execute(
                f"SELECT i.sku, i.pid, t.id FROM pt_listing_item i JOIN"
                f" pt_listing_task t ON t.id=i.task_id WHERE t.store_id=?"
                f" AND t.marketplace_id=? AND t.env=? AND t.phase IN"
                f" ({','.join('?' for _ in ACTIVE_TASK_PHASES)})"
                f" AND i.sku IN ({','.join('?' for _ in skus)})"
                f" LIMIT 1",
                [store_id, marketplace_id, env, *ACTIVE_TASK_PHASES,
                 *skus],
            ).fetchone()
            if clash is not None:
                raise TaskConflictError(
                    f"SKU {clash['sku']}（商品 {clash['pid']}）已属于活跃"
                    f"刊登任务 {clash['id']}"
                    f"（同店铺×站点×env 不得并行两个活跃刊登任务）")
            self._db.execute(
                "INSERT INTO pt_listing_task(id,user_id,store_id,"
                "marketplace_id,env,source_mode,source_ref,snapshot_hash,"
                "snapshot_n,phase,source,idem_key,idem_fp,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,'created',?,?,?,?,?)",
                (task_id, user_id, store_id, marketplace_id, env,
                 source_mode, source_ref, snap, len(rows),
                 source, idem_key, idem_fp, t, t),
            )
            self._db.executemany(
                "INSERT INTO pt_listing_item(task_id,pid,sku,parent_sku,"
                "variant_role,content_sha,assignment_id,pt_name,"
                "schema_cache_key,schema_version,attr_revision,state,"
                "attempt,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',0,?)",
                [(task_id, r["pid"], r["sku"], r["parent_sku"],
                  r["variant_role"], r["content_sha"],
                  r["assignment_id"], r["pt_name"],
                  r["schema_cache_key"], r["schema_version"],
                  r["attr_revision"], t) for r in rows],
            )
            row = self._db.execute(
                "SELECT * FROM pt_listing_task WHERE id=?",
                (task_id,)).fetchone()
            d = dict(row)
            d["_created"] = True
            d["_snapshot_hash"] = snap
            return d

    def cas_listing_phase(
        self, task_id: str, from_phases: Iterable[str], to_phase: str,
        error: str | None = None, finished: bool = False,
        now_ts: int | None = None,
    ) -> bool:
        """刊登任务条件相变（与 cas_task_phase 同构：绝不相位回退，
        非终态目标一并清 finished_at）。"""
        if to_phase not in VALID_TASK_PHASE:
            raise ValueError(f"非法任务状态: {to_phase}")
        t = now_ts or now()
        frm = tuple(from_phases)
        marks = ",".join("?" for _ in frm)
        clears_finish = (
            not finished and to_phase not in
            ("completed", "completed_with_issues", "failed", "cancelled"))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE pt_listing_task SET phase=?, error=?, updated_at=?"
                + (", finished_at=?" if finished else "")
                + (", finished_at=NULL" if clears_finish else "")
                + f" WHERE id=? AND phase IN ({marks})",
                (to_phase, error, t, t, task_id, *frm) if finished
                else (to_phase, error, t, task_id, *frm),
            )
            return int(cur.rowcount) == 1

    def set_listing_resume_pending(self, task_id: str, on: bool = True
                                   ) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE pt_listing_task SET resume_pending=?, updated_at=?"
                " WHERE id=?", (1 if on else 0, now(), task_id))

    # ---- S3-B2 P1-2：任务执行租约（跨实例互斥）----
    # 协议=take →（每次写库前）renew → 终态/退出 release。接管只能
    # 发生在租约过期后（BEGIN IMMEDIATE 原子判定），因此「renew 成功
    # →写」之间不可能被夺走（TTL 远大于单次 SQLite 写毫秒级耗时），
    # 过期执行者 renew 失败即不得再写（LeaseLostError 由调用方消化）。

    def take_listing_lease(self, task_id: str, owner: str, ttl: float = 30.0,
                           now_ts: float | None = None) -> bool:
        """获取任务执行租约：无主或已过期才可得（原子）。"""
        ts = float(now_ts if now_ts is not None else time.time())
        with self._tx():
            row = self._db.execute(
                "SELECT lease_owner, lease_expires FROM pt_listing_task"
                " WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return False
            if row["lease_owner"] and row["lease_owner"] != owner \
                    and float(row["lease_expires"] or 0) > ts:
                return False          # 别的执行者活着持有
            cur = self._db.execute(
                "UPDATE pt_listing_task SET lease_owner=?, lease_expires=?,"
                " updated_at=? WHERE id=?",
                (owner, ts + float(ttl), int(ts), task_id))
            if int(cur.rowcount) != 1:
                return False
            # 孤儿回收：take 成功 ⇒ 前任租约必已过期 ⇒ 其下一次 renew
            # 必失败、绝不会再写——它遗留的 running 条目安全归队 pending
            # （有受理号的条目重新领取后只做终态轮询，不二次提交）。
            self._db.execute(
                "UPDATE pt_listing_item SET state='pending', updated_at=?"
                " WHERE task_id=? AND state='running'",
                (int(ts), task_id))
            return True

    def renew_listing_lease(self, task_id: str, owner: str,
                            ttl: float = 30.0,
                            now_ts: float | None = None) -> bool:
        """续租：仅持有者本人且未过期才成功（失败=已被接管/过期，
        此后不得再写该任务任何状态）。"""
        ts = float(now_ts if now_ts is not None else time.time())
        with self._tx():
            cur = self._db.execute(
                "UPDATE pt_listing_task SET lease_expires=?, updated_at=?"
                " WHERE id=? AND lease_owner=? AND lease_expires>?",
                (ts + float(ttl), int(ts), task_id, owner, ts))
            return int(cur.rowcount) == 1

    def release_listing_lease(self, task_id: str, owner: str,
                              now_ts: float | None = None) -> None:
        """释放租约（终态/正常退出；仅本人持有时清）。"""
        ts = float(now_ts if now_ts is not None else time.time())
        with self._tx():
            self._db.execute(
                "UPDATE pt_listing_task SET lease_owner='', lease_expires=0,"
                " updated_at=? WHERE id=? AND lease_owner=?",
                (int(ts), task_id, owner))

    # ---- S3-B2-F 第四阶段：真实 Listings 提交意图（写前日志）----
    # 服务对象=LiveListingGateway（禁用态，唯一构造点在离线契约测试）。
    # 核心不变量：intent 落库先于外部调用；无 receipt 的 intent 在恢复
    # 时一律 unknown——绝不自动重发/自动撤销（真实 API 无「同令牌=同受
    # 理号」语义，W-A/W-B 崩溃窗口不可区分）。表由 ensure_… 惰性创建，
    # 绝不进 SCHEMA_VERSION 主迁移链（禁用功能不触碰存量库 schema）。

    def ensure_submit_intent_schema(self) -> None:
        """幂等建表（CREATE IF NOT EXISTS；可重复执行）。"""
        with self._tx():
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS pt_submit_intent ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " task_id TEXT NOT NULL, pid TEXT NOT NULL,"
                " sku TEXT NOT NULL, store_id TEXT NOT NULL,"
                " marketplace_id TEXT NOT NULL, env TEXT NOT NULL,"
                " idem_token TEXT NOT NULL UNIQUE,"
                " payload_sha TEXT NOT NULL,"
                " state TEXT NOT NULL,"       # open|dispatched|receipted|cancelled
                " submission_id TEXT NOT NULL DEFAULT '',"
                " dispatched_at INTEGER NOT NULL DEFAULT 0,"
                " acted_by TEXT NOT NULL DEFAULT '',"
                " acted_at INTEGER NOT NULL DEFAULT 0,"
                " note TEXT NOT NULL DEFAULT '',"
                " created_at INTEGER NOT NULL,"
                " updated_at INTEGER NOT NULL,"
                " UNIQUE(task_id, pid, sku))")

    def _intent_row(self, task_id: str, pid: str, sku: str) -> dict | None:
        r = self._db.execute(
            "SELECT * FROM pt_submit_intent WHERE task_id=? AND pid=?"
            " AND sku=?", (task_id, pid, sku)).fetchone()
        return dict(r) if r is not None else None

    def create_submit_intent(self, *, task_id: str, pid: str, sku: str,
                             store_id: str, marketplace_id: str,
                             env: str, idem_token: str, payload_sha: str,
                             now_ts: int | None = None) -> dict:
        """外部调用前落库（写前日志）。已有同 (task,pid,sku) 行→原样返回
        （_created=False）——存在未决（open/dispatched）意图时调用方必须
        走 unknown 恢复路径，不得重发；同令牌不同内容=指纹冲突拒绝。"""
        t = now_ts or now()
        with self._tx():
            row = self._intent_row(task_id, pid, sku)
            if row is not None:
                # 维度冲突闸（S3-C 四阶段）：同 (task,pid,sku) 已有意图
                # 但店铺/站点/env 与本次不一致=跨店混用（凭证/任务隔离
                # 被破坏）——拒绝，绝不静默复用他店意图。
                if (str(row["store_id"]) != str(store_id)
                        or str(row["marketplace_id"]) != str(marketplace_id)
                        or str(row["env"]) != str(env)):
                    raise ValueError(
                        f"提交意图维度冲突：({task_id},{pid},{sku}) 已属于"
                        f" 店铺 {row['store_id']}/{row['marketplace_id']}/"
                        f"{row['env']}，与本次 {store_id}/"
                        f"{marketplace_id}/{env} 不符（跨店隔离）")
                row["_created"] = False
                return row
            self._db.execute(
                "INSERT INTO pt_submit_intent(task_id,pid,sku,store_id,"
                "marketplace_id,env,idem_token,payload_sha,state,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'open',?,?)",
                (task_id, pid, sku, store_id, marketplace_id, env,
                 idem_token, payload_sha, t, t))
            row = self._intent_row(task_id, pid, sku)
            row["_created"] = True
            return row

    def get_submit_intent(self, task_id: str, pid: str,
                          sku: str) -> dict | None:
        with self._lock:
            return self._intent_row(task_id, pid, sku)

    def list_open_intents(self, task_id: str) -> list[dict]:
        """未决意图清单（恢复路径输入：open/dispatched 且无受理号）。"""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM pt_submit_intent WHERE task_id=? AND"
                " state IN ('open','dispatched')", (task_id,)).fetchall()]

    def mark_intent_dispatched(self, task_id: str, pid: str, sku: str,
                               now_ts: int | None = None) -> bool:
        """发出前打点（尽力缩小 W-A；正确性不依赖它——恢复不据此重发）。"""
        t = now_ts or now()
        with self._tx():
            cur = self._db.execute(
                "UPDATE pt_submit_intent SET state='dispatched',"
                " dispatched_at=?, updated_at=? WHERE task_id=? AND pid=?"
                " AND sku=? AND state='open'", (t, t, task_id, pid, sku))
            return int(cur.rowcount) == 1

    def set_intent_receipt(self, task_id: str, pid: str, sku: str,
                           submission_id: str,
                           now_ts: int | None = None) -> bool:
        """受理落库（open/dispatched → receipted；同值重放 no-op）。"""
        t = now_ts or now()
        with self._tx():
            row = self._intent_row(task_id, pid, sku)
            if row is None or row["state"] == "cancelled":
                return False
            if row["state"] == "receipted":
                return str(row["submission_id"]) == str(submission_id)
            cur = self._db.execute(
                "UPDATE pt_submit_intent SET state='receipted',"
                " submission_id=?, updated_at=? WHERE task_id=? AND pid=?"
                " AND sku=? AND state IN ('open','dispatched')",
                (str(submission_id), t, task_id, pid, sku))
            return int(cur.rowcount) == 1

    def cancel_submit_intent(self, task_id: str, pid: str, sku: str, *,
                             acted_by: str, note: str = "",
                             now_ts: int | None = None) -> dict:
        """人工「确认未提交」撤销意图（显式出路之一；全审计）。
        已有受理（receipted）=平台事实，拒绝撤销。"""
        t = now_ts or now()
        with self._tx():
            row = self._intent_row(task_id, pid, sku)
            if row is None:
                raise KeyError("提交意图不存在")
            if row["state"] == "receipted":
                raise ValueError(
                    f"已有受理号 {row['submission_id']}——受理是平台事实，"
                    "不可撤销；请走结果核对（confirm/轮询）路径")
            self._db.execute(
                "UPDATE pt_submit_intent SET state='cancelled',"
                " acted_by=?, acted_at=?, note=?, updated_at=?"
                " WHERE task_id=? AND pid=? AND sku=?",
                (acted_by, t, note, t, task_id, pid, sku))
            return self._intent_row(task_id, pid, sku)

    def mark_intent_platform_rejected(self, task_id: str, pid: str, sku: str,
                                      note: str = "",
                                      now_ts: int | None = None) -> bool:
        """平台**确定性拒绝**（put 阶段 400/413 类：请求未进处理管线，
        无受理号）——意图直接收敛为 rejected（区别于崩溃窗口 unknown）。

        这是网关语义补全（S3-C 四阶段）：ListingRejectError 从 put 抛出
        时调用。rejected 不在 open/dispatched 未决集里（恢复路径不会把
        它当 unknown 人工核对）；与人工动作区分：acted_by='platform'。
        receipted 后的拒绝走查询路径（get REJECTED），不经此方法。
        """
        t = now_ts or now()
        with self._tx():
            cur = self._db.execute(
                "UPDATE pt_submit_intent SET state='rejected',"
                " acted_by='platform', acted_at=?, note=?, updated_at=?"
                " WHERE task_id=? AND pid=? AND sku=? AND"
                " state IN ('open','dispatched')",
                (t, note[:400], t, task_id, pid, sku))
            return int(cur.rowcount) == 1

    def confirm_intent_receipt(self, task_id: str, pid: str, sku: str,
                               submission_id: str, *, acted_by: str,
                               note: str = "",
                               now_ts: int | None = None) -> dict:
        """人工「确认已提交」补录受理号（显式出路之二；全审计）。
        撤销后的意图不可再补录（须重走新一轮意图）。"""
        t = now_ts or now()
        with self._tx():
            row = self._intent_row(task_id, pid, sku)
            if row is None:
                raise KeyError("提交意图不存在")
            if row["state"] == "cancelled":
                raise ValueError("意图已撤销——重发须走新的提交意图")
            self._db.execute(
                "UPDATE pt_submit_intent SET state='receipted',"
                " submission_id=?, acted_by=?, acted_at=?, note=?,"
                " updated_at=? WHERE task_id=? AND pid=? AND sku=?",
                (str(submission_id), acted_by, t, note, t,
                 task_id, pid, sku))
            return self._intent_row(task_id, pid, sku)

    def consume_listing_resume_pending(self, task_id: str) -> bool:
        """原子消费「继续」意图（重复点击/并发续跑方只有一个拿到）。"""
        with self._lock:
            cur = self._db.execute(
                "UPDATE pt_listing_task SET resume_pending=0, updated_at=?"
                " WHERE id=? AND resume_pending=1", (now(), task_id))
            return int(cur.rowcount) == 1

    def get_listing_task(self, task_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_listing_task WHERE id=?",
                (task_id,)).fetchone()
            if r is None:
                return None
            d = dict(r)
            done = self._db.execute(
                f"SELECT COUNT(*) FROM pt_listing_item WHERE task_id=? AND"
                f" state IN"
                f" ({','.join('?' for _ in LISTING_ITEM_FINISHED_STATES)})",
                (task_id, *LISTING_ITEM_FINISHED_STATES),
            ).fetchone()[0]
            d["progress_done"] = int(done)
            return d

    def listing_tasks(self, *, store_id: str | None = None,
                      marketplace_id: str | None = None,
                      env: str | None = None) -> list[dict]:
        q = "SELECT * FROM pt_listing_task WHERE 1=1"
        args: list[Any] = []
        if store_id is not None:
            q += " AND store_id=?"
            args.append(store_id)
        if marketplace_id is not None:
            q += " AND marketplace_id=?"
            args.append(marketplace_id)
        if env is not None:
            _check_env(env)
            q += " AND env=?"
            args.append(env)
        q += " ORDER BY created_at DESC, id"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def claim_listing_item(self, task_id: str,
                           now_ts: int | None = None) -> dict | None:
        """原子领取一个 pending 条目置 running（只在任务相位=running；
        返回 {id,pid,sku} 或 None。不烧重试预算（与 claim_item 同语义）。"""
        t = now_ts or now()
        with self._tx():
            phase = self._db.execute(
                "SELECT phase FROM pt_listing_task WHERE id=?",
                (task_id,)).fetchone()
            if phase is None or phase["phase"] != "running":
                return None
            row = self._db.execute(
                "SELECT id, pid, sku FROM pt_listing_item WHERE task_id=?"
                " AND state='pending' ORDER BY id LIMIT 1", (task_id,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE pt_listing_item SET state='running',"
                " stage='precheck', error=NULL, error_kind=NULL,"
                " updated_at=? WHERE id=?", (t, row["id"]),
            )
            return {"id": int(row["id"]), "pid": str(row["pid"]),
                    "sku": str(row["sku"])}

    _LISTING_ITEM_UPDATABLE = (
        "pid", "sku", "parent_sku", "variant_role", "content_sha",
        "assignment_id", "pt_name", "schema_cache_key", "schema_version",
        "attr_revision", "stage", "state", "attempt", "error_kind",
        "error", "check_json", "submission_id", "submitted_at",
        "submit_result_json", "platform_status", "platform_checked_at",
        "platform_json")

    def update_listing_item(
        self, task_id: str, pid: str, sku: str, *, state: str | None = None,
        stage: str | None = None, error: str | None = None,
        error_kind: str | None = None, check: dict | None = None,
        submission_id: str | None = None, submitted_at: int | None = None,
        submit_result: dict | None = None, content_sha: str | None = None,
        assignment_id: int | None = None, pt_name: str | None = None,
        schema_cache_key: str | None = None,
        schema_version: str | None = None, attr_revision: str | None = None,
        platform_status: str | None = None, platform: dict | None = None,
        now_ts: int | None = None,
    ) -> None:
        """条目更新唯一入口（状态/阶段/快照回填/提交结果；列白名单，
        未给的列不动）。attempt 仅在 retryable_failed 时 +1；终态同时
        更新任务 last_pid。check/submit_result 存 JSON 文本。
        platform_status/platform（S3-B2 v9）：平台回执阶段与契约载荷
        （{source,request_id,response_id,submitted_at,issues[]}——
        issues 每条 {code,message,...} 原样透传，条数不限）。"""
        if state is not None and state not in VALID_LISTING_ITEM_STATE:
            raise ValueError(f"非法条目状态: {state}")
        if stage is not None and stage not in VALID_LISTING_ITEM_STAGE:
            raise ValueError(f"非法处理阶段: {stage}")
        if platform_status is not None and platform_status \
                not in VALID_PLATFORM_STATUS:
            raise ValueError(f"非法平台回执状态: {platform_status}")
        t = now_ts or now()
        with self._lock:
            row = self._db.execute(
                "SELECT id, attempt FROM pt_listing_item WHERE task_id=?"
                " AND pid=? AND sku=?", (task_id, pid, sku)).fetchone()
            if row is None:
                raise KeyError(f"刊登任务 {task_id} 没有条目 {pid}/{sku}")
            sets = ["updated_at=?"]
            args: list[Any] = [t]
            if state is not None:
                sets.append("state=?")
                args.append(state)
                sets.append("attempt=?")
                args.append(int(row["attempt"])
                            + (1 if state == "retryable_failed" else 0))
            if stage is not None:
                sets.append("stage=?")
                args.append(stage)
            if error is not None:
                sets.append("error=?")
                args.append(str(error)[:500])
            if error_kind is not None:
                sets.append("error_kind=?")
                args.append(error_kind)
            if check is not None:
                sets.append("check_json=?")
                args.append(json.dumps(check, ensure_ascii=False))
            if submission_id is not None:
                sets.append("submission_id=?")
                args.append(submission_id)
            if submitted_at is not None:
                sets.append("submitted_at=?")
                args.append(submitted_at)
            if submit_result is not None:
                sets.append("submit_result_json=?")
                args.append(json.dumps(submit_result, ensure_ascii=False))
            if platform_status is not None:
                sets.append("platform_status=?")
                args.append(platform_status)
                sets.append("platform_checked_at=?")
                args.append(t)
            if platform is not None:
                sets.append("platform_json=?")
                args.append(json.dumps(platform, ensure_ascii=False))
                sets.append("platform_checked_at=?")
                args.append(t)
            for col, val in (("content_sha", content_sha),
                             ("assignment_id", assignment_id),
                             ("pt_name", pt_name),
                             ("schema_cache_key", schema_cache_key),
                             ("schema_version", schema_version),
                             ("attr_revision", attr_revision)):
                if val is not None:
                    sets.append(f"{col}=?")
                    args.append(val)
            args.append(int(row["id"]))
            self._db.execute(
                f"UPDATE pt_listing_item SET {', '.join(sets)} WHERE id=?",
                args)
            if state in LISTING_ITEM_FINISHED_STATES:
                self._db.execute(
                    "UPDATE pt_listing_task SET last_pid=?, updated_at=?"
                    " WHERE id=?", (pid, t, task_id))
            else:
                self._db.execute(
                    "UPDATE pt_listing_task SET updated_at=? WHERE id=?",
                    (t, task_id))

    def listing_task_items(
        self, task_id: str, states: Iterable[str] | None = None,
    ) -> list[dict]:
        q = "SELECT * FROM pt_listing_item WHERE task_id=?"
        args: list[Any] = [task_id]
        if states:
            ss = [s for s in states if s]
            if ss:
                q += (" AND state IN ("
                      + ",".join("?" for _ in ss) + ")")
                args.extend(ss)
        q += " ORDER BY id"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def listing_item_counts(self, task_id: str) -> dict:
        """按状态计数（进度/终态推导真源——不依赖内存计数器）。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) AS n FROM pt_listing_item WHERE"
                " task_id=? GROUP BY state", (task_id,)).fetchall()
        counts = {s: 0 for s in VALID_LISTING_ITEM_STATE}
        total = 0
        for r in rows:
            counts[r["state"]] = int(r["n"])
            total += int(r["n"])
        counts["total"] = total
        return counts

    def requeue_listing_retryable(self, task_id: str, max_attempts: int,
                                  now_ts: int | None = None) -> int:
        """自动重试：预算内 retryable_failed 拉回 pending（attempt/error
        保留；与分类同语义）。blocked/已受理条目绝不在自动重试范围内。"""
        t = now_ts or now()
        with self._lock:
            cur = self._db.execute(
                "UPDATE pt_listing_item SET state='pending', updated_at=?"
                " WHERE task_id=? AND state='retryable_failed' AND"
                " attempt<?", (t, task_id, int(max_attempts)),
            )
            return int(cur.rowcount)

    def retry_listing_failed(self, task_id: str,
                             now_ts: int | None = None) -> int:
        """显式重试（用户操作）：blocked（人工修复后重检）与
        retryable/permanent_failed 拉回 pending。completed 条目绝不动
        ——其中已受理的提交（submission_id 已落库）不允许盲目重试提交，
        这是验收令硬约束。"""
        t = now_ts or now()
        with self._lock:
            cur = self._db.execute(
                "UPDATE pt_listing_item SET state='pending', updated_at=?"
                " WHERE task_id=? AND state IN ('blocked',"
                " 'retryable_failed', 'permanent_failed')", (t, task_id),
            )
            return int(cur.rowcount)

    def recover_listing_interrupted(self, now_ts: int | None = None
                                    ) -> list[str]:
        """启动恢复（与 recover_interrupted 同构，作用于刊登表）：
        running/pausing/cancelling → interrupted；其中 running 条目 →
        pending（终态条目原样保留——已受理提交绝不重发）。

        S3-B2 P1-2：**租约感知**——执行租约仍构成持有（owner 非空、
        未过期、owner 进程活着）=还有实例在跑，不打断（多实例启动不
        再抢断活任务）；只恢复无主/过期/owner 进程已死的残留任务
        （lease_holds）。lease_expires 用 epoch 秒（float），与 now()
        （整数秒）不可比——持有判定基准取 time.time()。
        """
        t = now_ts or now()
        ts = time.time()
        with self._tx():
            ids = [r["id"] for r in self._db.execute(
                "SELECT id, lease_owner, lease_expires FROM pt_listing_task"
                " WHERE phase IN ('running','pausing','cancelling')"
            ).fetchall()
                if not lease_holds(str(r["lease_owner"] or ""),
                                   float(r["lease_expires"] or 0), ts)]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            self._db.execute(
                f"UPDATE pt_listing_task SET phase='interrupted',"
                f" updated_at=?, resume_pending=0, lease_owner='',"
                f" lease_expires=0 WHERE id IN ({marks})",
                [t, *ids])
            self._db.execute(
                f"UPDATE pt_listing_item SET state='pending',"
                f" updated_at=? WHERE task_id IN ({marks})"
                " AND state='running'", [t, *ids],
            )
        return ids

    def latest_listing_item(self, store_id: str, marketplace_id: str,
                            env: str, pid: str) -> dict | None:
        """该商品最近一次刊登任务条目（新任务优先；工作台提交步回读用
        ——只读展示，不驱动任何写操作）。"""
        _check_env(env)
        with self._lock:
            rows = self._db.execute(
                "SELECT i.*, t.created_at AS t_created, t.env AS t_env,"
                " t.id AS t_id FROM pt_listing_item i JOIN pt_listing_task"
                " t ON t.id=i.task_id WHERE t.store_id=? AND"
                " t.marketplace_id=? AND t.env=? AND i.pid=?"
                " ORDER BY t.created_at DESC, i.id DESC",
                (store_id, marketplace_id, env, pid)).fetchall()
        for r in rows:                     # 已按新→旧排，首个即最新
            return dict(r)
        return None

    @staticmethod
    def platform_view(item: dict) -> dict:
        """条目平台回执视图（S3-B2 v9 读侧契约；纯推导零写库）。

        新行（platform_json 有值）：直接给 {status,source,request_id,
        response_id,submitted_at,last_checked_at,issues,mock}。
        旧行（v8 历史遗留，platform_status='not_submitted' 且无
        platform_json）：按 state/submit_result 推导等价视图——历史
        Mock 记录不动一个字节，展示语义对齐新契约：
          completed+受理        → accepted(mock)
          permanent_failed+受理 → rejected(mock)
          其余有受理            → unknown(mock)（受理后从未到终态）
          无受理                → not_submitted
        """
        pj = None
        if item.get("platform_json"):
            try:
                pj = json.loads(item["platform_json"])
            except (ValueError, TypeError):
                pj = None
        if pj is not None:
            return {
                "status": str(item.get("platform_status") or "unknown"),
                "source": str(pj.get("source") or ""),
                "request_id": str(pj.get("request_id") or ""),
                "response_id": str(pj.get("response_id") or ""),
                "submitted_at": item.get("submitted_at"),
                "last_checked_at": item.get("platform_checked_at"),
                "issues": list(pj.get("issues") or []),
                "mock": bool(pj.get("mock")),
            }
        sid = str(item.get("submission_id") or "")
        st = str(item.get("state") or "")
        status = "not_submitted"
        if sid:
            if st == "completed":
                status = "accepted"
            elif st == "permanent_failed":
                status = "rejected"
            else:
                status = "unknown"
        try:
            sr = json.loads(item.get("submit_result_json") or "null")
        except (ValueError, TypeError):
            sr = None
        sr = sr if isinstance(sr, dict) else {}
        return {
            "status": status,
            "source": "mock" if sid else "",
            "request_id": sid,
            "response_id": str(sr.get("submissionId")
                               or sr.get("response_id") or ""),
            "submitted_at": item.get("submitted_at"),
            "last_checked_at": item.get("updated_at"),
            "issues": list(sr.get("issues") or []),
            "mock": bool(sr.get("mock", bool(sid))),
        }

    # ---- S2d（v8）：校验报告快照 + 异常复核队列 ----

    def _upsert_exceptions_tx(
        self, store_id: str, marketplace_id: str, pid: str, sku: str,
        env: str, exceptions: list[dict], source: str, t: int,
        *, prune: bool = True,
    ) -> None:
        """异常行 upsert（**调用方必须已持锁并开事务**，公开方法不得
        在事务内互调——本私有方法为 save_validation / upsert_review_
        exceptions 共用体）。

        键=(store,marketplace,pid,sku,env,type,path)：open 行存在→刷新
        detail（重复校验不堆积）；不存在→开新 open 行（origin=engine，
        引擎只开不解决）。当前批次没出现的 open 行→auto_superseded
        （引擎关闭自己开的行；resolved 是历史事实永不改写）。
        prune=False=局部陈述（如快照失效单票），不关任何既有 open 行。
        """
        keys: set[tuple[str, str]] = set()
        for exc in exceptions:
            etype = str(exc.get("type") or "")
            if etype not in VALID_EXCEPTION_TYPES:
                raise ValueError(f"非法异常类型: {etype!r}")
            path = str(exc.get("path") or "")
            keys.add((etype, path))
            detail = json.dumps({
                "entries": exc.get("entries") or [],
                "summary": str(exc.get("summary") or ""),
            }, ensure_ascii=False)
            row = self._db.execute(
                "SELECT id FROM pt_review_exception WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND sku=? AND env=? AND"
                " exception_type=? AND attribute_path=? AND status='open'",
                (store_id, marketplace_id, pid, sku, env, etype, path),
            ).fetchone()
            if row is not None:
                self._db.execute(
                    "UPDATE pt_review_exception SET detail_json=?,"
                    " source=?, updated_at=? WHERE id=?",
                    (detail, source, t, int(row["id"])))
            else:
                self._db.execute(
                    "INSERT INTO pt_review_exception(store_id,"
                    " marketplace_id, pid, sku, env, exception_type,"
                    " attribute_path, status, origin, detail_json, source,"
                    " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (store_id, marketplace_id, pid, sku, env, etype, path,
                     "open", "engine", detail, source, t, t))
        # 本轮没出现的 open 行 → auto_superseded（含空批次=全清：校验
        # 全过就是「队列该清空」的明确事实；没事可说的调用方根本不该调）
        if prune:
            q = ("UPDATE pt_review_exception SET status='auto_superseded',"
                 " updated_at=? WHERE store_id=? AND marketplace_id=? AND"
                 " pid=? AND sku=? AND env=? AND status='open'")
            args: list[Any] = [t, store_id, marketplace_id, pid, sku, env]
            if keys:
                ph = ",".join("?" for _ in keys)
                q += (f" AND (exception_type || ' ' || attribute_path)"
                      f" NOT IN ({ph})")
                args.extend(f"{ty} {p}" for ty, p in keys)
            self._db.execute(q, args)

    def save_validation(
        self, store_id: str, marketplace_id: str, pid: str, env: str, *,
        sku: str = "", fingerprint: dict | None = None,
        report: dict, exceptions: list[dict] | None = None,
        source: str = "", now_ts: int | None = None,
    ) -> int:
        """落一份校验报告快照（is_current 单活；旧报告标 superseded 保留
        历史）+ 同事务 upsert 异常队列。report=validate.report_to_dict 形状；
        exceptions=validate.report_exceptions 形状（缺省时按 report 的
        blocking 条目现算）。validated 只是本地结论——存库不改任何商品/
        属性/任务状态。"""
        _check_env(env)
        t = now_ts or now()
        fp = fingerprint or {}
        exceptions = report.get("exceptions") if exceptions is None \
            else exceptions
        if exceptions is None:
            from pt.validate import report_from_dict, report_exceptions
            exceptions = report_exceptions(report_from_dict(report))
        types_csv = ",".join(sorted({str(e.get("type"))
                                     for e in exceptions if e.get("type")}))
        with self._tx():
            old = self._db.execute(
                "SELECT id FROM pt_validation WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND sku=? AND env=? AND"
                " is_current=1",
                (store_id, marketplace_id, pid, sku, env)).fetchone()
            cur = self._db.execute(
                "INSERT INTO pt_validation(store_id, marketplace_id, pid,"
                " sku, env, content_sha, assignment_id, pt_name,"
                " schema_cache_key, schema_version, attr_revision,"
                " validated, exception_types, report_json, is_current,"
                " source, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (store_id, marketplace_id, pid, sku, env,
                 str(fp.get("content_sha") or ""),
                 fp.get("assignment_id"),
                 str(fp.get("pt_name") or ""),
                 str(fp.get("schema_cache_key") or ""),
                 str(fp.get("schema_version") or ""),
                 str(fp.get("attr_revision") or ""),
                 1 if report.get("validated") else 0,
                 types_csv,
                 json.dumps(report, ensure_ascii=False),
                 source, t, t))
            new_id = int(cur.lastrowid)
            if old is not None:
                self._db.execute(
                    "UPDATE pt_validation SET is_current=0, superseded_by=?,"
                    " updated_at=? WHERE id=?",
                    (new_id, t, int(old["id"])))
            self._upsert_exceptions_tx(
                store_id, marketplace_id, pid, sku, env, exceptions,
                source, t)
        return new_id

    def current_validation(self, store_id: str, marketplace_id: str,
                           pid: str, env: str, sku: str = "",
                           ) -> dict | None:
        """现行校验报告（is_current=1；含 report_json 全文——调用方自行
        json.loads）。"""
        _check_env(env)
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_validation WHERE store_id=? AND"
                " marketplace_id=? AND pid=? AND sku=? AND env=? AND"
                " is_current=1 ORDER BY id DESC",
                (store_id, marketplace_id, pid, sku, env)).fetchone()
        return _row(r)

    def upsert_review_exceptions(
        self, store_id: str, marketplace_id: str, pid: str, env: str, *,
        sku: str = "", exceptions: list[dict], source: str = "",
        now_ts: int | None = None, prune: bool = True,
    ) -> None:
        """独立 upsert（不落报告行）——快照失效（context_stale）等
        「没跑完整校验也要开工单」的场景专用（该场景传 prune=False：
        只加票不清票）。"""
        _check_env(env)
        t = now_ts or now()
        with self._tx():
            self._upsert_exceptions_tx(
                store_id, marketplace_id, pid, sku, env, exceptions,
                source, t, prune=prune)

    def review_exceptions(
        self, *, store_id: str | None = None,
        marketplace_id: str | None = None, env: str = "mock",
        pid: str | None = None, sku: str | None = None,
        exception_type: str | None = None, status: str = "open",
        limit: int = 200,
    ) -> list[dict]:
        """复核队列视图：按店铺/站点/商品/SKU/类型/状态过滤（验收令
        「按商品、店铺、站点、类型查看」）。detail_json 原样带出（调用
        方自行解析）。"""
        _check_env(env)
        if exception_type is not None \
                and exception_type not in VALID_EXCEPTION_TYPES:
            raise ValueError(f"非法异常类型: {exception_type!r}")
        if status not in VALID_EXCEPTION_STATUS and status != "all":
            raise ValueError(f"非法异常状态: {status!r}")
        q = "SELECT * FROM pt_review_exception WHERE env=?"
        args: list[Any] = [env]
        if status != "all":
            q += " AND status=?"
            args.append(status)
        for col, val in (("store_id", store_id),
                         ("marketplace_id", marketplace_id),
                         ("pid", pid), ("sku", sku),
                         ("exception_type", exception_type)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def review_exception(self, exception_id: int) -> dict | None:
        """单条异常（resolve 前回显原值/详情用）。"""
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_review_exception WHERE id=?",
                (int(exception_id),)).fetchone()
        return _row(r)

    def resolve_review_exception(
        self, exception_id: int, *, action: str, operator: str,
        note: str = "", original_value: Any = None,
        resolved_value: Any = None, now_ts: int | None = None,
    ) -> dict:
        """人工处置唯一入口（验收令三：明确授权 API + 审计）。

        只能动 open 行（resolved/auto_superseded → ValueError，API 层
        映射 409——重复处置是错误不是幂等）。审计四件套落齐：操作人/
        时间/原值/修改值 + 备注。**本方法只记录复核决定，不碰属性行**
        ——真正改值必须走 set_review_status(human=True) / edit_attribute
        （S1 人工铁律），由 API 层在同一请求里显式编排。
        """
        if action not in VALID_RESOLUTION_ACTION:
            raise ValueError(f"非法处置动作: {action!r}")
        operator = str(operator or "").strip()
        if not operator:
            raise ValueError("处置必须留操作人（人工授权不可匿名）")
        t = now_ts or now()

        def _ser(v: Any) -> str | None:
            return None if v is None else json.dumps(
                v, ensure_ascii=False)

        with self._lock:
            row = self._db.execute(
                "SELECT * FROM pt_review_exception WHERE id=?",
                (int(exception_id),)).fetchone()
            if row is None:
                raise KeyError(f"异常 {exception_id} 不存在")
            if row["status"] != "open":
                raise ValueError(
                    f"异常 {exception_id} 已是 {row['status']}——只能处置"
                    " open 行（处置记录是历史事实，不可重写）")
            self._db.execute(
                "UPDATE pt_review_exception SET status='resolved',"
                " resolution_action=?, original_value=COALESCE(?,"
                " original_value), resolved_value=?, resolved_by=?,"
                " resolved_at=?, resolution_note=?, updated_at=? WHERE id=?",
                (action, _ser(original_value), _ser(resolved_value),
                 operator, t,
                 str(note)[:500] if note else None, t,
                 int(exception_id)))
            r = self._db.execute(
                "SELECT * FROM pt_review_exception WHERE id=?",
                (int(exception_id),)).fetchone()
            return dict(r)

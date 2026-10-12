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

数据库位置：默认 %LOCALAPPDATA%\\WZDesktop\\pt\\pt.sqlite3（代码更新/
robocopy 部署不会覆盖），环境变量 WZD_PT_DATA 可改根目录；测试直接
传显式路径。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

_LOG = logging.getLogger("pt.store")   # 模块 2 要求 11：异常必须可诊断

SCHEMA_VERSION = 2

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
VALID_TASK_PHASE = {"created", "running", "paused", "failed",
                    "cancelled", "done", "interrupted"}
VALID_ITEM_STATE = {"pending", "running", "done", "failed",
                    "skipped", "cancelled"}
VALID_CHOSEN_BY = {"ai", "human"}


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
-- env：任务在哪个环境跑的，mock 任务与 live 任务互不可见混淆）
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
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  finished_at    INTEGER
);

CREATE TABLE IF NOT EXISTS pt_task_item(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  pid        TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'pending',
  attempt    INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  updated_at INTEGER NOT NULL,
  UNIQUE(task_id, pid)
);
CREATE INDEX IF NOT EXISTS idx_task_item
  ON pt_task_item(task_id, state);
"""

# 索引不单独成份：全部在 _SCHEMA_SQL 里（CREATE INDEX IF NOT EXISTS）。
# v1→v2 迁移必须**先**给旧表补 env 列/重建，**最后**再跑 executescript
# 建索引——索引引用 env 列，v1 旧表（pt_assignment 等）还没有它，
# 先跑脚本会直接 OperationalError（真实老库的坑，2026-10-12 二轮修正）。


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
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
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
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
            if v == 1:
                # v1→v2（修正 3）：env 隔离列。三张带唯一键的表重建
                # （唯一键必须含 env），三张 ALTER 加列；旧行一律落
                # legacy_unverified 隔离区（修正 1：历史来源无法证明，
                # 不得自动获得 live 官方资格）。
                self._migrate_1_to_2()
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
            raise RuntimeError(f"未知 schema 版本 v{v}")

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
        #    索引安全；重建表丢弃的旧索引在这里重建）
        self._db.executescript(_SCHEMA_SQL)
        _LOG.info("pt.store v1→v2 迁移完成（env 隔离列；旧行与历史"
                  " live 验证记录一律降级 %s，不自动获得官方资格）"
                  " db=%s", LEGACY_ENV, self.path)

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

    # ---- 任务（逐商品条目 = 恢复的真源） ----

    def create_task(
        self, task_id: str, kind: str, store_id: str,
        marketplace_id: str, pids: Iterable[str],
        now_ts: int | None = None, env: str = "live",
    ) -> int:
        _check_env(env)
        t = now_ts or now()
        seen: list[str] = []
        for p in pids or []:
            p = str(p).strip()
            if p and p not in seen:
                seen.append(p)
        with self._tx():
            self._db.execute(
                "INSERT INTO pt_task(id,kind,store_id,marketplace_id,env,"
                "phase,progress_done,progress_total,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,0,?,?,?)",
                (task_id, kind, store_id, marketplace_id, env, "created",
                 len(seen), t, t),
            )
            self._db.executemany(
                "INSERT INTO pt_task_item(task_id,pid,state,attempt,"
                "updated_at) VALUES(?,?,'pending',0,?)",
                [(task_id, p, t) for p in seen],
            )
        return len(seen)

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

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM pt_task WHERE id=?", (task_id,)).fetchone()
            if r is None:
                return None
            d = dict(r)
            done = self._db.execute(
                "SELECT COUNT(*) FROM pt_task_item WHERE task_id=? AND"
                " state IN ('done','skipped')", (task_id,),
            ).fetchone()[0]
            d["progress_done"] = int(done)
            return d

    def tasks(self, phase: str | None = None) -> list[dict]:
        q = "SELECT * FROM pt_task"
        args: list[Any] = []
        if phase is not None:
            q += " WHERE phase=?"
            args.append(phase)
        q += " ORDER BY created_at DESC, id"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def set_item_state(
        self, task_id: str, pid: str, state: str,
        error: str | None = None, now_ts: int | None = None,
    ) -> None:
        """state→'running' 时 attempt+1（重试即重新起跑）。"""
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
            attempt = row["attempt"] + (1 if state == "running" else 0)
            self._db.execute(
                "UPDATE pt_task_item SET state=?, error=?, attempt=?,"
                " updated_at=? WHERE id=?",
                (state, error, attempt, t, row["id"]),
            )
            if state in ("done", "failed"):
                self._db.execute(
                    "UPDATE pt_task SET last_pid=?, updated_at=? WHERE id=?",
                    (pid, t, task_id),
                )
            else:
                self._db.execute(
                    "UPDATE pt_task SET updated_at=? WHERE id=?",
                    (t, task_id),
                )

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

    def retry_failed(self, task_id: str) -> int:
        """把 failed 条目拉回 pending（显式重试），返回条数。"""
        t = now()
        with self._lock:
            cur = self._db.execute(
                "UPDATE pt_task_item SET state='pending', error=NULL,"
                " updated_at=? WHERE task_id=? AND state='failed'",
                (t, task_id),
            )
            return int(cur.rowcount)

    def recover_interrupted(self, now_ts: int | None = None) -> list[str]:
        """启动恢复：running/paused 任务→interrupted；其中 running 条目
        →pending（done/failed/skipped 保留；失败重试走 retry_failed）。
        恢复以 pt_task_item 为准，不读 last_pid（修正 2）。"""
        t = now_ts or now()
        with self._tx():
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM pt_task WHERE phase IN"
                " ('running','paused')").fetchall()]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            self._db.execute(
                f"UPDATE pt_task SET phase='interrupted', updated_at=?"
                f" WHERE id IN ({marks})", [t, *ids],
            )
            self._db.execute(
                f"UPDATE pt_task_item SET state='pending', error=NULL,"
                f" updated_at=? WHERE task_id IN ({marks})"
                " AND state='running'", [t, *ids],
            )
        return ids

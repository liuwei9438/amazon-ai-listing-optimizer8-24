# -*- coding: utf-8 -*-
"""pt.authz —— S3-C 第二阶段：多店铺 SP-API 授权管理（桌面侧，离线）。

定位与边界
----------
LWA 凭证与 refresh token 的**保管与交换全部在 worker 侧**（KV
amzstores 托管 + 服务器端 exchange，v13.9/v14.1 起）；桌面侧永远只见
短效 access_token（内存持有，不落库/不落日志/不回前端）。本模块是
桌面侧的授权**管理层**，不是新的凭证存储：

1. 每店铺独立身份三元组 (store_id, seller_id, marketplace_id)——
   构造即校验（站点必须在 18 站点表内、seller 非空、店铺必须在
   当前用户的店铺清单里），任何跨店混用都在这里被结构性拦下。
2. 令牌通道按店铺隔离：缓存键=store_id，单店失败绝不影响别店；
   令牌只存在内存，repr/日志/异常一律只有**分类**没有值。
3. 失败分类（classify_token_error）：把 worker/网络侧错误文本归为
   六类，供 API 层给出可操作的 403 文案与 smoke readiness 判定。
4. 不发起真实授权（没有 OAuth 流程）、不写任何数据库表——纯内存
   状态 + 注入回调，天然可离线测试。

安全不变量（测试锚点见 test_pt_authz.py）
----------------------------------------
- 令牌值绝不出现：__repr__/异常/审计/状态输出只含分类与时间戳。
- 未授权店铺（probe 失败或从未 probe）不得拿到令牌——token() 在
  无缓存时必须重新 probe，probe 失败即抛 AuthzError（带分类）。
- 缓存命中严格按 store_id 隔离；identity_of 每次重读清单（不缓存
  店铺归属，防止清单变化后旧身份残留）。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .spapi_client import REGION_BY_MARKETPLACE

# 授权状态（对外稳定词表；前端/报告用）
AUTH_STATES = ("unknown", "ok", "failed")
# 失败分类（六类；文案只增不改，测试锚定字符串前缀）
FAILURE_CATEGORIES = (
    "no_authorization",     # 店铺未绑 LWA / 无权限 / 不存在
    "app_credential",       # 公共应用凭证未配置或无效
    "refresh_failed",       # refresh token 失效/过期（invalid_grant 族）
    "server_error",         # worker/服务器侧错误
    "network",              # 连不上服务器/超时
    "unknown_error",        # 无法归类
)


class AuthzError(Exception):
    """授权不可用（携带分类；绝不携带令牌或凭证）。"""

    def __init__(self, category: str, detail: str = ""):
        super().__init__(f"[{category}] {detail}" if detail else category)
        self.category = category


# ---------------------------------------------------------------------------
# 失败分类（纯函数；规则=worker 错误文案的已知模式，逐条测试锚定）
# ---------------------------------------------------------------------------

_RULES: tuple[tuple[str, re.Pattern], ...] = (
    # 顺序即优先级：先具体后泛化
    ("refresh_failed", re.compile(
        r"invalid[_ ]grant|refresh[_ ]?token|授权已过期|令牌已过期"
        r"|token\s*expired|expired_token", re.I)),
    ("no_authorization", re.compile(
        r"店铺不存在|无权限|未绑定|未授权|not\s*authorized|no\s*auth"
        r"|forbidden|没有授权|无此店铺", re.I)),
    ("app_credential", re.compile(
        r"应用凭证|app[_ ]credential|client[_ ]secret|lwa\s*app"
        r"|app[_ ]key|没配应用", re.I)),
    ("network", re.compile(
        r"连不上|timeout|timed?\s*out|connection|网络|unreachable"
        r"|refused|reset", re.I)),
    ("server_error", re.compile(
        r"服务器|5\d\d|internal|没返回令牌|读取失败|故障", re.I)),
)


def classify_token_error(err_text: str) -> str:
    """把 worker/网络侧错误文本归为 FAILURE_CATEGORIES 之一。"""
    t = str(err_text or "")
    for cat, pat in _RULES:
        if pat.search(t):
            return cat
    return "unknown_error"


# ---------------------------------------------------------------------------
# 店铺身份（三元组；构造即校验）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreIdentity:
    store_id: str
    seller_id: str
    marketplace_id: str
    region: str

    def __repr__(self) -> str:             # 安全习惯：无敏感值可泄
        return (f"StoreIdentity({self.store_id!r}, seller=…, "
                f"mk={self.marketplace_id}, region={self.region})")

    def as_view(self) -> dict:
        return {"store_id": self.store_id, "seller_id": self.seller_id,
                "marketplace_id": self.marketplace_id,
                "region": self.region}


def build_identity(store_rec: dict) -> StoreIdentity:
    """从服务器店铺记录构造身份（站点白名单校验 fail-closed）。

    marketplace 不在 18 站点表内=未知站点，直接拒绝（与 spapi_client
    的 REGION_BY_MARKETPLACE 同源——绝不猜区域）。
    """
    sid = str((store_rec or {}).get("id") or "").strip()
    mid = str((store_rec or {}).get("marketplace")
              or (store_rec or {}).get("marketplace_id") or "").strip()
    seller = str((store_rec or {}).get("seller_id") or "").strip()
    if not sid:
        raise AuthzError("no_authorization", "店铺记录缺少 id")
    if not seller:
        raise AuthzError("no_authorization",
                         f"店铺 {sid} 没有 seller_id（重新绑定一次）")
    region = REGION_BY_MARKETPLACE.get(mid)
    if region is None:
        raise AuthzError(
            "no_authorization",
            f"店铺 {sid} 站点 {mid or '(空)'} 不在受支持的 18 站点表内")
    return StoreIdentity(store_id=sid, seller_id=seller,
                         marketplace_id=mid, region=region)


# ---------------------------------------------------------------------------
# 授权状态记录（纯内存；令牌只在 _token 字段且绝不外显）
# ---------------------------------------------------------------------------

@dataclass
class AuthRecord:
    store_id: str
    state: str = "unknown"          # AUTH_STATES 之一
    category: str = ""              # 失败分类（state=failed 时）
    detail: str = ""                # 失败摘要（已脱敏：无令牌/无凭证）
    probed_at: float = 0.0
    expires_at: float = 0.0         # 成功时的令牌到期提示（epoch 秒）
    _token: str = field(default="", repr=False, compare=False)

    def view(self) -> dict:
        """对外视图：绝不含令牌值。"""
        return {"store_id": self.store_id, "state": self.state,
                "category": self.category,
                "detail": self.detail[:120],
                "probed_at": int(self.probed_at),
                "token_expires_in": max(
                    0, int(self.expires_at - time.time()))
                if self.expires_at else 0}


# ---------------------------------------------------------------------------
# 管理器（每 PtApi 一实例；deps 注入=可离线测试）
# ---------------------------------------------------------------------------

class StoreAuthzManager:
    """多店铺授权管理：身份校验 / 令牌通道（按店隔离+缓存）/ 状态视图。

    deps（与 PtApi.deps 同源注入）：
      stores()        -> (list[dict], err)   服务器店铺清单（用户维度）
      store_token(sid)-> str                  worker 换令牌（失败抛异常）
    audit(category, **kw)  可选审计回调（只收分类，不收令牌）。
    """

    # 令牌提前刷新余量（秒）：留 60s 缓冲，宁可早 probe 不拿将过期令牌
    TOKEN_REFRESH_MARGIN = 60.0
    # 负结果缓存（秒）：失败后短时间内不重复打 worker（防雪崩；正结果
    # 按 expires_at 自然过期）
    NEG_TTL = 30.0

    def __init__(self, stores: Callable[[], tuple],
                 store_token: Callable[[str], str],
                 audit: Callable | None = None,
                 now_fn: Callable[[], float] = time.time):
        self._stores = stores
        self._store_token = store_token
        self._audit = audit or (lambda *a, **k: None)
        self._now = now_fn
        self._lock = threading.Lock()
        self._records: dict[str, AuthRecord] = {}

    # ---- 身份（每次重读清单；不缓存归属） ----

    def identity_of(self, sid: str) -> StoreIdentity:
        stores, err = self._stores()
        if err:
            raise AuthzError("server_error", f"读店铺清单失败：{err}")
        rec = next((s for s in (stores or [])
                    if str((s or {}).get("id")) == str(sid or "").strip()),
                   None)
        if rec is None:
            raise AuthzError("no_authorization",
                             f"店铺不存在或无权使用：{sid}")
        return build_identity(rec)

    def identities(self) -> list[StoreIdentity]:
        stores, err = self._stores()
        if err:
            raise AuthzError("server_error", f"读店铺清单失败：{err}")
        out = []
        for s in stores or []:
            try:
                out.append(build_identity(s))
            except AuthzError:
                continue            # 清单里的坏行跳过，不拖垮整表
        return out

    # ---- 令牌通道（live 工作流唯一入口） ----

    def token(self, sid: str) -> str:
        """取店铺令牌（缓存→probe）。失败抛 AuthzError（带分类）。

        这是「未授权店铺不得进入真实 API 工作流」的落点：拿不到令牌
        =进不了任何 live 客户端构造。
        """
        sid = str(sid or "").strip()
        now = self._now()
        with self._lock:
            r = self._records.get(sid)
            if r is not None and r.state == "ok" \
                    and r._token \
                    and r.expires_at - now > self.TOKEN_REFRESH_MARGIN:
                return r._token
            # 负缓存：刚失败过（NEG_TTL 内）不再打 worker（防雪崩；
            # 想立刻重试走显式 probe）
            if r is not None and r.state == "failed" \
                    and now - r.probed_at < self.NEG_TTL:
                raise AuthzError(r.category, r.detail)
        self.probe(sid)            # 无有效缓存→probe（写记录）
        with self._lock:
            r = self._records.get(sid)
        if r is not None and r.state == "ok" and r._token:
            return r._token
        raise AuthzError(r.category if r else "unknown_error",
                         r.detail if r else "probe 未落记录")

    def probe(self, sid: str) -> AuthRecord:
        """主动验证一次店铺授权（worker 换令牌；非 SP-API 调用）。

        成功→ok+令牌缓存（含 expires 提示）；失败→分类落记录。
        令牌值只进内存记录，任何出口（view/异常/审计）都不带值。
        """
        sid = str(sid or "").strip()
        self.identity_of(sid)      # 归属+身份先过（403 语义在异常分类里）
        now = self._now()
        try:
            tok = str(self._store_token(sid) or "")
        except Exception as e:  # noqa: BLE001 —— 分类而非透传（防泄密）
            cat = classify_token_error(str(e))
            detail = str(e)[:160]
            with self._lock:
                self._records[sid] = AuthRecord(
                    store_id=sid, state="failed", category=cat,
                    detail=detail, probed_at=now)
            self._audit("store_auth_probe", store_id=sid,
                        result="failed", category=cat)
            return self._records[sid]
        if not tok:
            with self._lock:
                self._records[sid] = AuthRecord(
                    store_id=sid, state="failed",
                    category="server_error",
                    detail="服务器没返回令牌", probed_at=now)
            self._audit("store_auth_probe", store_id=sid,
                        result="failed", category="server_error")
            return self._records[sid]
        # 成功：令牌进内存缓存；expires 取 worker 提示（无则保守 300s）
        with self._lock:
            self._records[sid] = AuthRecord(
                store_id=sid, state="ok", probed_at=now,
                expires_at=now + 300.0, _token=tok)
        self._audit("store_auth_probe", store_id=sid, result="ok")
        return self._records[sid]

    # ---- 状态视图（默认零网络） ----

    def status(self, sid: str, *, probe: bool = False) -> dict:
        """单店授权状态。probe=False 纯读内存（零网络）；
        probe=True 才真正验证一次（打 worker，非 SP-API）。"""
        sid = str(sid or "").strip()
        ident = self.identity_of(sid)
        if probe:
            rec = self.probe(sid)
            return {**ident.as_view(), "auth": rec.view()}
        with self._lock:
            rec = self._records.get(sid)
        auth = rec.view() if rec is not None \
            else {"store_id": sid, "state": "unknown", "category": "",
                  "detail": "", "probed_at": 0, "token_expires_in": 0}
        return {**ident.as_view(), "auth": auth}

    def status_all(self, *, probe: bool = False) -> list[dict]:
        """全部店铺状态（身份必出；auth 视 probe 决定是否真验证）。"""
        out = []
        for ident in self.identities():
            if probe:
                out.append(self.status(ident.store_id, probe=True))
                continue
            with self._lock:
                rec = self._records.get(ident.store_id)
            auth = rec.view() if rec is not None else \
                {"store_id": ident.store_id, "state": "unknown",
                 "category": "", "detail": "", "probed_at": 0,
                 "token_expires_in": 0}
            out.append({**ident.as_view(), "auth": auth})
        return out

    def forget(self, sid: str) -> None:
        """丢弃单店缓存（登出/换账号/测试用；令牌随之消失）。"""
        with self._lock:
            self._records.pop(str(sid or "").strip(), None)


__all__ = ["AUTH_STATES", "FAILURE_CATEGORIES", "AuthzError",
           "StoreIdentity", "build_identity", "AuthRecord",
           "StoreAuthzManager", "classify_token_error",
           "REGION_BY_MARKETPLACE"]

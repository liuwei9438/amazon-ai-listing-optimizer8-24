# -*- coding: utf-8 -*-
"""S3-B2-F 第四阶段：真实 Listings 提交网关（**禁用态**，离线契约实现）。

与 Mock 引擎的关系
------------------
Mock 引擎（pt.listing.ListingEngine + mock_spapi）承担当前一切提交；
本模块是**真实 SP-API Listings 写通道的状态机骨架**，与 Mock 引擎完全
隔离（互不 import、互不共享状态）。设计文档：
wz_test\\s3b2f\\LIVE_GATEWAY_DESIGN.md。

禁用的确切含义（fail-closed）
------------------------------
1. `LiveListingGateway.for_production()` **恒 raise ListingGatewayDisabled**
   ——没有任何 env 变量、config 键、特性开关能改变这一行。启用=未来
   独立提交显式改码并重走验收。
2. main.py / pt_api.py 零改动：不存在任何路由/装配点能构造本网关。
3. 真实 HTTP 传输层**不存在**：`NullTransport.put/get` 直接 raise
   NotImplementedError——不是「留开关」，是「没有这层」。唯一可用构造
   入口=离线测试直传 fake transport（进程内，零网络）。

核心安全语义（与 Mock 的本质差异）
----------------------------------
Mock 同幂等令牌重放=同一 submissionId（mock_spapi）；**真实 Listings
Items API 每次 PUT=新 submissionId，不承诺去重**。因此：
- 外部调用前先落「提交意图」（store.pt_submit_intent，写前日志）；
- 意图无受理号（open/dispatched）时——无论崩溃窗口 W-A（未发出）还是
  W-B（已发出未落库）——恢复一律 **result_unknown**：绝不自动重发、
  绝不自动撤销，只允许人工两条显式出路（confirm_receipt / cancel_
  intent，全审计）；
- 受理落库（receipted）之后才进入查询轮询；轮询预算尽 → unknown。
"""

from __future__ import annotations

import hashlib
from typing import Callable, Protocol

from . import store as pt_store

# 平台状态词表与 P1-3 契约一致（条目级；partially_accepted 只可能出现在
# 一次提交覆盖多 SKU 的回执汇总级——本网关逐 SKU 提交，条目级不出现）。
PLATFORM_STATES = ("not_submitted", "submitting", "acknowledged",
                   "processing", "accepted", "rejected", "unknown")


class ListingGatewayDisabled(RuntimeError):
    """真实 Listings 网关处于禁用态（无任何启用路径）。"""


class ListingTransportError(Exception):
    """传输层故障（超时/断线/5xx/429）——请求可能已被平台收到。"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind          # timeout|network|server|ratelimit


class ListingRejectError(Exception):
    """平台确定性拒绝（回执明确 rejected，附 issues）。"""

    def __init__(self, issues: list[dict]):
        super().__init__(f"rejected: {issues}")
        self.issues = issues


class ListingTransport(Protocol):
    """真实 SP-API Listings 传输接口（唯一实现方=离线测试的 fake）。"""

    def put(self, seller_id: str, marketplace_id: str, sku: str,
            payload: dict, idem_token: str) -> dict:
        """PUT /listings/…/items/{seller}/{sku} → {"submissionId": str}。
        网关不假定任何幂等语义——同 token 重放可能返回新受理号。"""
        ...

    def get(self, seller_id: str, marketplace_id: str, sku: str) -> dict:
        """GET 单 SKU listing 状态 → {"status": "PROCESSING|ACCEPTED|
        REJECTED|NOT_FOUND", "issues": […]}。"""
        ...


class NullTransport:
    """真实 HTTP 传输不存在（启用前占位；调用即错，fail-closed）。"""

    def put(self, *a, **k) -> dict:  # pragma: no cover - 恒错
        raise NotImplementedError(
            "真实 Listings 传输层未实现（网关禁用态；启用须独立验收）")

    def get(self, *a, **k) -> dict:  # pragma: no cover - 恒错
        raise NotImplementedError(
            "真实 Listings 传输层未实现（网关禁用态；启用须独立验收）")


def _idem_token(task_id: str, pid: str, sku: str, payload_sha: str) -> str:
    return hashlib.sha256(
        f"{task_id}|{pid}|{sku}|{payload_sha}".encode("utf-8")
    ).hexdigest()[:32]


class LiveListingGateway:
    """真实 Listings 网关状态机（逐 SKU；店铺三元组绑定）。

    构造即校验维度隔离：(store_id, seller_id, marketplace_id) 贯穿一切
    意图与调用；调用方（未来真实接线层）负责店铺归属闸（pt_api 模式）。
    本类不含任何 HTTP——transport 由构造方注入（当前只有测试注入 fake）。
    """

    def __init__(self, store: pt_store.PtStore, transport: ListingTransport,
                 *, store_id: str, seller_id: str, marketplace_id: str,
                 env: str = "live"):
        if str(env or "").strip().lower() != "live":
            raise ValueError("LiveListingGateway 只服务 env=live")
        self._store = store
        self._tp = transport
        self.store_id = str(store_id)
        self.seller_id = str(seller_id)
        self.marketplace_id = str(marketplace_id)
        store.ensure_submit_intent_schema()

    # ---- 禁用闸（唯一「生产构造」入口，恒拒） ----

    @classmethod
    def for_production(cls, *a, **k) -> "LiveListingGateway":
        raise ListingGatewayDisabled(
            "真实 Listings 网关未启用（无 env/config/开关路径）；"
            "启用=独立提交显式改码+重走安全验收")

    # ---- 提交主流程（单条目） ----

    def submit_item(self, task_id: str, pid: str, sku: str,
                    payload: dict) -> dict:
        """逐 SKU 提交。返回平台契约视图（platform_status/…）。

        规则（崩溃窗口核心）：
        - 已有未决意图（open/dispatched 无受理）→ 直接 unknown 返回，
          **不发出任何调用**（put 调用计数不增）——W-A/W-B 不可区分；
        - 已 receipted → 不重发，转 poll；
        - 新意图：落库 → 打点 → transport.put → 受理落库；
          put 抛 ListingTransportError → 意图留在 dispatched（请求可能
          已到平台）→ unknown；ListingRejectError 在 put 回执内处理。
        """
        payload_sha = hashlib.sha256(
            repr(sorted(payload.items())).encode("utf-8")).hexdigest()[:24]
        it = self._store.create_submit_intent(
            task_id=task_id, pid=pid, sku=sku,
            store_id=self.store_id,
            marketplace_id=self.marketplace_id, env="live",
            idem_token=_idem_token(task_id, pid, sku, payload_sha),
            payload_sha=payload_sha)
        if not it.get("_created"):
            if it["state"] == "receipted":
                return self._view("acknowledged", it,
                                  note="已有受理号，转结果核对")
            if it["state"] == "cancelled":
                return self._view("not_submitted", it,
                                  note="意图已被人工撤销（审计在案）")
            return self._view(
                "unknown", it,
                note="存在未决提交意图（请求可能已达平台）——绝不自动"
                     "重发；人工核对：confirm_receipt 或 cancel_intent")
        try:
            self._store.mark_intent_dispatched(task_id, pid, sku)
            res = self._tp.put(self.seller_id, self.marketplace_id, sku,
                               payload, it["idem_token"])
            sid = str((res or {}).get("submissionId") or "")
            if not sid:
                raise ListingTransportError("bad_receipt",
                                            "回执无 submissionId")
            ok = self._store.set_intent_receipt(task_id, pid, sku, sid)
            if not ok:
                return self._view("unknown", it, note="受理落库冲突")
            it = self._store.get_submit_intent(task_id, pid, sku) or it
            return self._view("acknowledged", it)
        except ListingRejectError as e:
            # 平台确定性拒绝（put 400 类：无受理号、请求未进处理）——
            # 意图收敛为 rejected（不进未决集，恢复不当 unknown 处理），
            # 异常继续上抛给调用方落条目终态+issues
            self._store.mark_intent_platform_rejected(
                task_id, pid, sku, note=f"platform rejected: {e.issues}")
            raise
        except ListingTransportError as e:
            # 请求可能已到平台：意图留 dispatched → unknown（不重发）
            it = self._store.get_submit_intent(task_id, pid, sku) or it
            return self._view("unknown", it,
                              note=f"提交在途失败（{e.kind}）——请求可能"
                                   "已达平台，绝不自动重发，人工核对")

    # ---- 结果查询（预算轮询由调用方逐 tick 驱动；单次查询在此） ----

    def query_item(self, task_id: str, pid: str, sku: str) -> dict:
        """单次结果查询（receipted 之后才允许；否则 not_submitted/unknown）。"""
        it = self._store.get_submit_intent(task_id, pid, sku)
        if it is None:
            return self._view("not_submitted", None, note="无提交意图")
        if it["state"] != "receipted" or not it["submission_id"]:
            return self._view("unknown", it, note="无受理号（未决意图）")
        try:
            res = self._tp.get(self.seller_id, self.marketplace_id, sku)
        except ListingTransportError as e:
            return self._view("unknown", it,
                              note=f"查询失败（{e.kind}）——稍后重查，"
                                   "不视为失败")
        status = str((res or {}).get("status") or "").upper()
        issues = list((res or {}).get("issues") or [])
        if status == "ACCEPTED":
            return self._view("accepted", it)
        if status == "REJECTED":
            raise ListingRejectError(issues)  # 调用方落 rejected+issues
        if status == "NOT_FOUND":
            return self._view("unknown", it,
                              note="平台尚无此 listing——受理在途或被弃，"
                                   "人工核对")
        return self._view("processing", it)   # PROCESSING/未知→继续查

    # ---- 人工显式出路（唯一能改变未决意图的路径；全审计） ----

    def confirm_receipt(self, task_id: str, pid: str, sku: str,
                        submission_id: str, *, acted_by: str,
                        note: str = "") -> dict:
        return {"ok": True,
                "intent": self._store.confirm_intent_receipt(
                    task_id, pid, sku, submission_id,
                    acted_by=acted_by, note=note)}

    def cancel_intent(self, task_id: str, pid: str, sku: str, *,
                      acted_by: str, note: str = "") -> dict:
        return {"ok": True,
                "intent": self._store.cancel_submit_intent(
                    task_id, pid, sku, acted_by=acted_by, note=note)}

    def open_intents(self, task_id: str) -> list[dict]:
        """恢复入口：未决意图清单（调用方据此把条目置 unknown+人工面板）。"""
        return self._store.list_open_intents(task_id)

    # ---- 视图 ----

    def _view(self, platform_status: str, it: dict | None,
              note: str = "") -> dict:
        v = {"platform_status": platform_status,
             "source": "live_gateway", "mock": False,
             "store_id": self.store_id,
             "marketplace_id": self.marketplace_id,
             "disabled_state": True, "note": note}
        if it is not None:
            v.update({"request_id": str(it.get("idem_token") or ""),
                      "response_id": str(it.get("submission_id") or ""),
                      "intent_state": str(it.get("state") or "")})
        return v


__all__ = ["PLATFORM_STATES", "ListingGatewayDisabled",
           "ListingTransportError", "ListingRejectError",
           "ListingTransport", "NullTransport", "LiveListingGateway"]

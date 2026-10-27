# -*- coding: utf-8 -*-
"""pt.listings_items —— S3-C 第四阶段：Listings Items API 独立适配器
（**禁用态**：无真实 HTTP 实现，仅离线契约层）。

与 listing_gateway 的关系
------------------------
gateway（状态机：intent 写前日志/崩溃窗口 unknown/人工出路）面向
`ListingTransport` 协议编程；本模块把 **Listings Items API 的 HTTP 语义**
翻译成该协议——将来真实传输=把 `http` 回调换成真实现（独立提交+验收），
状态机零改动。当前唯一 http 实现=离线测试注入的 fake（零网络）。

Listings Items vs Feeds（语义差异，实现只取前者）
------------------------------------------------
| 维度          | Listings Items（本适配器）             | Feeds（不实现）          |
|--------------|---------------------------------------|--------------------------|
| 粒度          | 逐 SKU PUT items/{seller}/{sku}       | 批量文档一次多 SKU       |
| 受理回执      | 同步 submissionId（≠刊登成功）        | feedId + 处理报告        |
| 结果          | GET items/{seller}/{sku} 看 issues    | 轮询 feed + 解析报告     |
| 部分成功      | 天然逐 SKU，无汇总态                  | 报告级（partially_*）    |
| 重放          | 每次 PUT=新 submissionId（无去重承诺） | 不重发文档即无重复       |

**铁律（与 LIVE_GATEWAY_DESIGN.md §1 一致）**：
1. HTTP 200/202 只代表「平台受理进入处理」（=acknowledged/processing），
   **绝不直接视为刊登成功**——最终态只能来自 GET 的 ACCEPTED。
2. 回执无 submissionId=坏回执（bad_receipt）→ 网关判 unknown（不猜）。
3. 不假定任何服务端幂等：同 payload 重复 PUT 就是一次新提交。
4. 本模块无任何「启用真实写入」路径：不读 env/config；真实 http 回调
   不存在（NullHttp 调用即错）；不绕过 gateway.for_production()。
"""

from __future__ import annotations

import json
from typing import Callable

from .listing_gateway import ListingRejectError, ListingTransportError

# 官方端点模板（契约常量；真实接线前仅用于报告/校验）
PUT_PATH = "/listings/2021-08-01/items/{seller_id}/{sku}"
GET_PATH = "/listings/2021-08-01/items/{seller_id}/{sku}"

# GET 状态 → 网关 platform 契约（对齐 P1-3 词表；未识别=processing 续查）
GET_STATUS_MAP = {
    "PROCESSING": "processing",
    "ACCEPTED": "accepted",
    "REJECTED": "rejected",
    "NOT_FOUND": "not_found",
}


class NullHttp:
    """真实 HTTP 传输不存在（占位恒错——「没有这层」，不是开关）。"""

    def __call__(self, *a, **k):
        raise NotImplementedError(
            "Listings Items 真实 HTTP 传输未实现（网关禁用态；启用须"
            "独立提交显式改码+重走安全验收）")


def _issues_of(body: dict | None) -> list[dict]:
    """官方回执 issues 数组透传（多条保留；非列表/缺省=空）。"""
    v = (body or {}).get("issues")
    return list(v) if isinstance(v, list) else []


class ListingsItemsAdapter:
    """实现 ListingTransport 协议（put/get）的 Listings Items 适配器。

    http: (method, path, headers, body_bytes|None, timeout) ->
          (status:int, body_bytes:bytes)。唯一实现方=离线测试 fake；
    生产构造不存在（NullHttp 恒错；无 env/config/UI 路径构造真实现）。
    """

    def __init__(self, http: Callable):
        self._http = http

    # ---- PUT：逐 SKU 提交，同步受理 ----

    def put(self, seller_id: str, marketplace_id: str, sku: str,
            payload: dict, idem_token: str) -> dict:
        path = PUT_PATH.format(seller_id=seller_id, sku=sku)
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        # idem 令牌进 header（与现有 Mock 通道同位；服务端是否理会=平台
        # 行为，本适配器不承诺任何去重语义——铁律 3）
        headers = {"x-amzn-idempotency-token": str(idem_token or "")}
        try:
            status, raw = self._http("PUT", path, headers, body, 30.0)
        except Exception as e:  # noqa: BLE001 —— 网络/超时归一（在途失败）
            raise ListingTransportError("network", str(e)[:120]) from e
        body_obj = self._parse(status, raw)
        if status in (200, 202):
            # 受理回执：submissionId 存在=已进处理（≠成功，铁律 1）
            sid = str((body_obj or {}).get("submissionId") or "")
            if not sid:
                # 平台收了请求但不给受理号=无法核对结果（不猜，交网关判
                # unknown 人工核对）
                raise ListingTransportError(
                    "bad_receipt", f"HTTP {status} 回执无 submissionId")
            return {"submissionId": sid}
        if status == 400:
            # 确定性拒绝（载荷问题）：issues 全量透传给网关落 rejected
            raise ListingRejectError(_issues_of(body_obj) or [
                {"code": "HTTP_400", "message": str(body_obj)[:160]}])
        raise ListingTransportError(
            *self._transport_kind(status, body_obj))

    # ---- GET：单 SKU 结果查询 ----

    def get(self, seller_id: str, marketplace_id: str, sku: str) -> dict:
        path = GET_PATH.format(seller_id=seller_id, sku=sku)
        try:
            status, raw = self._http("GET", path, {}, None, 30.0)
        except Exception as e:  # noqa: BLE001
            raise ListingTransportError("network", str(e)[:120]) from e
        body_obj = self._parse(status, raw)
        if status == 200:
            raw_st = str((body_obj or {}).get("status") or "").upper()
            mapped = GET_STATUS_MAP.get(raw_st, "processing")
            if mapped == "rejected":
                # 拒绝=确定性终态：issues 多条全量透传（网关抛
                # ListingRejectError 给调用方落库）
                raise ListingRejectError(_issues_of(body_obj))
            return {"status": mapped, "issues": _issues_of(body_obj)}
        if status == 404:
            return {"status": "not_found", "issues": []}
        raise ListingTransportError(*self._transport_kind(status, body_obj))

    # ---- 内部 ----

    def _parse(self, status: int, raw: bytes) -> dict | None:
        if not raw:
            return None
        try:
            v = json.loads(raw.decode("utf-8", "replace"))
            return v if isinstance(v, dict) else None
        except (UnicodeDecodeError, ValueError):
            # 状态可判但形状不可读：返回 None 让调用方按状态走
            return None

    @staticmethod
    def _transport_kind(status: int, body: dict | None) -> tuple[str, str]:
        """非受理/非终态 HTTP → 传输错误分类（请求可能已达平台）。"""
        if status == 401:
            return "auth", f"HTTP 401：{_truncate(body)}"
        if status == 403:
            return "forbidden", f"HTTP 403：{_truncate(body)}"
        if status == 429:
            return "ratelimit", f"HTTP 429：{_truncate(body)}"
        if status >= 500:
            return "server", f"HTTP {status}：{_truncate(body)}"
        return "http", f"HTTP {status}：{_truncate(body)}"


def _truncate(body: dict | None) -> str:
    try:
        return json.dumps(body, ensure_ascii=False)[:120]
    except Exception:  # noqa: BLE001
        return ""


class FeedsAdapter:
    """Feeds API 适配器——**刻意不实现**（禁用态占位）。

    语义差异见模块 docstring：批量文档/异步报告/汇总级部分成功，与
    网关「逐 SKU+同步受理+条目级状态」模型不同构。将来若引入=独立
    设计（报告解析层/部分成功映射）+独立验收，不是打开本类的开关。
    """

    def __init__(self, *a, **k):
        raise NotImplementedError(
            "Feeds 适配器刻意未实现（语义不同构；引入须独立设计+验收）")


__all__ = ["PUT_PATH", "GET_PATH", "GET_STATUS_MAP", "NullHttp",
           "ListingsItemsAdapter", "FeedsAdapter",
           "ListingTransportError", "ListingRejectError"]

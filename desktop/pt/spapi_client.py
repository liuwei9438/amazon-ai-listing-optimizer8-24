# -*- coding: utf-8 -*-
"""pt.spapi_client —— 第一阶段模块 2：SP-API 客户端（注入式适配层）。

覆盖四个官方操作（契约逐字抄自官方 OpenAPI，2026-10-10 从
developer-docs.amazon 抓取核对，规范镜像存 spec_ptd_*.json/spec_cat_*.json）：

  操作                          方法  路径                                     用量计划
  searchDefinitionsProductTypes GET   /definitions/2020-09-01/productTypes      5 rps / burst 10
  getDefinitionsProductType     GET   /definitions/2020-09-01/productTypes/{pt} 5 rps / burst 10
  searchCatalogItems            GET   /catalog/2022-04-01/items                 2 rps / burst 2
  getCatalogItem                GET   /catalog/2022-04-01/items/{asin}          2 rps / burst 2

工程约束（模块 2 批复 12 条）在代码中的落点：
  3  不 import main：transport / token_provider / endpoint_for 全部构造注入，
     main 在装配期把自己的 SP-API 请求能力（代理、令牌、区域表）注入进来。
  4  错误分类：SpApiAuthError(401) / SpApiForbiddenError(403) /
     SpApiBadRequestError(400 及 413/415 等其他 4xx) / SpApiNotFoundError(404) /
     SpApiRateLimitError(429) / SpApiServerError(5xx) / SpApiNetworkError(网络
     超时/连接失败) / SpApiParseError(响应不是合法官方形状)。
  5  有界重试：仅 429 / 5xx / 网络错误重试（本客户端四个操作全是 GET，天然
     幂等；重试引擎按 method==GET 守卫，非幂等操作永不自动重试）。指数退避
     base=0.5s 翻倍，上限 max_delay；429 优先遵守 Retry-After（秒），超过
     retry_after_cap 直接放弃返回限流错误，绝不无限等。
  6  限流：每操作独立 RateLimiter（官方用量计划做缺省），可构造覆盖；每次
     响应读 x-amzn-RateLimit-Limit 实时修正速率。
  7  区域正确：marketplace_id → 区域端点（18 站点表与 main._MARKETPLACES
     逐条一致），未知站点直接 ValueError，绝不猜区域。
  9  Mock 结果显式标记：响应头 X-WZ-Mock: 1 → SpApiResult.mock=True。
     官方真响应没有这个头 → mock=False。上层据此决定 verified_against。
  10 认证秘密绝不入日志/异常文本：令牌只在发请求瞬间进 header；异常只带
     状态码、官方 ErrorList、request id。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# 官方契约常量（逐字抄自官方 OpenAPI，禁止手改）
# ---------------------------------------------------------------------------

PTD_BASE = "/definitions/2020-09-01"
CATALOG_BASE = "/catalog/2022-04-01"

# searchDefinitionsProductTypes.query.keywords 与 itemName 官方互斥
# （描述原文："Cannot be used with ..."）
# searchCatalogItems.query.identifiers 与 keywords 官方互斥；
# identifiersType "is required when identifiers is in the request"；
# classificationIds "Cannot be used with identifiers"；
# identifiers/keywords 官方 maxItems=20；marketplaceIds maxItems=1。

IDENTIFIERS_TYPES = frozenset({
    "ASIN", "EAN", "GTIN", "ISBN", "JAN", "MINSAN", "SKU", "UPC",
})
INCLUDED_DATA = frozenset({
    "attributes", "classifications", "dimensions", "identifiers", "images",
    "productTypes", "relationships", "salesRanks", "summaries", "vendorDetails",
})
REQUIREMENTS = frozenset({
    "LISTING", "LISTING_PRODUCT_ONLY", "LISTING_OFFER_ONLY",
})
REQUIREMENTS_ENFORCED = frozenset({"ENFORCED", "NOT_ENFORCED"})
PARENTAGE_LEVEL = frozenset({"NONE", "CHILD", "PARENT"})
# getDefinitionsProductType.locale 官方枚举（39 值，逐字抄）
PTD_LOCALES = frozenset({
    "DEFAULT", "ar", "ar_AE", "de", "de_DE", "en", "en_AE", "en_AU", "en_CA",
    "en_GB", "en_IN", "en_SG", "en_US", "es", "es_ES", "es_MX", "es_US", "fr",
    "fr_CA", "fr_FR", "it", "it_IT", "ja", "ja_JP", "nl", "nl_NL", "pl",
    "pl_PL", "pt", "pt_BR", "pt_PT", "sv", "sv_SE", "tr", "tr_TR", "zh",
    "zh_CN", "zh_TW",
})

# 18 站点 marketplace_id → 区域（与 main._MARKETPLACES 逐条一致）
REGION_BY_MARKETPLACE = {
    "ATVPDKIKX0DER": "na", "A2EUQ1WTGCTBG2": "na",
    "A1AM78C64UM0Y8": "na", "A2Q3Y263D00KWC": "na",
    "A1F83G8C2ARO7P": "eu", "A1PA6795UKMFR9": "eu",
    "A13V1IB3VIYZZH": "eu", "APJ6JRA9NG5V4": "eu",
    "A1RKKUPIHCS9HS": "eu", "A2NODRKZP88ZB9": "eu",
    "A1805IZSGTT6HS": "eu", "A1C3SOZRARQ6R3": "eu",
    "A2VIGQ35RCS4UG": "eu", "A17E79C6D8DWNP": "eu",
    "A21TJRUUN4KGV": "eu",
    "A1VC38T7YXB528": "fe", "A39IBJ37TRP1C6": "fe",
    "A19VAU5U5O7RUS": "fe",
}
REGION_HOSTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}

# 官方用量计划缺省值（页内 Usage Plan 表逐字抄）
DEFAULT_RATE_PLANS = {
    "search_product_types": (5.0, 10.0),
    "get_product_type": (5.0, 10.0),
    "search_catalog_items": (2.0, 2.0),
    "get_catalog_item": (2.0, 2.0),
}


def official_endpoint(marketplace_id: str) -> str:
    """marketplace → 区域端点；未知站点 ValueError（绝不猜区域）。"""
    region = REGION_BY_MARKETPLACE.get(marketplace_id)
    if region is None:
        raise ValueError(
            f"未知 marketplace_id：{marketplace_id!r}（已知 "
            f"{len(REGION_BY_MARKETPLACE)} 个官方站点）"
        )
    return REGION_HOSTS[region]


# ---------------------------------------------------------------------------
# 错误分类（要求 4）
# ---------------------------------------------------------------------------


class SpApiError(Exception):
    """SP-API 调用失败基类。携带 op / marketplace_id / HTTP 状态 / 官方
    ErrorList（{code,message,details?} 列表）/ x-amzn-RequestId。
    绝不携带请求头或令牌（要求 10）。"""

    def __init__(self, message: str, *, op: str = "", marketplace_id: str = "",
                 status: int | None = None,
                 errors: list[dict] | None = None,
                 request_id: str | None = None):
        self.op = op
        self.marketplace_id = marketplace_id
        self.status = status
        self.errors = errors or []
        self.request_id = request_id
        super().__init__(message)

    def official_messages(self) -> list[str]:
        out = []
        for e in self.errors:
            m = f"[{e.get('code','')}] {e.get('message','')}".strip()
            if e.get("details"):
                m += f" ({e['details']})"
            out.append(m)
        return out


class SpApiAuthError(SpApiError):
    """401 认证失败 / 令牌获取失败。"""


class SpApiForbiddenError(SpApiError):
    """403 权限不足。"""


class SpApiBadRequestError(SpApiError):
    """400 参数错误（含 413/415 等其他 4xx）。"""


class SpApiNotFoundError(SpApiError):
    """404 资源不存在。"""


class SpApiRateLimitError(SpApiError):
    """429 限流（重试耗尽或 Retry-After 超帽后抛出）。"""

    def __init__(self, *a, retry_after: float | None = None, **kw):
        self.retry_after = retry_after
        super().__init__(*a, **kw)


class SpApiServerError(SpApiError):
    """5xx 服务端错误（重试耗尽后抛出）。"""


class SpApiNetworkError(SpApiError):
    """连接失败 / 超时（重试耗尽后抛出）。"""

    def __init__(self, message, *, phase: str = "connect", **kw):
        # phase: "timeout"（发出后超时）/ "connect"（根本没连上）
        self.phase = phase
        super().__init__(message, **kw)


class SpApiParseError(SpApiError):
    """200 但响应体不是官方合法形状（非 JSON / 缺必填字段）。"""


# ---------------------------------------------------------------------------
# 结果对象（要求 9：mock 显式标记）
# ---------------------------------------------------------------------------


@dataclass
class SpApiResult:
    op: str                      # search_product_types / ...
    marketplace_id: str
    status: int
    data: Any                    # 官方响应原样（dict）
    mock: bool = False           # 响应头 X-WZ-Mock: 1 → True（Mock 铁律）
    request_id: str | None = None
    rate_limit: float | None = None   # 本次响应 x-amzn-RateLimit-Limit
    attempts: int = 1            # 含重试的总尝试次数
    elapsed: float = 0.0         # 秒


# ---------------------------------------------------------------------------
# 限流（要求 6）
# ---------------------------------------------------------------------------


class RateLimiter:
    """令牌桶（速率 rps + 桶容量 burst）。acquire() 阻塞到有令牌；
    等待封顶 max_wait 秒（防外部时钟/参数异常导致死等）。"""

    def __init__(self, rate: float, burst: float, max_wait: float = 20.0):
        self.rate = max(float(rate), 0.01)
        self.burst = max(float(burst), 1.0)
        self.max_wait = float(max_wait)
        self._tokens = self.burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst,
                           self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self) -> float:
        """返回实际等待的秒数。"""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            need = 1.0 - self._tokens
            wait = min(need / self.rate, self.max_wait)
            time.sleep(wait)
            self._refill()
            self._tokens = max(self._tokens - 1.0, 0.0)
            return wait

    def update_rate(self, rate: float) -> None:
        """用响应头 x-amzn-RateLimit-Limit 实测速率修正（burst 不变）。"""
        with self._lock:
            try:
                r = float(rate)
            except (TypeError, ValueError):
                return
            if r > 0:
                self.rate = r


# ---------------------------------------------------------------------------
# 传输层（要求 3：注入）
# ---------------------------------------------------------------------------

Transport = Callable[[str, str, dict, float], tuple[int, dict, bytes]]
# transport(method, url, headers, timeout) -> (status, headers小写dict, body字节)


def stdlib_transport(method: str, url: str, headers: dict,
                     timeout: float) -> tuple[int, dict, bytes]:
    """缺省传输：urllib，显式禁用环境代理（本模块只对接本地 Mock 与注入
    的定制传输；真实出网代理由 main 注入的 transport 负责——防 NO_PROXY
    继承事故重演）。HTTPError 统一拆成三元组返回，不在这里抛。"""
    req = urllib.request.Request(url, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), _lc(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            pass
        return int(e.code), _lc(e.headers), body


def _lc(h) -> dict:
    return {str(k).lower(): v for k, v in (h.items() if hasattr(h, "items")
                                           else h)}


# ---------------------------------------------------------------------------
# 重试配置（要求 5）
# ---------------------------------------------------------------------------


@dataclass
class RetryConfig:
    max_attempts: int = 3          # 总尝试次数上限（含首次）
    base_delay: float = 0.5        # 指数退避基数（秒）
    max_delay: float = 8.0         # 单次退避上限（秒）
    retry_after_cap: float = 30.0  # Retry-After 超过它=直接放弃（防无限等）


@dataclass
class _OpSpec:
    name: str
    method: str
    path: str
    official: str                  # 官方操作名（诊断用）


OPS = {
    "search_product_types": _OpSpec(
        "search_product_types", "GET", f"{PTD_BASE}/productTypes",
        "searchDefinitionsProductTypes"),
    "get_product_type": _OpSpec(
        "get_product_type", "GET", f"{PTD_BASE}/productTypes/{{productType}}",
        "getDefinitionsProductType"),
    "search_catalog_items": _OpSpec(
        "search_catalog_items", "GET", f"{CATALOG_BASE}/items",
        "searchCatalogItems"),
    "get_catalog_item": _OpSpec(
        "get_catalog_item", "GET", f"{CATALOG_BASE}/items/{{asin}}",
        "getCatalogItem"),
}


def _csv(values: Iterable) -> str:
    return ",".join(str(v) for v in values)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class SpApiClient:
    """四个官方操作的客户端。一切外部能力（发请求、取令牌、选端点）全部
    注入；默认传输 urllib 直连不走环境代理，默认取令牌=报错提醒注入。"""

    def __init__(self, *, transport: Transport | None = None,
                 token_provider: Callable[[], str] | None = None,
                 endpoint_for: Callable[[str], str] | None = None,
                 rate_overrides: dict[str, tuple[float, float]] | None = None,
                 retry: RetryConfig | None = None,
                 timeout: float = 20.0,
                 user_agent: str = "wz-desktop-pt/1.0"):
        self._transport = transport or stdlib_transport
        self._token_provider = token_provider
        self._endpoint_for = endpoint_for or official_endpoint
        self._retry = retry or RetryConfig()
        self._timeout = float(timeout)
        self._user_agent = user_agent
        self._limiters: dict[str, RateLimiter] = {}
        for op, (r, b) in DEFAULT_RATE_PLANS.items():
            if rate_overrides and op in rate_overrides:
                r, b = rate_overrides[op]
            self._limiters[op] = RateLimiter(r, b)

    # ---- 对外诊断 ----

    def rate_status(self) -> dict[str, dict]:
        return {op: {"rate": lim.rate, "burst": lim.burst}
                for op, lim in self._limiters.items()}

    def op_methods(self) -> dict[str, str]:
        """四个操作全是官方 GET（幂等）——非 GET 在重试引擎里被硬拒。"""
        return {name: spec.method for name, spec in OPS.items()}

    # ---- 四个官方操作 ----

    def search_product_types(self, marketplace_id: str, *,
                             keywords: list[str] | None = None,
                             item_name: str | None = None,
                             locale: str | None = None,
                             search_locale: str | None = None) -> SpApiResult:
        """官方：keywords 与 itemName 互斥，必须给其一。"""
        keywords = [k for k in (keywords or []) if k]
        item_name = (item_name or "").strip()
        if keywords and item_name:
            raise ValueError("keywords 与 itemName 官方互斥，只能给其一")
        if not keywords and not item_name:
            raise ValueError("必须给 keywords 或 itemName 其一")
        params: dict[str, str] = {"marketplaceIds": marketplace_id}
        if keywords:
            params["keywords"] = _csv(keywords)
        if item_name:
            params["itemName"] = item_name
        if locale:
            params["locale"] = locale
        if search_locale:
            params["searchLocale"] = search_locale
        return self._call_checked(
            "search_product_types", marketplace_id, params,
            required=("productTypes", "productTypeVersion"))

    def get_product_type(self, product_type: str, marketplace_id: str, *,
                         seller_id: str | None = None,
                         product_type_version: str | None = None,
                         requirements: str | None = None,
                         requirements_enforced: str | None = None,
                         locale: str | None = None,
                         parentage_level: str | None = None) -> SpApiResult:
        pt = (product_type or "").strip()
        if not pt:
            raise ValueError("productType 不能为空")
        for name, val, allowed in (
            ("requirements", requirements, REQUIREMENTS),
            ("requirementsEnforced", requirements_enforced,
             REQUIREMENTS_ENFORCED),
            ("parentageLevel", parentage_level, PARENTAGE_LEVEL),
            ("locale", locale, PTD_LOCALES),
        ):
            if val is not None and val not in allowed:
                raise ValueError(f"非法 {name}：{val!r}（官方枚举见 "
                                 "spapi_client 常量）")
        params = {"marketplaceIds": marketplace_id}
        if seller_id:
            params["sellerId"] = seller_id
        if product_type_version:
            params["productTypeVersion"] = product_type_version
        if requirements:
            params["requirements"] = requirements
        if requirements_enforced:
            params["requirementsEnforced"] = requirements_enforced
        if locale:
            params["locale"] = locale
        if parentage_level:
            params["parentageLevel"] = parentage_level
        return self._call_checked(
            "get_product_type", marketplace_id, params,
            path_args={"productType": pt},
            required=("schema", "requirements", "requirementsEnforced",
                      "propertyGroups", "locale", "marketplaceIds",
                      "productType", "displayName", "productTypeVersion"))

    def search_catalog_items(self, marketplace_id: str, *,
                             identifiers: list[str] | None = None,
                             identifiers_type: str | None = None,
                             keywords: list[str] | None = None,
                             included_data: list[str] | None = None,
                             locale: str | None = None,
                             seller_id: str | None = None,
                             brand_names: list[str] | None = None,
                             classification_ids: list[str] | None = None,
                             page_size: int | None = None,
                             page_token: str | None = None,
                             keywords_locale: str | None = None) -> SpApiResult:
        identifiers = [i for i in (identifiers or []) if i]
        keywords = [k for k in (keywords or []) if k]
        included_data = [d for d in (included_data or []) if d]
        brand_names = [b for b in (brand_names or []) if b]
        classification_ids = [c for c in (classification_ids or []) if c]
        if identifiers and keywords:
            raise ValueError("identifiers 与 keywords 官方互斥，只能给其一")
        if not identifiers and not keywords:
            raise ValueError("必须给 identifiers 或 keywords 其一")
        if len(identifiers) > 20:
            raise ValueError(f"identifiers 官方上限 20 个（给了 "
                             f"{len(identifiers)}）")
        if len(keywords) > 20:
            raise ValueError(f"keywords 官方上限 20 个（给了 "
                             f"{len(keywords)}）")
        if identifiers_type is not None and identifiers_type \
                not in IDENTIFIERS_TYPES:
            raise ValueError(f"非法 identifiersType：{identifiers_type!r}")
        if identifiers and not identifiers_type:
            raise ValueError("官方规定：给了 identifiers 就必须给 "
                             "identifiersType")
        if identifiers_type and not identifiers:
            raise ValueError("identifiersType 只能配合 identifiers 使用")
        if classification_ids and identifiers:
            raise ValueError("classificationIds 官方规定不能与 identifiers "
                             "同用（只配合 keywords）")
        bad = [d for d in included_data if d not in INCLUDED_DATA]
        if bad:
            raise ValueError(f"非法 includedData：{bad}（官方枚举 10 种）")
        if page_size is not None and (not isinstance(page_size, int)
                                      or page_size < 1):
            raise ValueError(f"非法 pageSize：{page_size!r}（正整数）")
        params = {"marketplaceIds": marketplace_id}   # 官方 maxItems=1
        if identifiers:
            params["identifiers"] = _csv(identifiers)
            params["identifiersType"] = identifiers_type
        if keywords:
            params["keywords"] = _csv(keywords)
        if included_data:
            params["includedData"] = _csv(included_data)
        if locale:
            params["locale"] = locale
        if seller_id:
            params["sellerId"] = seller_id
        if brand_names:
            params["brandNames"] = _csv(brand_names)
        if classification_ids:
            params["classificationIds"] = _csv(classification_ids)
        if page_size is not None:
            params["pageSize"] = str(page_size)
        if page_token:
            params["pageToken"] = page_token
        if keywords_locale:
            params["keywordsLocale"] = keywords_locale
        return self._call_checked(
            "search_catalog_items", marketplace_id, params,
            required=("numberOfResults",))

    def get_catalog_item(self, asin: str, marketplace_id: str, *,
                         included_data: list[str] | None = None,
                         locale: str | None = None) -> SpApiResult:
        a = (asin or "").strip()
        if not a:
            raise ValueError("asin 不能为空")
        included_data = [d for d in (included_data or []) if d]
        bad = [d for d in included_data if d not in INCLUDED_DATA]
        if bad:
            raise ValueError(f"非法 includedData：{bad}（官方枚举 10 种）")
        params = {"marketplaceIds": marketplace_id}
        if included_data:
            params["includedData"] = _csv(included_data)
        if locale:
            params["locale"] = locale
        return self._call_checked(
            "get_catalog_item", marketplace_id, params,
            path_args={"asin": a}, required=("asin",))

    # ---- 引擎 ----

    def _call_checked(self, op: str, marketplace_id: str, params: dict,
                      required: tuple[str, ...] = (),
                      path_args: dict | None = None) -> SpApiResult:
        spec = OPS[op]
        path = spec.path
        for k, v in (path_args or {}).items():
            path = path.replace("{" + k + "}", urllib.parse.quote(
                str(v), safe=""))
        result = self._call(op, spec, marketplace_id, path, params)
        # 200 响应形状核验（官方 OpenAPI required 字段，缺了=不是官方形状）
        data = result.data
        if not isinstance(data, dict):
            raise SpApiParseError(
                f"{spec.official} 响应不是 JSON 对象：{type(data).__name__}",
                op=op, marketplace_id=marketplace_id, status=result.status,
                request_id=result.request_id)
        missing = [k for k in required if k not in data]
        if missing:
            raise SpApiParseError(
                f"{spec.official} 响应缺官方必填字段：{missing}",
                op=op, marketplace_id=marketplace_id, status=result.status,
                request_id=result.request_id)
        if op == "search_product_types":
            if not isinstance(data.get("productTypes"), list) or \
                    not isinstance(data.get("productTypeVersion"), str):
                raise SpApiParseError(
                    "searchDefinitionsProductTypes 响应字段类型不对"
                    f"：productTypes={type(data.get('productTypes')).__name__}"
                    f" productTypeVersion="
                    f"{type(data.get('productTypeVersion')).__name__}",
                    op=op, marketplace_id=marketplace_id,
                    status=result.status, request_id=result.request_id)
        return result

    def _call(self, op: str, spec: _OpSpec, marketplace_id: str,
              path: str, params: dict) -> SpApiResult:
        base = self._endpoint_for(marketplace_id).rstrip("/")
        url = base + path + "?" + urllib.parse.urlencode(params)
        # 区域/站点正确性（要求 7）：默认 resolver 已拒未知站点；注入的
        # resolver 也兜底再验一次 marketplace 合法性。
        if marketplace_id not in REGION_BY_MARKETPLACE:
            raise ValueError(f"未知 marketplace_id：{marketplace_id!r}")
        limiter = self._limiters[op]
        retry = self._retry
        attempts = 0
        t0 = time.monotonic()
        last_error: SpApiError | None = None
        while attempts < retry.max_attempts:
            attempts += 1
            limiter.acquire()
            # 令牌只在发请求前一刻取（要求 10：令牌不进日志/异常）
            if self._token_provider is None:
                raise SpApiAuthError(
                    "未注入 token_provider（构造 SpApiClient 时必须提供取"
                    " LWA 访问令牌的回调）", op=op,
                    marketplace_id=marketplace_id)
            try:
                token = self._token_provider()
            except SpApiError:
                raise
            except Exception as e:  # noqa: BLE001
                raise SpApiAuthError(
                    f"token_provider 取令牌失败：{type(e).__name__}",
                    op=op, marketplace_id=marketplace_id) from None
            headers = {
                "x-amz-access-token": token,
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            }
            try:
                status, resp_headers, body = self._transport(
                    spec.method, url, headers, self._timeout)
            except (socket.timeout, TimeoutError) as e:
                last_error = SpApiNetworkError(
                    f"{spec.official} 请求超时（>{self._timeout}s）",
                    phase="timeout", op=op, marketplace_id=marketplace_id)
                del e
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    last_error = SpApiNetworkError(
                        f"{spec.official} 请求超时", phase="timeout",
                        op=op, marketplace_id=marketplace_id)
                else:
                    last_error = SpApiNetworkError(
                        f"{spec.official} 连接失败：{type(reason).__name__}",
                        phase="connect", op=op, marketplace_id=marketplace_id)
            except (ConnectionError, OSError) as e:
                last_error = SpApiNetworkError(
                    f"{spec.official} 连接失败：{type(e).__name__}",
                    phase="connect", op=op, marketplace_id=marketplace_id)
            else:
                self._absorb_rate_header(op, resp_headers)
                if 200 <= status < 300:
                    data, parsed_errs = _parse_json(body)
                    if parsed_errs is None:
                        return SpApiResult(
                            op=op, marketplace_id=marketplace_id,
                            status=status, data=data, mock=_is_mock(
                                resp_headers),
                            request_id=resp_headers.get("x-amzn-requestid"),
                            rate_limit=_rate_of(resp_headers),
                            attempts=attempts,
                            elapsed=time.monotonic() - t0)
                    raise SpApiParseError(
                        f"{spec.official} 返回 200 但响应体不是合法 JSON"
                        f"（{parsed_errs}）", op=op,
                        marketplace_id=marketplace_id, status=status,
                        request_id=resp_headers.get("x-amzn-requestid"))
                errs = _error_list(body)
                req_id = resp_headers.get("x-amzn-requestid")
                kw = dict(op=op, marketplace_id=marketplace_id,
                          status=status, errors=errs, request_id=req_id)
                if status == 429:
                    ra = _retry_after(resp_headers)
                    if ra is not None and ra > retry.retry_after_cap:
                        raise SpApiRateLimitError(
                            f"{spec.official} 限流且 Retry-After={ra}s 超过"
                            f"上限 {retry.retry_after_cap}s，放弃重试",
                            retry_after=ra, **kw)
                    last_error = SpApiRateLimitError(
                        f"{spec.official} 限流（429）", retry_after=ra, **kw)
                    delay = ra if ra is not None else min(
                        retry.base_delay * (2 ** (attempts - 1)),
                        retry.max_delay)
                elif status >= 500:
                    last_error = SpApiServerError(
                        f"{spec.official} 服务端错误（{status}）", **kw)
                    ra = _retry_after(resp_headers)
                    delay = ra if ra is not None else min(
                        retry.base_delay * (2 ** (attempts - 1)),
                        retry.max_delay)
                else:
                    # 400/401/403/404/413/415……客户端错误一律不重试
                    raise _client_error(spec.official, **kw)
                if attempts < retry.max_attempts and \
                        spec.method == "GET":    # 幂等守卫（要求 5）
                    time.sleep(max(delay, 0.0))
                    continue
                raise last_error
            # 网络错误：有界重试（要求 5）
            if attempts < retry.max_attempts and spec.method == "GET":
                time.sleep(min(retry.base_delay * (2 ** (attempts - 1)),
                               retry.max_delay))
                continue
            assert last_error is not None
            raise last_error
        raise SpApiError(          # 理论不可达（循环内必 raise），保险丝
            f"{spec.official} 重试耗尽", op=op,
            marketplace_id=marketplace_id, status=None)

    def _absorb_rate_header(self, op: str, resp_headers: dict) -> None:
        v = resp_headers.get("x-amzn-ratelimit-limit")
        if v:
            try:
                self._limiters[op].update_rate(float(v))
            except (TypeError, ValueError):
                pass


def _parse_json(body: bytes) -> tuple[Any, str | None]:
    try:
        return json.loads(body.decode("utf-8", "replace")), None
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"{type(e).__name__}: {e}"[:120]


def _error_list(body: bytes) -> list[dict]:
    data, _ = _parse_json(body)
    if isinstance(data, dict) and isinstance(data.get("errors"), list):
        return [e for e in data["errors"] if isinstance(e, dict)]
    return []


def _is_mock(resp_headers: dict) -> bool:
    return str(resp_headers.get("x-wz-mock", "")).strip() == "1"


def _rate_of(resp_headers: dict) -> float | None:
    v = resp_headers.get("x-amzn-ratelimit-limit")
    if not v:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _retry_after(resp_headers: dict) -> float | None:
    v = resp_headers.get("retry-after")
    if not v:
        return None
    try:
        return max(float(v), 0.0)
    except (TypeError, ValueError):
        return None


def _client_error(official: str, *, status: int, errors: list[dict],
                  op: str, marketplace_id: str,
                  request_id: str | None) -> SpApiError:
    msgs = "; ".join(f"[{e.get('code','')}] {e.get('message','')}"
                     for e in errors)[:300]
    msg = f"{official} 客户端错误（{status}）" + (f"：{msgs}" if msgs else "")
    kw = dict(op=op, marketplace_id=marketplace_id, status=status,
              errors=errors, request_id=request_id)
    if status == 401:
        return SpApiAuthError(msg, **kw)
    if status == 403:
        return SpApiForbiddenError(msg, **kw)
    if status == 404:
        return SpApiNotFoundError(msg, **kw)
    return SpApiBadRequestError(msg, **kw)   # 400/413/415/其余 4xx

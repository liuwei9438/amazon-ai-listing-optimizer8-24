# -*- coding: utf-8 -*-
"""pt.mock_spapi —— 本地 Mock SP-API 服务（仅测试/联调用，绝不声称官方）。

铁律（模块 2 要求 9）：本服务一切响应都带 `X-WZ-Mock: 1` 头，
SpApiClient 据此把结果标 mock=True——Mock 通过永不等于真实官方验证通过。
唯一例外：no_mock_header 模式故意不发该头，专测「官方响应 → mock=False」。

契约：路径/参数/错误体形状按官方 OpenAPI 复刻（错误体一律
{"errors":[{"code","message","details"?}]}）；429 模式带 Retry-After 头；
成功响应带 x-amzn-RateLimit-Limit / x-amzn-RequestId。

故障注入（STATE["mode"]）：
  ok                    正常响应
  empty                 空候选（PTD productTypes=[]；Catalog numberOfResults=0 items=[]）
  badrequest            400 + ErrorList
  unauthorized          401 + ErrorList
  forbidden             403 + ErrorList
  notfound              404 + ErrorList
  ratelimit             恒 429（带 Retry-After=STATE["retry_after"]）
  ratelimit_then_ok     前 STATE["fail_times"] 次 429，之后正常
  server_error          恒 500
  server_error_then_ok  前 STATE["fail_times"] 次 500，之后正常
  timeout               睡 STATE["sleep"] 秒再回（测客户端超时）
  badjson               200 + 非法 JSON
  shape_bad             200 但缺官方必填字段（productTypes=123）
  bad_token             令牌=="BAD_TOKEN" 时 401（模拟令牌失效）
  no_token              无 x-amz-access-token 头 → 401
  no_mock_header        正常响应但不带 X-WZ-Mock 头

模块 6 STATE 新键（默认关闭，不影响任何既有行为）：
  schema_serve   True=PTD schema.link 指向本服务 /schemas/{pt}.json，
                 checksum=真实 md5 摘要 base64（客户端按此校验）
  schemas        None=按需生成默认 Schema（$ref/allOf/if-then/枚举/
                 pattern/数组嵌套/条件必填俱全）；dict {pt: bytes|str} 可覆盖
  schema_mode    /schemas/ 定向故障：link_dead(404)|timeout|badjson|
                 oversize(~2.5MB)；hash_wrong=PTD 里的 checksum 给错值
  pt_version     覆盖 productTypeVersion（版本变更测试用）
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MID_UK = "A1F83G8C2ARO7P"
MID_US = "ATVPDKIKX0DER"

STATE: dict = {
    "mode": "ok",
    "fail_times": 1,        # *_then_ok 模式的失败次数
    "retry_after": "1",     # 429 模式的 Retry-After 秒
    "rate_limit": "5",      # 成功响应的 x-amzn-RateLimit-Limit
    "sleep": 5.0,           # timeout 模式睡眠秒
    "calls": 0,             # 总请求数（含所有路由）
    # 模块 5 验证层用（默认 None=不影响既有行为）：
    "known_pts": None,      # None=PTD get 全知道；传集合=白名单，外者 404
    "catalog_items": None,  # None=默认三件；传 Item 列表（make_item 构造）
    # 模块 5 补强用：只打 /catalog 路由的定向故障（None=关闭）。取值
    # server_error|notfound|badjson|timeout——PTD 等其余路由照常，专测
    # 「定义拿得到、目录佐证拿不到」的降级行为。
    "catalog_mode": None,
    # 模块 6 Schema 层用（默认 None/False=不影响任何既有行为）：
    "schema_serve": False,   # True=PTD schema.link 指向本服务 /schemas/ 且带真 md5
    "schemas": None,         # None=按需生成默认 Schema；dict {pt_name: bytes|str} 自定义
    "schema_mode": None,     # /schemas/ 定向故障：link_dead|timeout|badjson|oversize；
                             # hash_wrong 只把 PTD 里的 checksum 换成错的
    "pt_version": None,      # 覆盖 productTypeVersion（None=mock-ver-1；版本变更测试用）
    # ---- S2c：Listings 模拟提交（默认关闭，不影响任何既有路由）----
    "listings_mode": "ok",   # ok|badrequest|unauthorized|ratelimit_then_ok|
                             # server_error_then_ok|timeout|rejected
    "listings_fail_times": 1,   # *_then_ok 的失败次数
    "listings_reject_skus": None,  # None=不拒；传 SKU 集合=定向拒绝
    "listings_process_after": 0,   # 状态轮询几次后才 PROCESSED（0=立即）
    "listings_get_fails_left": 0,  # S3-B2：GET 前多少次注入 429/500（0=关）
}
_LOCK = threading.Lock()
RECORDS: list[dict] = []    # 每请求一条（不含令牌值，只记长度）
LISTINGS: dict[str, dict] = {}   # S2c：幂等令牌 → 模拟提交台账
LISTING_BY_SKU: dict[str, list[dict]] = {}   # "seller|sku" → 台账引用列表
_LISTING_SEQ = [0]               # submissionId 单调序号（_LOCK 内自增）


def reset_listings() -> None:
    """清空 Listings 台账与旋钮（测试隔离用；不影响其他路由状态）。"""
    with _LOCK:
        LISTINGS.clear()
        LISTING_BY_SKU.clear()
        _LISTING_SEQ[0] = 0
        STATE["listings_mode"] = "ok"
        STATE["listings_fail_times"] = 1
        STATE["listings_get_fails_left"] = 0
        STATE["listings_reject_skus"] = None
        STATE["listings_process_after"] = 0


def make_item(asin: str, mid: str, classification_id: str,
              display_name: str, pt: str,
              parents: tuple[tuple[str, str], ...] = (
                  ("1570964031", "Car & Vehicle Electronics"),),
              cls_mid: str | None = None) -> dict:
    """官方 Item 形状构造器（测试用）。parents=根→…→直接父级链；
    cls_mid 单独指定 classifications 的 marketplaceId（跨站点证据用）。"""
    parent = None
    for cid, name in reversed(parents):
        parent = {"classificationId": cid, "displayName": name,
                  "parent": parent}
    return {
        "asin": asin,
        "identifiers": [{
            "marketplaceId": mid,
            "identifiers": [
                {"identifierType": "EAN", "identifier": "69" + asin[-7:]},
            ],
        }],
        "productTypes": [{"marketplaceId": mid, "productType": pt}],
        "classifications": [{"marketplaceId": cls_mid or mid,
                             "classifications": [
                                 {"classificationId": classification_id,
                                  "displayName": display_name,
                                  "parent": parent}]}],
    }


def set_known_pts(pts) -> None:
    """PTD get 白名单（None=恢复默认全知道）。之外的 productType 一律 404。"""
    with _LOCK:
        STATE["known_pts"] = set(pts) if pts is not None else None


def set_catalog_items(items) -> None:
    """目录检索自定义商品（None=恢复默认三件）。"""
    with _LOCK:
        STATE["catalog_items"] = [dict(i) for i in items] \
            if items is not None else None


# 模块 6：默认 JSON Schema（官方 draft 风状：$ref/allOf/if-then/枚举/
# pattern/数组嵌套/条件必填俱全——分析器契约测试就按这份断言）。
_DEFAULT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "type": "object",
    "required": ["item_name", "brand", "feed_product_type",
                 "external_product_id"],
    "properties": {
        "item_name": {"type": "string", "maxLength": 200},
        "brand": {"$ref": "#/definitions/brand_def"},
        "feed_product_type": {"type": "string",
                              "enum": ["GPS_TRACKER", "LUGGAGE"]},
        "external_product_id": {"type": "string",
                                "pattern": "^[0-9A-Za-z\\-]{6,14}$"},
        "quantity": {"type": "integer", "minimum": 1},
        "material": {"type": "string"},
        "batteries": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"cell_composition": {
                          "type": "string",
                          "enum": ["lithium_ion", "alkaline"]}},
                      "required": ["cell_composition"]}},
    },
    "allOf": [
        {"if": {"properties": {"quantity": {"minimum": 10}}},
         "then": {"required": ["item_package_quantity"]}},
        {"properties": {"supplier_declared_value_per_unit": {
            "type": ["number", "null"]}}},
    ],
    "definitions": {
        "brand_def": {"type": "object",
                      "properties": {"value": {"type": "string",
                                               "maxLength": 50}},
                      "required": ["value"]},
    },
}


def default_schema(pt: str) -> bytes:
    """默认 Schema（确定性字节：sort_keys 紧凑序列化）。"""
    doc = json.loads(json.dumps(_DEFAULT_SCHEMA))     # 深拷贝防串改
    doc["product_type"] = str(pt)
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def schema_bytes(pt: str) -> bytes:
    """STATE["schemas"] 自定义优先，缺省按需生成。"""
    with _LOCK:
        custom = (STATE["schemas"] or {}).get(pt)
    if custom is not None:
        return custom.encode("utf-8") if isinstance(custom, str) else custom
    return default_schema(pt)


def md5_b64(data: bytes) -> str:
    """官方 checksum 同式：MD5 摘要的 base64（客户端按此校验）。"""
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def _error_body(code: str, message: str, details: str = "") -> bytes:
    e = {"code": code, "message": message}
    if details:
        e["details"] = details
    return json.dumps({"errors": [e]}, ensure_ascii=False).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):     # 静音（测试输出干净）
        pass

    # ---- 工具 ----

    def _send(self, status: int, body: bytes, *, mock: bool = True,
              extra: dict | None = None) -> None:
        with _LOCK:
            STATE["calls"] += 1
            n = STATE["calls"]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-amzn-RequestId", f"mock-req-{n}")
        if mock:
            self.send_header("X-WZ-Mock", "1")
        if status < 300:
            self.send_header("x-amzn-RateLimit-Limit", str(STATE["rate_limit"]))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, code: str, message: str) -> None:
        self._send(status, _error_body(code, message))

    def _record(self, qs: dict) -> None:
        token = self.headers.get("x-amz-access-token") or ""
        with _LOCK:
            RECORDS.append({
                "method": self.command,
                "path": self.path,
                "query": {k: v for k, v in qs.items()},
                "token_len": len(token),
                "ua": self.headers.get("User-Agent", ""),
            })

    def _mode(self) -> str:
        with _LOCK:
            return STATE["mode"]

    def _hits(self) -> int:
        """当前路由被命中次数（判 *_then_ok 用）。"""
        with _LOCK:
            return sum(1 for r in RECORDS
                       if r["path"].split("?")[0] == self.path.split("?")[0])

    # ---- S2c：Listings 模拟提交（PUT 受理 + GET 终态轮询）----
    # 契约按官方 Listings Items API 形状简化：PUT /listings/2021-08-01/
    # items/{sellerId}/{sku} → {submissionId,status}；幂等令牌走
    # X-WZ-Idempotency-Token 头（同令牌重放=返回同一 submissionId，
    # 绝不产生第二次受理）。GET 同路径 → 当前模拟终态。

    def do_PUT(self) -> None:      # noqa: N802（http.server 约定）
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        # /listings/2021-08-01/items/{sellerId}/{sku}
        if (len(parts) == 5 and parts[0] == "listings"
                and parts[2] == "items"):
            self._record({})
            return self._listings_put(parts[3], parts[4])
        self._record({})
        self._err(404, "NotFound", f"mock 无此路由：{u.path}")

    def _listings_auth(self) -> bool:
        token = self.headers.get("x-amz-access-token") or ""
        mode = self._mode()
        if mode == "no_token" and not token:
            self._err(401, "Unauthorized", "缺少 x-amz-access-token（mock）")
            return False
        if mode == "bad_token" and token == "BAD_TOKEN":
            self._err(401, "InvalidAuthenticationToken",
                      "访问令牌无效或已过期（mock）")
            return False
        return True

    def _listings_put(self, seller: str, sku: str) -> None:
        if not self._listings_auth():
            return
        with _LOCK:
            lm = STATE["listings_mode"]
            ft = int(STATE["listings_fail_times"])
            reject = set(STATE["listings_reject_skus"] or ())
        if lm == "badrequest":
            return self._err(400, "InvalidInput", "载荷不合法（mock）")
        if lm == "unauthorized":
            return self._err(401, "Unauthorized", "认证失败（mock）")
        if lm in ("ratelimit_then_ok", "server_error_then_ok") \
                and self._hits() <= ft:
            if lm == "ratelimit_then_ok":
                return self._send(
                    429, _error_body("QuotaExceeded", "请求超限（mock）"),
                    extra={"Retry-After": "0"})
            return self._send(500, _error_body("InternalFailure",
                                               "服务端错误（mock）"))
        if lm == "timeout":
            time.sleep(float(STATE["sleep"]))
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:  # noqa: BLE001
            return self._err(400, "InvalidInput", "载荷不是合法 JSON（mock）")
        token = self.headers.get("X-WZ-Idempotency-Token") or ""
        with _LOCK:
            hit = LISTINGS.get(token) if token else None
            if hit is not None:      # 幂等重放：同一令牌=同一受理
                rec = dict(hit)
            else:
                _LISTING_SEQ[0] += 1
                n = _LISTING_SEQ[0]
                rejected = (lm == "rejected") or (sku in reject)
                rec = {
                    "token": token, "submissionId": f"mock-sub-{n}",
                    "sellerId": seller, "sku": sku,
                    "status": "REJECTED" if rejected else "PROCESSING",
                    "issues": ([{"code": "5000", "message":
                                 "模拟平台拒绝：必填属性缺失（mock）"},
                               {"code": "5001", "message":
                                "模拟平台拒绝：属性值与 Schema 约束"
                                "不符（mock）"}]
                               if rejected else []),
                    "payload": payload, "polls": 0, "at": time.time(),
                }
                if token:
                    LISTINGS[token] = rec
                LISTING_BY_SKU.setdefault(f"{seller}|{sku}", []).append(rec)
            resp_status = rec["status"]
        self._send(200, json.dumps({
            "submissionId": rec["submissionId"],
            "status": resp_status}, ensure_ascii=False).encode())

    def _listings_get(self, seller: str, sku: str) -> None:
        if not self._listings_auth():
            return
        with _LOCK:
            lm = STATE["listings_mode"]
            gl = int(STATE.get("listings_get_fails_left") or 0)
            if gl > 0:
                STATE["listings_get_fails_left"] = gl - 1
        # S3-B2：GET 故障旋钮（结果 unknown 语义测试用——受理后查询
        # 断线/超限/认证失败 ≠ 平台拒绝）：unauthorized=恒 401；
        # listings_get_fails_left>0 时按 listings_mode 注入 429/500
        # （计数独立于 PUT 的 _hits，PUT/GET 互不干扰）
        if lm == "unauthorized":
            return self._err(401, "Unauthorized", "认证失败（mock）")
        if gl > 0:
            if lm == "server_error_then_ok":
                return self._send(500, _error_body(
                    "InternalFailure", "服务端错误（mock）"))
            return self._send(
                429, _error_body("QuotaExceeded", "请求超限（mock）"),
                extra={"Retry-After": "0"})
        if lm == "timeout":
            # 睡在锁外（持锁睡会挡住全服）；醒来再取锁读台账
            time.sleep(float(STATE["sleep"]))
        with _LOCK:
            after = int(STATE["listings_process_after"])
            lst = LISTING_BY_SKU.get(f"{seller}|{sku}") or []
            rec = lst[-1] if lst else None
            if rec is None:
                missing = True
            else:
                missing = False
                if rec["status"] == "PROCESSING":
                    rec["polls"] += 1
                    if rec["polls"] > after:
                        rec["status"] = "PROCESSED"
                out = {"sku": sku, "submissionId": rec["submissionId"],
                       "status": rec["status"], "issues": rec["issues"]}
        # 发响应不持锁（_send 自取锁——持锁调用会自锁死）
        if missing:
            return self._err(404, "NotFound",
                             f"该 SKU 尚无模拟提交：{sku}")
        self._send(200, json.dumps(out, ensure_ascii=False).encode())

    # ---- 路由 ----

    def do_GET(self) -> None:      # noqa: N802（http.server 约定）
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True)
              .items()}
        self._record(qs)
        mode = self._mode()
        mid = qs.get("marketplaceIds", "")

        # 认证层（no_token/bad_token 优先于故障模式）
        token = self.headers.get("x-amz-access-token") or ""
        if mode == "no_token" and not token:
            self._err(401, "Unauthorized",
                      "缺少 x-amz-access-token（mock）")
            return
        if mode == "bad_token" and token == "BAD_TOKEN":
            self._err(401, "InvalidAuthenticationToken",
                      "访问令牌无效或已过期（mock）")
            return

        # 全局故障模式（不分路由）
        if mode == "unauthorized":
            self._err(401, "Unauthorized", "认证失败（mock）")
            return
        if mode == "forbidden":
            self._err(403, "UnauthorizedAccess",
                      "无该资源访问权限（mock）")
            return
        if mode == "badrequest":
            self._err(400, "InvalidInput", "请求参数不合法（mock）")
            return
        if mode == "notfound":
            self._err(404, "NotFound", "资源不存在（mock）")
            return
        if mode == "ratelimit":
            self._send(429, _error_body("QuotaExceeded", "请求超限（mock）"),
                       extra={"Retry-After": str(STATE["retry_after"])})
            return
        if mode == "ratelimit_then_ok":
            if self._hits() <= STATE["fail_times"]:
                self._send(429, _error_body("QuotaExceeded",
                                            "请求超限（mock）"),
                           extra={"Retry-After": str(STATE["retry_after"])})
                return
        if mode == "server_error":
            self._send(500, _error_body("InternalFailure", "服务端错误（mock）"))
            return
        if mode == "server_error_then_ok":
            if self._hits() <= STATE["fail_times"]:
                self._send(500, _error_body("InternalFailure",
                                            "服务端错误（mock）"))
                return
        if mode == "timeout":
            time.sleep(float(STATE["sleep"]))
            mode = "ok"      # 睡完给正常响应（客户端早超时了也无所谓）
        if mode == "badjson":
            self._send(200, b"<<<not-json-at-all>>>")
            return
        if mode == "shape_bad":
            # 200 但缺官方必填字段/类型不对（PTD search 路由专用）
            self._send(200, json.dumps(
                {"productTypes": 123,
                 "productTypeVersion": "mock-ver-1"}).encode())
            return
        if mode == "no_mock_header":
            self._send(200, self._body_ok(u.path, qs, mid), mock=False)
            return

        # 目录路由定向故障（模块 5 补强；默认 None 不影响任何既有行为）
        with _LOCK:
            cm = STATE["catalog_mode"]
        if cm and u.path.startswith("/catalog/"):
            if cm == "server_error":
                self._send(500, _error_body("InternalFailure",
                                            "目录接口故障（mock）"))
                return
            if cm == "notfound":
                self._err(404, "NotFound", "目录资源不存在（mock）")
                return
            if cm == "badjson":
                self._send(200, b"<<<not-json>>>")
                return
            if cm == "timeout":
                time.sleep(float(STATE["sleep"]))
                self._send(200, b'{"numberOfResults":0,"items":[]}')
                return

        # 重定向测试端点（模块 6 修正 1：只给 _RedirectGuard 测试用，
        # to= 目标完全由测试指定——专测跨主机/降级跳转被客户端拒）
        if u.path == "/_redirect":
            self._send(302, b"", extra={"Location": qs.get("to", "")})
            return

        # Schema 路由定向故障（模块 6；默认 None 不影响任何既有行为）
        with _LOCK:
            sm = STATE["schema_mode"]
            schema_sleep = STATE["sleep"]
        if sm and u.path.startswith("/schemas/"):
            if sm == "link_dead":
                self._err(404, "NotFound", "Schema 链接已失效（mock）")
                return
            if sm == "timeout":
                time.sleep(float(schema_sleep))
                self._send(200, b'{"type":"object"}')
                return
            if sm == "badjson":
                self._send(200, b"<<<not-json>>>")
                return
            if sm == "oversize":
                self._send(200, b'{"padding":"' + b"x" * 2_500_000 + b'"}')
                return

        # S2c：Listings 终态轮询（自带响应；不经 _body）
        if u.path.startswith("/listings/2021-08-01/items/"):
            parts = [p for p in u.path.split("/") if p]
            return self._listings_get(parts[3], parts[4])

        body = self._body(u.path, qs, mid)
        if body is None:
            self._err(404, "NotFound", f"mock 无此路由：{u.path}")
            return
        self._send(200, body)

    # ---- 响应构造（按官方 OpenAPI 形状） ----

    def _body(self, path: str, qs: dict, mid: str) -> bytes | None:
        if path.startswith("/schemas/"):
            return self._schema_get(path.rsplit("/", 1)[1])
        if path == "/definitions/2020-09-01/productTypes":
            return self._ptd_search(qs, mid)
        if path.startswith("/definitions/2020-09-01/productTypes/"):
            pt = path.rsplit("/", 1)[1]
            return self._ptd_get(pt, qs, mid)
        if path == "/catalog/2022-04-01/items":
            return self._cat_search(qs, mid)
        if path.startswith("/catalog/2022-04-01/items/"):
            return self._cat_get(path.rsplit("/", 1)[1], qs, mid)
        return None

    @staticmethod
    def _item(asin: str, mid: str, classification_id: str,
              display_name: str, pt: str) -> dict:
        """默认 Item 形状（历史行为不变：单层父链）。"""
        return make_item(asin, mid, classification_id, display_name, pt)

    def _ptd_search(self, qs: dict, mid: str) -> bytes:
        if self._mode_force_empty():
            doc = {"productTypes": [], "productTypeVersion": "mock-ver-1"}
        else:
            doc = {
                "productTypes": [
                    {"name": "LUGGAGE", "displayName": "Luggage",
                     "marketplaceIds": [mid]},
                    {"name": "BACKPACK", "displayName": "Backpacks",
                     "marketplaceIds": [mid]},
                    {"name": "CARRY_ON", "displayName": "Carry-Ons",
                     "marketplaceIds": [mid]},
                ],
                "productTypeVersion": "mock-ver-1",
            }
        return json.dumps(doc, ensure_ascii=False).encode()

    def _ptd_get(self, pt: str, qs: dict, mid: str) -> bytes | None:
        with _LOCK:
            kp = STATE["known_pts"]
            serve = STATE["schema_serve"]
            smode = STATE["schema_mode"]
            ver = STATE["pt_version"]
        if kp is not None and pt not in kp:
            return None                 # 白名单外 → do_GET 统一回 404
        doc = {
            "metaSchema": {
                "link": {"resource": f"https://mock.local/meta/{pt}.json",
                         "verb": "GET"},
                "checksum": "mock-meta-md5==",
            },
            "schema": {
                "link": {"resource": f"https://mock.local/schema/{pt}.json",
                         "verb": "GET"},
                "checksum": "mock-schema-md5==",
            },
            "requirements": qs.get("requirements", "LISTING"),
            "requirementsEnforced": qs.get("requirementsEnforced",
                                           "ENFORCED"),
            "propertyGroups": [{
                "propertyGroup": {
                    "title": {"propertyNames": ["item_name"]}},
                "requirements": [{"name": "item_name", "level": "REQUIRED"}],
            }],
            "locale": qs.get("locale", "en_GB"),
            "marketplaceIds": [mid],
            "productType": pt,
            "displayName": pt.replace("_", " ").title(),
            "productTypeVersion": ver or "mock-ver-1",
        }
        if serve:
            # 模块 6：schema.link 指向本服务 /schemas/ 路由，checksum=真实
            # md5(base64)（客户端可校验）；hash_wrong=故意给错值
            body = schema_bytes(pt)
            host = self.headers.get("Host") or "127.0.0.1"
            doc["schema"] = {
                "link": {"resource": f"http://{host}/schemas/{pt}.json",
                         "verb": "GET"},
                "checksum": ("intentionally-wrong==" if smode == "hash_wrong"
                             else md5_b64(body)),
            }
        if qs.get("sellerId"):
            # 官方 2020-09-01 响应无 sellerId 字段——故意不加（契约测试守卫）
            pass
        return json.dumps(doc, ensure_ascii=False).encode()

    def _cat_search(self, qs: dict, mid: str) -> bytes:
        if self._mode_force_empty():
            doc = {"numberOfResults": 0, "items": []}
            return json.dumps(doc, ensure_ascii=False).encode()
        cid = qs.get("classificationIds", "")
        with _LOCK:
            custom = STATE["catalog_items"]
        if custom is not None:
            items = [dict(i) for i in custom]
        else:
            items = [
                self._item("B0MOCK0001", mid, "10971181031",
                           "GPS Trackers", "GPS_TRACKER"),
                self._item("B0MOCK0002", mid, "10971181031",
                           "GPS Trackers", "GPS_TRACKER"),
                self._item("B0MOCK0003", mid, "348445031",
                           "Phone Cases", "PHONE_CASE"),
            ]
        if cid:
            keep = [i for i in items
                    if any(c["classificationId"] == cid
                           for grp in i["classifications"]
                           for c in grp["classifications"])]
            doc = {"numberOfResults": len(keep), "items": keep}
        else:
            doc = {"numberOfResults": len(items), "items": items}
        return json.dumps(doc, ensure_ascii=False).encode()

    def _cat_get(self, asin: str, qs: dict, mid: str) -> bytes | None:
        table = {
            "B0MOCK0001": ("10971181031", "GPS Trackers", "GPS_TRACKER"),
            "B0MOCK0002": ("10971181031", "GPS Trackers", "GPS_TRACKER"),
            "B0MOCK0003": ("348445031", "Phone Cases", "PHONE_CASE"),
        }
        if asin not in table:
            return None                    # do_GET 统一回 404
        cid, name, pt = table[asin]
        return json.dumps(self._item(asin, mid, cid, name, pt),
                          ensure_ascii=False).encode()

    def _schema_get(self, fname: str) -> bytes:
        """/schemas/{pt}.json（模块 6）——自定义优先，缺省按需生成。"""
        name = fname[:-5] if fname.endswith(".json") else fname
        return schema_bytes(name)

    def _mode_force_empty(self) -> bool:
        with _LOCK:
            return STATE["mode"] == "empty"

    def _body_ok(self, path: str, qs: dict, mid: str) -> bytes:
        if self._mode_force_empty():
            if path == "/definitions/2020-09-01/productTypes":
                return json.dumps({"productTypes": [],
                                   "productTypeVersion": "mock-ver-1"}
                                  ).encode()
            return json.dumps({"numberOfResults": 0, "items": []}).encode()
        if path.startswith("/schemas/"):
            return self._schema_get(path.rsplit("/", 1)[1])
        if path == "/definitions/2020-09-01/productTypes":
            return self._ptd_search(qs, mid)
        if path.startswith("/definitions/2020-09-01/productTypes/"):
            return self._ptd_get(path.rsplit("/", 1)[1], qs, mid)
        if path == "/catalog/2022-04-01/items":
            return self._cat_search(qs, mid)
        return self._cat_get(path.rsplit("/", 1)[1], qs, mid) or b"{}"


def set_mode(mode: str, **kw) -> None:
    """测试里切故障模式（顺带可改 fail_times/retry_after/sleep/rate_limit）。"""
    with _LOCK:
        STATE["mode"] = mode
        for k, v in kw.items():
            STATE[k] = v
        RECORDS.clear()          # 每次切模式清请求记录，断言干净


def records() -> list[dict]:
    with _LOCK:
        return list(RECORDS)


def calls() -> int:
    with _LOCK:
        return STATE["calls"]


def serve(port: int = 18998) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def url(port: int = 18998) -> str:
    return f"http://127.0.0.1:{port}"

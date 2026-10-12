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
"""

from __future__ import annotations

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
}
_LOCK = threading.Lock()
RECORDS: list[dict] = []    # 每请求一条（不含令牌值，只记长度）


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

        body = self._body(u.path, qs, mid)
        if body is None:
            self._err(404, "NotFound", f"mock 无此路由：{u.path}")
            return
        self._send(200, body)

    # ---- 响应构造（按官方 OpenAPI 形状） ----

    def _body(self, path: str, qs: dict, mid: str) -> bytes | None:
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
            "productTypeVersion": "mock-ver-1",
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

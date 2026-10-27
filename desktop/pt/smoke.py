# -*- coding: utf-8 -*-
"""pt.smoke —— S3-C 第三阶段：真实只读 SP-API smoke runner（默认 dry-run）。

铁律
----
1. **默认 dry-run**：run() 不带 authorization 绝不发任何网络请求，
   只输出就绪计划（ready）或 BLOCKED+缺失清单。
2. **真实执行需单独授权**：`SmokeAuthorization` 必须由调用方显式构造
   （granted_by=操作人、note=授权说明）。**本交付内没有任何代码构造
   它**——pt_api 路由只接 dry-run（源码守卫测试锚定）；未来获批后由
   独立提交接线。
3. **只读与写通道完全隔离**：本模块不 import（也不被 import）任何
   Listings 写路径（listing/listing_gateway/mock_spapi 的提交面）；
   六步全部 GET，无任何写操作。
4. **不以 Mock 成功冒充真实成功**：dry 模式的输出永远写 would_run/
   blocked，绝不出现「验证通过」；live 模式逐项记录真实 SP-API 的
   状态与错误分类，失败就是失败。
5. 请求/响应日志脱敏：报告与异常只含操作名/状态码/官方错误摘要，
   不含令牌（令牌只在 spapi_client 发请求瞬间进 header）。

六步清单（对应 S3-B3 readiness 的最小只读面）
----------------------------------------------
S1 store_identity        本地校验身份三元组（零网络）
S2 search_product_types  GET /definitions/2020-09-01/productTypes
S3 get_product_type      GET /definitions/2020-09-01/productTypes/{pt}
S4 fetch_schema          GET PTD schema.link（受限下载，allow-list）
S5 search_catalog_items  GET /catalog/2022-04-01/items（SKU 维度）
S6 get_catalog_item      GET /catalog/2022-04-01/items/{asin}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .authz import AuthzError, StoreAuthzManager


class SmokeDisabled(RuntimeError):
    """dry 模式下请求真实执行（编程错误/越权尝试）。"""


@dataclass(frozen=True)
class SmokeAuthorization:
    """真实执行授权（显式构造；本交付内无构造点）。

    granted_by：授权操作人（审计用）；note：授权说明。
    没有任何 env/config/UI 路径能凭空造出这个对象。
    """
    granted_by: str
    note: str = ""


# 六步定义（op=spapi_client.SpApiClient 方法名；全 GET）
# param=params 里的参数键名（空缺=该步 skipped，不阻断）
SMOKE_STEPS: tuple[dict, ...] = (
    {"key": "store_identity", "name": "店铺身份校验",
     "op": None, "param": "", "endpoint": "(local)", "kind": "local"},
    {"key": "search_product_types", "name": "PT 检索",
     "op": "search_product_types", "param": "search_keywords",
     "kind": "read_only",
     "endpoint": "GET /definitions/2020-09-01/productTypes"},
    {"key": "get_product_type", "name": "PTD 定义获取",
     "op": "get_product_type", "param": "product_type",
     "kind": "read_only",
     "endpoint": "GET /definitions/2020-09-01/productTypes/{pt}"},
    {"key": "fetch_schema", "name": "PTD Schema 下载",
     "op": "fetch_schema", "param": "schema_link",
     "kind": "read_only",
     "endpoint": "GET {schema.link}（受限下载）"},
    {"key": "search_catalog_items", "name": "Catalog SKU 检索",
     "op": "search_catalog_items", "param": "sku",
     "kind": "read_only",
     "endpoint": "GET /catalog/2022-04-01/items"},
    {"key": "get_catalog_item", "name": "Catalog 条目获取",
     "op": "get_catalog_item", "param": "asin",
     "kind": "read_only",
     "endpoint": "GET /catalog/2022-04-01/items/{asin}"},
)


class SmokeRunner:
    """只读 smoke 计划与执行器。

    deps（注入，可离线测试）：
      authz     StoreAuthzManager（身份+令牌通道）
      live_env  () -> Environment|None（None=无真实传输能力）
      client    (sid) -> SpApiClient（live 客户端工厂；仅 live 模式调用）
      params    () -> dict  检索参数（keywords/pt/sku/asin/schema_link；
                             空缺步自动跳过并在报告标注 skipped）
    """

    def __init__(self, authz: StoreAuthzManager, live_env: Callable,
                 client: Callable[[str], object], params: Callable[[], dict]):
        self._authz = authz
        self._live_env = live_env
        self._client = client
        self._params = params

    # ---- 计划（dry；零网络） ----

    def plan(self, sid: str, *, probe: bool = False) -> dict:
        """就绪计划：逐步 ready/blocked/skipped + 缺失清单。

        probe=True 时对目标店铺做一次授权 probe（打 worker 换令牌，
        **不是 SP-API 调用**）；默认 False=纯本地判定（unknown 状态
        按「未验证」列入缺失项，不冒充也不否定）。
        """
        sid = str(sid or "").strip()
        params = dict(self._params() or {})
        missing: list[str] = []
        steps: list[dict] = []
        env_ok = self._live_env() is not None
        if not env_ok:
            missing.append("live 传输环境未配置（live_transport/schema_hosts）")
        # 身份（本地；AuthzError→整计划 BLOCKED）
        try:
            ident = self._authz.identity_of(sid)
            auth = None
            if probe:
                rec = self._authz.probe(sid)
                auth = rec.view()
                if rec.state != "ok":
                    missing.append(
                        f"店铺授权不可用（{rec.category}）：{rec.detail[:80]}")
            else:
                st = self._authz.status(sid)
                auth = st["auth"]
                if auth["state"] == "failed":
                    missing.append(
                        f"店铺授权失败（{auth['category']}）——上次探测："
                        f"{auth['detail'][:80]}")
                elif auth["state"] == "unknown":
                    missing.append(
                        "店铺授权未验证（从未探测）——就绪判定需 probe 或"
                        "绑定 LWA 后重试")
            steps.append({**SMOKE_STEPS[0], "status": "ready",
                          "identity": ident.as_view()})
        except AuthzError as e:
            missing.append(f"店铺身份不可用（{e.category}）：{e}")
            steps.append({**SMOKE_STEPS[0], "status": "blocked",
                          "reason": f"{e.category}"})
        # 网络步：环境+授权双就绪才 ready；参数空缺=skipped
        for st in SMOKE_STEPS[1:]:
            has_param = bool(str(params.get(st["param"]) or "").strip())
            blocked_env = not env_ok
            blocked_auth = any("授权" in m for m in missing)
            if blocked_env or blocked_auth:
                steps.append({**st, "status": "blocked",
                              "reason": ("无 live 传输环境" if blocked_env
                                         else "店铺授权未就绪")})
                continue
            if not has_param:
                steps.append({**st, "status": "skipped",
                              "reason": "参数空缺（不影响其他步）"})
                continue
            steps.append({**st, "status": "ready"})
        ready = all(s["status"] in ("ready", "skipped")
                    for s in steps) and not missing
        return {"ok": True, "store_id": sid,
                "mode": "dry", "status": "ready" if ready else "blocked",
                "missing": missing, "steps": steps,
                "note": ("dry-run：未发出任何网络请求；就绪≠验证通过"
                         if ready else
                         "dry-run：未发出任何网络请求；缺失项补齐后才"
                         "具备真实只读 smoke 条件")}

    # ---- 执行（dry 拒绝 / live 需显式授权对象） ----

    def run(self, sid: str, authorization: SmokeAuthorization | None = None,
            *, probe: bool = False) -> dict:
        """dry（默认）：返回计划（零网络）。live：authorization 显式
        传入才执行六步真实 GET；任何一步异常=该步 failed+分类，绝不算
        整体通过，绝不重试写面（本模块没有写面）。"""
        p = self.plan(sid, probe=probe)
        if authorization is None:
            return p
        if p["status"] != "ready":
            return {**p, "executed": False,
                    "note": "计划未就绪，拒绝执行（缺失项见 missing）"}
        ident = self._authz.identity_of(sid)
        cli = self._client(sid)
        mk = ident.marketplace_id
        params = dict(self._params() or {})
        results = []
        n_ok = 0
        for st in SMOKE_STEPS:
            if st["key"] == "store_identity":
                results.append({**st, "status": "ok",
                                "identity": ident.as_view()})
                n_ok += 1
                continue
            arg = str(params.get(st["param"]) or "").strip()
            if not arg:
                results.append({**st, "status": "skipped",
                                "reason": "参数空缺"})
                continue
            try:
                if st["key"] == "search_product_types":
                    res = cli.search_product_types(mk, keywords=[arg])
                elif st["key"] == "get_product_type":
                    res = cli.get_product_type(arg, mk)
                elif st["key"] == "fetch_schema":
                    res = cli.fetch_schema(arg)
                elif st["key"] == "search_catalog_items":
                    res = cli.search_catalog_items(
                        mk, identifiers=[arg], identifiers_type="SKU")
                else:
                    res = cli.get_catalog_item(arg, mk)
                # 成功判定：无异常（_call_checked 已验官方形状）且非 mock
                # 环境（mock 结果绝不计为官方成功——铁律 4）
                ok = getattr(res, "mock", True) is False \
                    and int(getattr(res, "status", 0)) == 200
                results.append({**st, "status": "ok" if ok else "failed",
                                "http": getattr(res, "status", None),
                                "official": not getattr(res, "mock", True)})
                n_ok += 1 if ok else 0
            except Exception as e:  # noqa: BLE001 —— 分类记录，不中断余步
                results.append({**st, "status": "failed",
                                "error_kind": type(e).__name__,
                                "error": str(e)[:160]})
        return {"ok": n_ok > 0 and all(
                    r["status"] in ("ok", "skipped") for r in results),
                "store_id": sid, "mode": "live",
                "authorization": {"granted_by": authorization.granted_by,
                                  "note": authorization.note[:120]},
                "results": results,
                "note": "live 只读执行：逐项真实结果；任何 failed=未通过，"
                        "不以部分成功冒充整体通过"}


__all__ = ["SmokeDisabled", "SmokeAuthorization", "SMOKE_STEPS",
           "SmokeRunner", "AuthzError"]

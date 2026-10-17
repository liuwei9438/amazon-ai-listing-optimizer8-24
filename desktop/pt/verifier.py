# -*- coding: utf-8 -*-
"""pt.verifier —— 模块 5：分类验证与 Browse Node 证据管理
（编号统一见 2026-10-12 批复：5=验证与 Browse Node / 6=Schema 与属性 /
7=批量任务 / 8=UI）。

只做验证与证据，不做选择（选择属模块 4）、不碰 Schema（属模块 6）。
铁律（模块 5 批复 10 条）：
1. 独立验证：PT 官方存在性（getDefinitionsProductType）与商品语义
   适配性分开出结论，互不代劳——存在性过了语义照样可能不合格，反之
   亦然。语义适配不只看 displayName 词汇重叠（2026-10-12 补强 1）：
   四路可解释证据=商品理解词 ×（官方 displayName/PT 名 + 官方类型
   定义属性名）+ 目录同类商品佐证（同类有多少官方标注该 PT）+ 候选
   来源追溯；跨语言 displayName 零重叠时目录强佐证可支撑 passed，
   双向否定才 failed，佐证拿不到只降说明为 unavailable 绝不伪装否定。
2. Browse Node 证据四级分记（2026-10-12 补强 2）：官方目录观察
   （evidence_status，哪些 ASIN 链引用）、同类商品佐证（similar_seen/
   referencing/占比）、当前商品适配（fit_status）、实际刊登可用性
   （usability=not_assessable——Catalog 节点被引用≠该商品可在此节点
   刊登，刊登可用性须待属性检查（模块 6）与真实上传验证，本模块
   绝不自动放行）分别记录（落 pt_browse_node 列+evidence_json 四键）。
3. Catalog 检索为空 = 证据不足（insufficient，kind=empty_search），
   **绝不判「节点不存在」**——官方目录覆盖率有限，absence of evidence
   不是 evidence of absence。本模块四个操作下「节点不存在」本来就
   不可证明，evidence_status 永不写 missing（目录检索 404 亦按
   unavailable 处理，同样不判存在性）。
4. 零售前台分类树（/b 页挖的树、btmap 等）不是官方验证结果：
   record_unofficial_hint() 只能登记 pending 线索，且来源命名不得
   冒充 spapi；confirmed 只能出自 save_browse_node_verification()
   （官方 Catalog 证据，source 恒为 spapi_catalog）。
5. 验证记录五要素齐全：source（官方操作名|manual）、marketplace_id、
   store_id、checked_at（注入 now_fn，可测）、env（client.env_mode
   透传——mock 环境验证 verified_against=mock，永不冒充 live 官方）。
6. PT 验证通过绝不自动带过 Browse Node / Schema：三类 check_kind
   （pt_exists / semantic / browse_node）各自出结论，没有总括
   「verified」字段；Schema 属模块 6，本模块不写 attribute 检查。
7. 状态四态可分（另加 skipped/error）：
   passed=官方确认；failed=官方明确否定（仅 PTD 404 这种确定性否定）；
   insufficient=证据不足（空目录/无引用/弱语义）；unavailable=接口
   不可用（网络/5xx/429/解析失败）；forbidden=权限不足（401/403，
   拿不到官方结论 ≠ 官方否定）；skipped=前置检查未过免检；
   error=己方参数/环境违规。
8. 人工确认 ≠ 官方接口验证：record_manual_confirmation() 落
   status=passed 但 source="manual"、official=False——记录可查、
   可审计，但任何消费方都能一眼看出这不是官方接口的结论。
9. 纯业务逻辑 + 注入式接口：client（SpApiClient 协议）/ store /
   now_fn 全部注入；verify_* 纯查询不落库，save_* / record_* 才写
   store；不碰 main/上传/图片模块，不改现有上传流程。
10. 无单商品硬编码：阈值集中在文件头（SEMANTIC_FIT_MIN 单源取自
    matcher.ABS_MIN_SEMANTIC，匹配与验证同一把语义尺子）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pt.material import ProductMaterial
from pt.matcher import ABS_MIN_SEMANTIC, semantic_terms
from pt.spapi_client import (
    SpApiClient, SpApiAuthError, SpApiBadRequestError, SpApiEnvironmentError,
    SpApiError, SpApiForbiddenError, SpApiNetworkError, SpApiNotFoundError,
    SpApiParseError, SpApiRateLimitError, SpApiServerError,
)

# ---- 通用常量（与商品无关；调优改这里，不许加 if 某商品） ----
SEMANTIC_FIT_MIN = ABS_MIN_SEMANTIC   # 语义适配下限（单源：matcher 同尺）
BN_KEYWORDS_MAX = 8                   # 目录检索词上限（官方上限 20，留余量）
BN_PAGE_SIZE = 20                     # 单页证据够用；翻页聚合属模块 7
OFFICIAL_BN_SOURCE = "spapi_catalog"  # 官方目录证据来源名（落 store.source）
# 补强 1：目录同类佐证门槛（同类商品官方标注该 PT 的占比/数量）
CATALOG_CORROB_SHARE = 0.5            # 占比下限（≥ 一半同类在用才算佐证）
CATALOG_CORROB_MIN_ITEMS = 2          # 可比同类数量下限（1 件说明不了问题）

# 状态常量（落库对齐 store.VALID_VSTATUS，模块 5 起后两值可写）
S_PASSED = "passed"
S_FAILED = "failed"
S_INSUFFICIENT = "insufficient"
S_UNAVAILABLE = "unavailable"
S_FORBIDDEN = "forbidden"
S_SKIPPED = "skipped"
S_ERROR = "error"

_SRC_PTD_GET = "spapi:getDefinitionsProductType"
_SRC_CAT_SEARCH = "spapi:searchCatalogItems"

_CJK_RE = re.compile(r"[一-鿿]")


@dataclass
class CheckResult:
    """单项检查结论（只读值对象；official=结论是否出自官方接口数据）。"""
    check_kind: str                 # pt_exists | semantic | browse_node
    status: str                     # 见铁律 7
    source: str                     # spapi:操作名 | manual | internal
    official: bool                  # passed/failed 官方结论恒 True；
                                    # forbidden 以下/manual 恒 False
    detail: dict = field(default_factory=dict)


@dataclass
class PtVerification:
    """一次 PT 验证的完整记录：存在性与语义适配两条独立结论。"""
    pt_name: str
    marketplace_id: str
    env: str                         # client.env_mode 透传（mock|live）
    checked_at: int
    exists: CheckResult
    semantic: CheckResult


@dataclass
class BrowseNodeVerification:
    """一次 Browse Node 验证：证据四级分记（补强 2）。"""
    node_id: str
    marketplace_id: str
    env: str
    checked_at: int
    existence: CheckResult           # ①官方目录观察：confirmed/insufficient
    catalog: CheckResult             # 目录证据本身（检索成败+引用清单+佐证占比）
    fit: CheckResult                # ③当前商品适配：fit/mismatch/unknown
    usability: CheckResult           # ④实际刊登可用性：恒 not_assessable


def _tokens(text: str) -> set[str]:
    """与 material/candidates/matcher 同法分词（拉丁词+中文 2 字滑窗）。"""
    out = {w.lower() for w in
           re.findall(r"[A-Za-z0-9][\w\-\+\.]*", str(text or ""))
           if len(w) >= 2}
    cjk = "".join(_CJK_RE.findall(str(text or "")))
    for i in range(len(cjk) - 1):
        out.add(cjk[i:i + 2])
    return out


def _overlap_score(terms: set[str], basis: str) -> float:
    """本体语义词 × 证据文本的重叠率（与 matcher._semantic_score 同式）。"""
    basis_tokens = _tokens(basis)
    if not terms or not basis_tokens:
        return 0.0
    overlap = terms & basis_tokens
    return round(min(len(overlap) / min(len(terms), 4), 1.0), 4)


def _err_kind(e: BaseException) -> str:
    """SpApi/参数异常 → 细分 kind（与 candidates 报告同一套词汇）。"""
    for exc, kind in (
        (SpApiEnvironmentError, "environment"),
        (SpApiAuthError, "auth"),
        (SpApiForbiddenError, "forbidden"),
        (SpApiRateLimitError, "rate_limit"),
        (SpApiNetworkError, "network"),
        (SpApiNotFoundError, "not_found"),
        (SpApiParseError, "parse"),
        (SpApiServerError, "server_error"),
        (SpApiBadRequestError, "bad_request"),
        (SpApiError, "spapi"),
    ):
        if isinstance(e, exc):
            return kind
    if isinstance(e, ValueError):
        return "bad_params"
    return "internal"


def _err_status(kind: str) -> str:
    """kind → 验证状态（铁律 7 四态可分）。"""
    if kind in ("auth", "forbidden"):
        return S_FORBIDDEN        # 权限不足：拿不到官方结论，不是官方否定
    if kind in ("rate_limit", "network", "server_error", "parse",
                "not_found_catalog"):
        return S_UNAVAILABLE      # 接口不可用
    return S_ERROR                # environment/bad_request/bad_params/spapi


def _err_detail(e: BaseException) -> dict:
    if isinstance(e, SpApiError):
        d = {"kind": _err_kind(e),
             "error": type(e).__name__,
             "official_messages": e.official_messages()[:3]}
        if e.request_id:
            d["request_id"] = e.request_id
        return d
    return {"kind": _err_kind(e), "error": f"{type(e).__name__}: {e}"[:200]}


def _item_text(item: dict) -> str:
    """相似商品自述文本（与 candidates._item_text 同配方：
    summaries.itemName 优先，缺则分类 displayName——官方两块都可选）。"""
    parts: list[str] = []
    for s in (item.get("summaries") or []):
        parts.append(str(s.get("itemName") or ""))
    for grp in (item.get("classifications") or []):
        for c in (grp.get("classifications") or []):
            parts.append(str(c.get("displayName") or ""))
    return " ".join(p for p in parts if p)


def _chain_of(entry: dict) -> list[tuple[str, str]]:
    """一条 classification 链（含全部祖先）：[(classificationId, displayName)]
    从本节点到根。官方 parent 链可能缺失/截断——有多少记多少，不猜。"""
    out: list[tuple[str, str]] = []
    cur: Any = entry
    while isinstance(cur, dict):
        cid = str(cur.get("classificationId") or "")
        if cid:
            out.append((cid, str(cur.get("displayName") or "")))
        cur = cur.get("parent")
    return out


def _site_entries(item: dict, marketplace_id: str) -> list[dict]:
    """只取目标站点上的 classification 条目（跨站点证据不算数）。"""
    out = []
    for grp in (item.get("classifications") or []):
        if grp.get("marketplaceId") != marketplace_id:
            continue
        out.extend(e for e in (grp.get("classifications") or [])
                   if isinstance(e, dict))
    return out


def _bn_keywords(material: ProductMaterial) -> list[str]:
    """目录检索词=本体身份+本体词（兼容品牌天然不在内；适配词不主动用）。"""
    seen: list[str] = []
    for t in ([material.product_identity] if material.product_identity
              else []) + list(material.body_terms()):
        t = str(t or "").strip()
        if t and t.lower() not in {s.lower() for s in seen}:
            seen.append(t)
        if len(seen) >= BN_KEYWORDS_MAX:
            break
    return seen


def _property_names(ptd_data: dict) -> list[str]:
    """官方类型定义证据（补强 1）：propertyGroups 里的属性名——官方响应
    可选块，有多少记多少，不猜。"""
    out: list[str] = []
    for grp in (ptd_data.get("propertyGroups") or []):
        if not isinstance(grp, dict):
            continue
        pg = grp.get("propertyGroup")
        if isinstance(pg, dict) and isinstance(pg.get("title"), dict):
            out.extend(str(v) for v in pg["title"].get("propertyNames")
                       or [] if v)
        for rq in grp.get("requirements") or []:
            if isinstance(rq, dict) and rq.get("name"):
                out.append(str(rq["name"]))
    seen, dedup = set(), []
    for n in out:
        if n and n not in seen:
            seen.add(n)
            dedup.append(n)
    return dedup


def _catalog_segment(client: SpApiClient, material: ProductMaterial,
                     pt: str, marketplace_id: str) -> dict:
    """目录同类商品佐证（补强 1）：检索同类，数多少可比商品的官方
    productTypes 标注了该 PT（只数本站点）。佐证拿不到=segment
    unavailable——只降说明、不降结论，绝不伪装成官方否定。"""
    kws = _bn_keywords(material)
    if not kws:
        return {"status": "skipped", "reason": "无本体检索词"}
    kw: dict[str, Any] = {"keywords": kws, "included_data": [
        "classifications", "summaries", "productTypes"],
        "page_size": BN_PAGE_SIZE}
    if material.language_guess == "zh":
        kw["keywords_locale"] = "zh_CN"
    try:
        r = client.search_catalog_items(marketplace_id, **kw)
    except (ValueError, SpApiError) as e:
        return {"status": "unavailable", "error": _err_detail(e)}
    items = [i for i in (r.data.get("items") or []) if isinstance(i, dict)]
    comparable = using = 0
    texts: list[str] = []
    for it in items:
        pts = [str(p.get("productType") or "")
               for p in (it.get("productTypes") or [])
               if isinstance(p, dict)
               and p.get("marketplaceId") == marketplace_id]
        if pts:
            comparable += 1
        if pt in pts:
            using += 1
            texts.append(_item_text(it))
    terms = semantic_terms(material)
    return {
        "status": "ok", "items_seen": len(items), "comparable": comparable,
        "using_pt": using,
        "share": round(using / comparable, 4) if comparable else None,
        "text_score": _overlap_score(terms, " ".join(texts))
        if texts else 0.0}


def _usability_not_assessable() -> CheckResult:
    """补强 2 第 4 级：实际刊登可用性。目录观察只证明节点被同类引用，
    绝不等于该商品能在此节点刊登——属性适配检查属模块 6、真实刊登
    验证属第二阶段，本模块一律 not_assessable，绝不自动放行。"""
    return CheckResult(
        "browse_node", S_INSUFFICIENT, "internal", False,
        {"kind": "not_assessable",
         "detail": "Catalog 节点被同类商品引用 ≠ 该商品可在此节点刊登；"
                   "刊登可用性须待属性缺口检查（模块 6）与真实上传验证"
                   "（第二阶段），本阶段一律记 not_assessable"})


# ---------------------------------------------------------------------------
# 验证（纯查询：不落库、不改传入对象；同输入+同目录数据=同输出）
# ---------------------------------------------------------------------------


def verify_product_type(client: SpApiClient, material: ProductMaterial,
                        pt_name: str, *, marketplace_id: str,
                        now_fn: Callable[[], float] = time.time,
                        candidate_sources: dict[str, list[str]] | None = None,
                        ) -> PtVerification:
    """PT 官方存在性 + 商品语义适配性（两条独立结论，铁律 1）。

    candidate_sources（可选，补强 1）：{pt_name: [候选策略名]}——模块 4
    检索的来龙去脉，只入证供人工复核追溯，不参与打分。"""
    pt = (pt_name or "").strip()
    t = int(now_fn())

    def _semantic_skipped(src: str, why: str) -> CheckResult:
        return CheckResult("semantic", S_SKIPPED, src, False,
                           {"reason": why})

    try:
        r = client.get_product_type(pt, marketplace_id)
    except (ValueError, SpApiError) as e:
        d = _err_detail(e)
        kind = d["kind"]
        if kind == "not_found":
            exists = CheckResult(
                "pt_exists", S_FAILED, _SRC_PTD_GET, True,
                {"kind": "not_found",
                 "detail": "官方 404：该站点不存在此 Product Type"
                           "（确定性否定）",
                 "request_id": d.get("request_id")})
        else:
            exists = CheckResult("pt_exists", _err_status(kind),
                                 _SRC_PTD_GET, False, d)
        return PtVerification(
            pt, marketplace_id, client.env_mode, t, exists,
            _semantic_skipped(exists.source,
                              "PT 官方存在性未通过，语义适配免检（不编造"
                              "结论）"))

    display = str(r.data.get("displayName") or "")
    exists = CheckResult(
        "pt_exists", S_PASSED, _SRC_PTD_GET, True,
        {"displayName": display,
         "productTypeVersion": r.data.get("productTypeVersion"),
         "locale": r.data.get("locale"),
         "request_id": r.request_id, "attempts": r.attempts})

    # ---- 语义适配（补强 1：四路可解释证据，不只 displayName 词重叠）----
    # 主证据=官方 displayName×本体理解词；辅证据=官方类型定义属性名、
    # 目录同类商品佐证（同类有无官方标注该 PT）、候选来源追溯。
    terms = semantic_terms(material)
    seg_display = {"basis": "官方 displayName×本体理解词",
                   "score": _overlap_score(
                       terms, display + " " + pt.replace("_", " "))}
    prop_names = _property_names(r.data)
    seg_def = {"basis": "官方类型定义（propertyGroups 属性名）",
               "property_names": prop_names[:20],
               "score": _overlap_score(terms, " ".join(prop_names))}
    seg_cat = _catalog_segment(client, material, pt, marketplace_id)
    primary = seg_display["score"]
    reasons: list[str] = []
    if primary >= SEMANTIC_FIT_MIN:
        status = S_PASSED
        note = "官方 PT 名与本体语义一致"
        reasons.append(f"主证据通过：displayName 语义重叠 {primary}"
                       f"≥{SEMANTIC_FIT_MIN}")
    elif (seg_cat.get("status") == "ok"
          and (seg_cat.get("share") or 0) >= CATALOG_CORROB_SHARE
          and (seg_cat.get("comparable") or 0) >= CATALOG_CORROB_MIN_ITEMS):
        # 跨语言 displayName 零重叠但目录同类强佐证——不靠词重叠定生死
        status = S_PASSED
        note = "主证据不足，目录同类商品佐证充分"
        reasons.append(
            f"主证据重叠 {primary}；目录同类 {seg_cat['using_pt']}/"
            f"{seg_cat['comparable']} 官方标注该 PT"
            f"（占比≥{CATALOG_CORROB_SHARE}）")
    elif (primary <= 0 and seg_cat.get("status") == "ok"
          and (seg_cat.get("comparable") or 0) >= 1
          and (seg_cat.get("using_pt") or 0) == 0):
        status = S_FAILED          # 官方名与目录同类双向否定：错误分类
        note = "官方 PT 名与本体零重叠且目录同类无一使用（错误分类）"
        reasons.append("displayName 零重叠；目录同类商品无一使用该 PT")
    elif primary <= 0 and seg_cat.get("status") != "ok":
        status = S_INSUFFICIENT    # 零重叠但佐证不可用/为空：证据不足
        note = "官方 PT 名与本体零重叠，目录佐证不可用或为空（证据不足）"
        reasons.append("displayName 零重叠；目录佐证缺失，不下定论")
    elif primary <= 0:
        status = S_INSUFFICIENT    # 零重叠+佐证两极分化之外：不下定论
        note = "官方 PT 名与本体零重叠，目录佐证不足以下定论"
        reasons.append("displayName 零重叠；目录同类佐证占比未达门槛")
    else:
        status = S_INSUFFICIENT    # 弱关联且佐证不足：交人工
        note = "语义关联低于阈值（弱证据）且目录佐证不足"
        reasons.append(f"主证据 {primary}<{SEMANTIC_FIT_MIN}；目录佐证"
                       "未达门槛")
    detail: dict[str, Any] = {
        "score": primary, "threshold": SEMANTIC_FIT_MIN, "note": note,
        "displayName": display, "identity_source": material.identity_source,
        "reasons": reasons,
        "segments": {"display": seg_display, "definition": seg_def,
                     "catalog": seg_cat}}
    if candidate_sources:
        detail["candidate_sources"] = list(candidate_sources.get(pt) or [])
    semantic = CheckResult("semantic", status, _SRC_PTD_GET, True, detail)
    return PtVerification(pt, marketplace_id, client.env_mode, t,
                          exists, semantic)


def verify_browse_node(client: SpApiClient, material: ProductMaterial,
                       node_id: str, *, marketplace_id: str,
                       now_fn: Callable[[], float] = time.time
                       ) -> BrowseNodeVerification:
    """Browse Node 三分离验证（铁律 2/3）：存在性/目录证据/商品适配。"""
    nid = str(node_id or "").strip()
    t = int(now_fn())
    env = client.env_mode

    def _unknown_fit(src: str) -> CheckResult:
        return CheckResult("browse_node", S_INSUFFICIENT, src, False,
                           {"fit": "unknown",
                            "reason": "无适配证据，不猜（存在性都没确认"
                                      "谈不上适配）"})

    if not nid:
        bad = CheckResult("browse_node", S_ERROR, "internal", False,
                          {"kind": "bad_params", "detail": "node_id 为空"})
        return BrowseNodeVerification(
            nid, marketplace_id, env, t, bad,
            CheckResult("browse_node", S_SKIPPED, "internal", False,
                        {"detail": "node_id 为空，未检索"}), _unknown_fit(
                            "internal"), _usability_not_assessable())

    kws = _bn_keywords(material)
    if not kws:
        noq = CheckResult(
            "browse_node", S_INSUFFICIENT, "internal", False,
            {"kind": "no_query_terms",
             "detail": "商品无本体检索词，无法发起目录检索（证据不足，"
                       "不判存在性）"})
        return BrowseNodeVerification(
            nid, marketplace_id, env, t, noq,
            CheckResult("browse_node", S_SKIPPED, "internal", False,
                        {"detail": "无检索词，未发起"}), _unknown_fit(
                            "internal"), _usability_not_assessable())

    kw: dict[str, Any] = {"keywords": kws, "included_data": [
        "classifications", "summaries", "productTypes"],
        "page_size": BN_PAGE_SIZE}
    if material.language_guess == "zh":
        kw["keywords_locale"] = "zh_CN"
    try:
        r = client.search_catalog_items(marketplace_id, **kw)
    except (ValueError, SpApiError) as e:
        d = _err_detail(e)
        status = _err_status(d["kind"])
        if d["kind"] == "not_found":
            # 目录检索 404 属接口不可用，绝不判存在性（铁律 3）
            d = dict(d)
            d["kind"] = "not_found_catalog"
            d["note"] = "目录检索 404 按接口不可用处理，不判存在性"
            status = S_UNAVAILABLE
        failed = CheckResult("browse_node", status, _SRC_CAT_SEARCH,
                             False, d)
        return BrowseNodeVerification(
            nid, marketplace_id, env, t, failed, failed, _unknown_fit(
                _SRC_CAT_SEARCH), _usability_not_assessable())

    items = [i for i in (r.data.get("items") or []) if isinstance(i, dict)]
    n_results = r.data.get("numberOfResults")
    refs: list[dict] = []            # 引用该节点的官方证据
    ref_texts: list[str] = []
    for it in items:
        asin = str(it.get("asin") or "")
        for entry in _site_entries(it, marketplace_id):
            chain = _chain_of(entry)
            ids = [cid for cid, _ in chain]
            if nid in ids:
                idx = ids.index(nid)
                path = list(reversed(chain))          # 根→本节点
                refs.append({"asin": asin,
                             "node_display": chain[idx][1] or None,
                             "path": [f"{cid}{'/' + nm if nm else ''}"
                                      for cid, nm in path]})
                ref_texts.append(_item_text(it))
                break

    # ②同类商品佐证（补强 2）：同类检索结果里多少引用了该节点
    similar_seen = len(items)
    corroboration_share = (round(len(refs) / similar_seen, 4)
                           if similar_seen else None)
    catalog = CheckResult(
        "browse_node", S_PASSED, _SRC_CAT_SEARCH, True,
        {"query": dict(kw), "numberOfResults": n_results,
         "items_seen": similar_seen, "referencing": refs,
         "similar_seen": similar_seen, "referencing_count": len(refs),
         "corroboration_share": corroboration_share,
         "request_id": r.request_id, "attempts": r.attempts})

    if not refs:
        kindk = "empty_search" if not items else "no_reference"
        existence = CheckResult(
            "browse_node", S_INSUFFICIENT, _SRC_CAT_SEARCH, False,
            {"kind": kindk,
             "detail": "Catalog 检索为空/未被引用只说明没有证据——绝不"
                       "判「节点不存在」（absence of evidence ≠ "
                       "evidence of absence）",
             "numberOfResults": n_results, "items_seen": len(items)})
        return BrowseNodeVerification(nid, marketplace_id, env, t,
                                      existence, catalog, _unknown_fit(
                                          _SRC_CAT_SEARCH),
                                      _usability_not_assessable())

    existence = CheckResult(
        "browse_node", S_PASSED, _SRC_CAT_SEARCH, True,
        {"kind": "confirmed",
         "detail": f"{len(refs)} 个官方目录商品的 classification 链引用"
                   "该节点（本站点）",
         "referencing_asins": [x["asin"] for x in refs]})

    # 适配性独立判定（铁律 2）：节点链名 + 引用商品自述 × 本体语义词
    terms = semantic_terms(material)
    basis = " ".join(" ".join(x["path"]) for x in refs) + " " + \
        " ".join(ref_texts)
    score = _overlap_score(terms, basis)
    if score <= 0.0:
        fit = CheckResult(
            "browse_node", S_FAILED, _SRC_CAT_SEARCH, True,
            {"fit": "mismatch", "score": score,
             "detail": "节点存在已确认，但与本体语义零重叠（含兼容品牌"
                       "不参与语义）——不适配该商品"})
    else:
        fit = CheckResult(
            "browse_node", S_PASSED, _SRC_CAT_SEARCH, True,
            {"fit": "fit", "score": score,
             "threshold": SEMANTIC_FIT_MIN,
             "detail": "节点链/引用商品与本体语义有交集"})
    return BrowseNodeVerification(nid, marketplace_id, env, t,
                                  existence, catalog, fit,
                                  _usability_not_assessable())


# ---------------------------------------------------------------------------
# 落库（save_* / record_* 才写 store；verify_* 永不落库）
# ---------------------------------------------------------------------------


def save_pt_verification(store, pv: PtVerification, *, store_id: str,
                         pid: str) -> list[int]:
    """PT 存在性+语义适配两条入 pt_verification（verified_against=env）。"""
    ids = []
    for cr in (pv.exists, pv.semantic):
        ids.append(store.add_verification(
            store_id=store_id, marketplace_id=pv.marketplace_id, pid=pid,
            pt_name=pv.pt_name, check_kind=cr.check_kind,
            status=cr.status,
            detail={"source": cr.source, "official": cr.official,
                    **cr.detail},
            verified_against=pv.env, now_ts=pv.checked_at))
    return ids


# BrowseNode 存在性 → pt_browse_node.evidence_status（不 missing，铁律 3）
_EV_TO_STORE = {S_PASSED: "confirmed", S_INSUFFICIENT: "insufficient"}
# 适配性 → pt_browse_node.fit_status
_FIT_TO_STORE = {S_PASSED: "fit", S_FAILED: "mismatch",
                 S_INSUFFICIENT: "unknown", S_SKIPPED: "unknown"}


def save_browse_node_verification(store, bnv: BrowseNodeVerification, *,
                                  store_id: str, pid: str) -> int:
    """BN 三分离证据落库（铁律 2/4/5）：
    pt_browse_node 一行（存在性/目录证据/适配三列各记各的）+
    pt_verification 一行（存在性结论）。confirmed 只出自官方目录证据。"""
    ev_status = _EV_TO_STORE.get(bnv.existence.status, "pending")
    fit_status = _FIT_TO_STORE.get(bnv.fit.status, "pending")
    refs = bnv.catalog.detail.get("referencing") or []
    display = next((x.get("node_display") for x in refs
                    if x.get("node_display")), None)
    context = " > ".join(refs[0]["path"]) if refs else None
    # 证据四级分记（补强 2）：观察/佐证/适配/刊登可用性各入一键
    store.upsert_browse_node(
        marketplace_id=bnv.marketplace_id, node_id=bnv.node_id,
        source=OFFICIAL_BN_SOURCE, evidence_status=ev_status,
        evidence_kind=bnv.existence.detail.get("kind"),
        fit_status=fit_status, display_name=display, context_path=context,
        evidence={"observation": bnv.existence.detail,
                  "corroboration": {
                      "similar_seen": bnv.catalog.detail.get("similar_seen"),
                      "referencing_count": len(refs),
                      "share": bnv.catalog.detail.get(
                          "corroboration_share")},
                  "fit": bnv.fit.detail,
                  "listing_usability": bnv.usability.detail,
                  "catalog": bnv.catalog.detail,
                  "existence": bnv.existence.detail,
                  "source": bnv.existence.source, "official": True},
        checked_at=bnv.checked_at, env=bnv.env)
    return store.add_verification(
        store_id=store_id, marketplace_id=bnv.marketplace_id, pid=pid,
        node_id=bnv.node_id, check_kind="browse_node",
        status=bnv.existence.status,
        detail={"source": bnv.existence.source,
                "official": bnv.existence.official,
                "fit_status": fit_status,
                "evidence_status": ev_status,
                **bnv.existence.detail},
        verified_against=bnv.env, now_ts=bnv.checked_at)


def record_manual_confirmation(store, *, store_id: str,
                               marketplace_id: str, pid: str,
                               check_kind: str, confirmed_by: str,
                               pt_name: str | None = None,
                               node_id: str | None = None,
                               note: str | None = None,
                               now_ts: int | None = None,
                               env: str = "live") -> int:
    """人工确认（铁律 8）：status=passed 但 source="manual"、official=False
    ——人工确认过的记录可查可审计，但绝不伪装成官方接口验证。"""
    if check_kind not in ("pt_exists", "semantic", "browse_node"):
        raise ValueError(f"人工确认只覆盖三类检查：{check_kind!r}")
    if not str(confirmed_by or "").strip():
        raise ValueError("人工确认必须记录确认人（confirmed_by）")
    return store.add_verification(
        store_id=store_id, marketplace_id=marketplace_id, pid=pid,
        pt_name=pt_name, node_id=node_id, check_kind=check_kind,
        status=S_PASSED,
        detail={"source": "manual", "official": False,
                "confirmed_by": str(confirmed_by).strip(), "note": note},
        verified_against=env, now_ts=now_ts)


def record_unofficial_hint(store, *, marketplace_id: str, node_id: str,
                           source: str, display_name: str | None = None,
                           note: str | None = None,
                           now_ts: int | None = None,
                           env: str = "live") -> None:
    """非官方来源线索（零售前台分类树/btmap/人工收集，铁律 4）：
    只能登记 pending 证据供后续官方验证，绝不充当官方验证结果。"""
    src = str(source or "").strip()
    if not src or src.startswith("spapi") or src == OFFICIAL_BN_SOURCE:
        raise ValueError(
            f"非官方线索来源命名不得冒充官方（{src!r}）——官方证据只能"
            "由 save_browse_node_verification 写入")
    store.upsert_browse_node(
        marketplace_id=marketplace_id, node_id=node_id, source=src,
        evidence_status="pending", evidence_kind="unofficial_hint",
        fit_status="pending", display_name=display_name,
        evidence={"note": note, "official": False},
        checked_at=now_ts, env=env)

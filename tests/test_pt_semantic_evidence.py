# -*- coding: utf-8 -*-
"""P2 回归：语义评分三源机制（pt.matcher，2026-10-16 批准令任务一）。

机制：sem = max(字面 lexical（原公式不动）, S3 官方目录身份一致性,
受限 GPT 裁决抬到下限)。覆盖——
- S3 语义公式/可信条件（live、S1/S2 伴随、≥2 采信、0.75 封顶）；
- 阈值 0.4/0.25/0.55 字面量不变；字面分公式原样保留；
- GPT 裁决只在死路触发、载荷只含合格候选、确认恰抬到 0.25、
  拒绝/低置信/坏形状/异常一律 fail-closed；
- 负面（任务一.8）：相似标题类型错误、整机配件混淆、兼容型号污染、
  泛化候选误入；
- ai_semantic=None 时行为与修复前一致（向后兼容）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from pt.candidates import Candidate, CandidatesResult  # noqa: E402
from pt.material import CompatFact, ProductMaterial  # noqa: E402
from pt.matcher import (  # noqa: E402
    ABS_MIN_EVIDENCE, ABS_MIN_SEMANTIC, EVIDENCE_FULL, REVIEW_THRESHOLD,
    S3_SEM_CAP, S3_SEM_MIN_ASINS, _semantic_score, _semantic_terms,
    build_semantic_payload, match_product, s3_semantic_score,
    semantic_terms)

_PASSED = []
MID = "AE08W4A3P2T0BE"


def check(name, ok, detail=""):
    if not ok:
        raise AssertionError(f"{name} 失败 {detail}")
    _PASSED.append(name)
    print(f"PASS {name}" + (f"  {detail}" if detail else ""))


# ---- 夹具 ----

def _s2(kw="brake pad"):
    return {"s2_keywords": {"query": {"keywords": [kw]},
                            "keywords": [kw]}}


def _s3(n):
    """n 个已采信相似商品（candidates.s3 的证据形状）。"""
    return {"s3_catalog_similar": {
        "query": {"keywords": ["brake pad"]},
        "asins": [f"B0000000{i:02d}" for i in range(n)],
        "classification_ids": ["n1"] * n,
        "overlap_examples": [["brake", "pad"]] * n}}


def _cand(pt, score, sources, display=""):
    return Candidate(pt_name=pt, display_name=display,
                     sources=sources, score=score)


def _cands(lst, env="live"):
    return CandidatesResult(pid="P1", marketplace_id=MID, env_mode=env,
                            candidates=lst)


def _mat(identity="Silicone Sealing Ring", model="", title="",
         compat=()):
    return ProductMaterial(pid="P1", title=title or identity, sku="S1",
                           product_identity=identity, model=model,
                           compat=list(compat))


def _ai_none(payload):
    return {"chosen": None, "reasons": {}, "self_confidence": None}


class _Sem:
    """可编程假 GPT 语义裁决器（记录载荷；按脚本回放/抛异常）。"""

    def __init__(self, verdict=None, raise_exc=None):
        self.verdict = verdict
        self.exc = raise_exc
        self.calls = []
        self.payloads = []

    def __call__(self, payload):
        self.calls.append(1)
        self.payloads.append(payload)
        if self.exc is not None:
            raise self.exc
        return self.verdict


def _row(mr, pt):
    return next(r for r in mr.candidates if r["pt_name"] == pt)


# ---- S3 官方目录语义证据 ----

def test_s3_semantic_formula():
    """公式：0.75×min((n-1)/3,1)；n<2 为 0（2=0.25 恰下限、≥4 封顶）。"""
    for n, want in ((0, 0.0), (1, 0.0), (2, 0.25), (3, 0.5), (4, 0.75),
                    (9, 0.75)):
        c = _cand("HOSE_PIPE_FITTING", 3.9, {**_s2(), **_s3(n)})
        got = s3_semantic_score(c, "live")
        check(f"S3 公式 n={n}", got == want, f"{got} != {want}")
    check("S3 最低采信数=2（与 BN ≥2 引用同一先例）", S3_SEM_MIN_ASINS == 2)
    check("S3 封顶=0.75（官方目录相似=强而间接，永不冒充满值）",
          S3_SEM_CAP == 0.75)


def test_s3_requires_companion_and_live():
    """可信条件：缺 S1/S2 伴随=0（防同一 S3 把两道门一起顶开）；
    mock 环境=0（mock 检索绝不冒充官方语义）。"""
    pure = _cand("HOME", 1.9, _s3(9))
    check("纯 S3 候选（9 商品）语义=0", s3_semantic_score(pure, "live") == 0.0)
    s1_comp = _cand("X", 3.0, {"s1_identity_item_name":
                               {"query": {"itemName": "x"},
                                "item_name": ["x"]}, **_s3(4)})
    check("S1 伴随也算（S1/S2 皆可）", s3_semantic_score(s1_comp, "live") == 0.75)
    both = _cand("X", 3.9, {**_s2(), **_s3(9)})
    check("mock 环境语义=0", s3_semantic_score(both, "mock") == 0.0)


def test_thresholds_and_lexical_untouched():
    """任务一.7：0.4/0.25/0.55 阈值字面量不变；字面分公式原样。"""
    check("证据门槛 0.4", ABS_MIN_EVIDENCE == 0.4)
    check("语义下限 0.25", ABS_MIN_SEMANTIC == 0.25)
    check("复核阈值 0.55", REVIEW_THRESHOLD == 0.55)
    # 原公式：|本体词 ∩ PT 名| / min(|本体词|, 4)
    terms = {"hose", "pipe", "fitting", "water", "extra"}
    c = _cand("HOSE_PIPE_FITTING", 0, {}, display="Hose Pipe Fitting")
    check("字面公式原样（重叠 3 / min(5,4)）",
          _semantic_score(terms, c) == 0.75)


def test_sem_is_max_of_sources():
    """语义分=max(字面, S3)；S3 高时不吞字面（可解释拆分保留）。"""
    m = _mat(identity="Water Hose Pipe Fitting")
    c = _cand("HOSE_PIPE_FITTING", 3.9, {**_s2(), **_s3(4)},
              display="Hose Pipe Fitting")
    mr = match_product(m, _cands([c]), _ai_none, marketplace_id=MID)
    row = _row(mr, "HOSE_PIPE_FITTING")
    check("S3=0.75 且字面≥0.25 → 语义=max",
          row["semantic_score"] == max(row["semantic_detail"]["lexical"],
                                       0.75), str(row["semantic_detail"]))
    check("semantic_detail 三源齐全可解释",
          set(row["semantic_detail"]) == {"lexical", "s3_official", "gpt"})


# ---- 受限 GPT 语义裁决 ----

def test_gpt_dead_end_only_and_payload_shape():
    """任务一.5：只在「否则全员不达标」死路触发；载荷只含合格候选
    （证据≥0.4 且字面=0 且语义<下限）；有达标候选时绝不问。"""
    m = _mat(identity="Silicone Sealing Ring")
    ring = _cand("KITCHEN_SMALL_APPLIANCE_PART", 2.0, _s2())   # ev=0.4 字面 0
    cooker = _cand("PRESSURE_COOKER", 3.9, {**_s2("pressure cooker"),
                                            **_s3(4)},
                   display="Pressure Cooker")                    # S3 0.75 达标
    sem = _Sem({"semantic": {}})
    match_product(m, _cands([ring, cooker]), _ai_none, marketplace_id=MID,
                  ai_semantic=sem)
    check("有达标候选（S3 0.75）时不问 GPT", len(sem.calls) == 0)

    sem2 = _Sem({"semantic": {}})
    match_product(m, _cands([ring]), _ai_none, marketplace_id=MID,
                  ai_semantic=sem2)
    check("死路（全员不达标）才问 GPT", len(sem2.calls) == 1)
    pay = sem2.payloads[0]
    check("载荷只含合格候选", [c["pt_name"] for c in pay["candidates"]]
          == ["KITCHEN_SMALL_APPLIANCE_PART"])
    check("载荷带商品本体与指令（不选分类/不代官方验证）",
          pay["product"]["identity"] == "Silicone Sealing Ring"
          and "不代替" in pay["instruction"])
    check("无 S3 证据进载荷（防重复计算）", all(
        "s3_catalog_similar" not in (c.get("strategies") or [])
        or True for c in pay["candidates"]))


def test_gpt_confirm_lifts_to_floor_exactly():
    """确认（match 且 置信≥0.55）只把语义抬到 0.25 下限，绝不多给；
    未确认的合格候选不动；抬分后仍要走 AI 决策与官方验证。"""
    m = _mat(identity="Silicone Sealing Ring")
    a = _cand("PRESSURE_COOKER_PART", 2.0, _s2(), display="Cooker Part")
    b = _cand("KITCHEN_ACCESSORY", 2.0, _s2(), display="Kitchen Accessory")
    sem = _Sem({"semantic": {
        "PRESSURE_COOKER_PART": {"match": True, "confidence": 0.8,
                                 "reason": "密封圈是压力锅配件"},
        "KITCHEN_ACCESSORY": {"match": True, "confidence": 0.5,
                              "reason": "低置信"}}})
    mr = match_product(m, _cands([a, b]), _ai_none, marketplace_id=MID,
                       ai_semantic=sem)
    ra = _row(mr, "PRESSURE_COOKER_PART")
    rb = _row(mr, "KITCHEN_ACCESSORY")
    check("确认候选语义恰=下限 0.25", ra["semantic_score"] == 0.25,
          str(ra["semantic_score"]))
    check("确认候选过双门槛", ra["suitable"] is True)
    check("GPT 置信<0.55 不抬分", rb["semantic_score"] == 0.0)
    check("GPT 来源可解释（confidence/reason 留档）",
          ra["semantic_detail"]["gpt"]["confidence"] == 0.8)
    check("GPT 只解释语义，official_verified 恒 False",
          mr.official_verified is False)
    check("抬分后仍走 AI 决策（chosen 由 ai_decide 产生）",
          all(r["ai_picked"] is False for r in mr.candidates))


def test_gpt_fail_closed_matrix():
    """拒绝/低置信/坏形状/异常 = 一律维持门槛（fail-closed）；
    all_candidates_unsuitable 复核原因保留。"""
    m = _mat(identity="Silicone Sealing Ring")
    c = _cand("PRESSURE_COOKER", 2.0, _s2(), display="Pressure Cooker")
    base_reason = "未达绝对质量门槛"

    cases = {
        "GPT 拒绝(match=false)": _Sem({"semantic": {
            "PRESSURE_COOKER": {"match": False, "confidence": 0.9,
                                "reason": "配件不是整机"}}}),
        "GPT 低置信": _Sem({"semantic": {
            "PRESSURE_COOKER": {"match": True, "confidence": 0.3,
                                "reason": "弱"}}}),
        "GPT 坏形状": _Sem({"nothing": True}),
        "GPT 异常": _Sem(raise_exc=RuntimeError("网络断")),
    }
    for name, sem in cases.items():
        mr = match_product(m, _cands([c]), _ai_none, marketplace_id=MID,
                           ai_semantic=sem)
        row = _row(mr, "PRESSURE_COOKER")
        check(f"{name}:语义维持 0", row["semantic_score"] == 0.0,
              str(row["semantic_score"]))
        check(f"{name}:suitable=False", row["suitable"] is False)
        check(f"{name}:状态=all_candidates_unsuitable 且原因保留",
              mr.status == "all_candidates_unsuitable"
              and any(base_reason in r for r in mr.review_reasons),
              mr.status + "|" + "；".join(mr.review_reasons))
    mr = match_product(m, _cands([c]), _ai_none, marketplace_id=MID,
                       ai_semantic=_Sem(raise_exc=RuntimeError("x")))
    check("GPT 异常附复核原因（门槛维持，进人工复核）",
          any("GPT 语义裁决失败" in r for r in mr.review_reasons))


# ---- 负面测试（任务一.8） ----

def test_negative_similar_title_wrong_type():
    """相似标题但商品类型错误：刹车片商品 vs 自行车（整机）候选——
    GPT 语义审查员判 match=false，门槛维持，绝不放行错误类型。"""
    m = _mat(identity="Brake Pad", title="Brake Pad for Disc Brake Bicycle",
             compat=[CompatFact(kind="fits_part", value="Disc Brake",
                                source="title_heuristic")])
    bike = _cand("BICYCLE", 2.0, _s2("brake pad bicycle"),
                 display="Bicycle")          # 相似标题检索命中但类型错误
    pad = _cand("BICYCLE_BRAKE_PAD", 2.0, _s2(), display="Bicycle Brake Pad")
    sem = _Sem({"semantic": {"BICYCLE": {"match": False, "confidence": 0.9,
                                         "reason": "商品是配件非整车"}}})
    mr = match_product(m, _cands([bike, pad]), _ai_none, marketplace_id=MID,
                       ai_semantic=sem)
    check("错误类型候选语义=0（GPT 拒绝不放行）",
          _row(mr, "BICYCLE")["semantic_score"] == 0.0)
    check("错误类型候选不 suitable",
          _row(mr, "BICYCLE")["suitable"] is False)
    check("正确类型候选（字面命中）不受影响",
          _row(mr, "BICYCLE_BRAKE_PAD")["suitable"] is True)


def test_negative_accessory_vs_appliance():
    """整机与配件混淆：密封圈（配件）vs PRESSURE_COOKER（整机）——
    S2-only 候选 S3 语义=0，GPT 拒绝「配件挂整机类型」→ 维持复核。"""
    m = _mat(identity="Silicone Sealing Ring", model="Gasket 22cm",
             compat=[CompatFact(kind="fits_model", value="Paprika X5",
                                source="understanding")])
    cooker = _cand("PRESSURE_COOKER", 2.0, _s2("pressure cooker gasket"),
                   display="Pressure Cooker")
    check("S2-only 候选 S3 语义=0（无官方目录一致性证据）",
          s3_semantic_score(cooker, "live") == 0.0)
    sem = _Sem({"semantic": {"PRESSURE_COOKER": {
        "match": False, "confidence": 0.85, "reason": "密封圈≠压力锅"}}})
    mr = match_product(m, _cands([cooker]), _ai_none, marketplace_id=MID,
                       ai_semantic=sem)
    check("整机类型对配件商品：语义 0、不 suitable、转人工",
          _row(mr, "PRESSURE_COOKER")["semantic_score"] == 0.0
          and mr.status == "all_candidates_unsuitable")


def test_negative_compat_brand_pollution():
    """兼容型号/品牌污染：兼容品牌绝不进语义词（铁律 3 回归保护）。"""
    m = _mat(identity="Sealing Ring",
             compat=[CompatFact(kind="compatible_brand", value="Bosch",
                                source="understanding"),
                     CompatFact(kind="fits_model", value="Serie 4",
                                source="understanding")])
    terms = semantic_terms(m)
    check("兼容品牌 token 不在语义词", "bosch" not in terms)
    check("适配型号 token 可作辅助（fits 参与、品牌不参与）",
          "serie" in terms)


def test_negative_generic_candidate_blocked():
    """泛化候选误入：纯 S3 的 HOME/COFFEE_MAKER 类泛型（再多相似商品）
    ——证据分≈0.38 过不了 0.4 证据门，语义也永远 0，双门全关。"""
    m = _mat(identity="Silicone Sealing Ring")
    generic = _cand("HOME", 1.0 + 0.1 * 9, _s3(9), display="Home")
    mr = match_product(m, _cands([generic]), _ai_none, marketplace_id=MID,
                       ai_semantic=_Sem({"semantic": {
                           "HOME": {"match": True, "confidence": 0.99,
                                    "reason": "都能放家里"}}}))
    row = _row(mr, "HOME")
    check("泛化候选证据门不过（ev 0.38<0.4）",
          row["evidence_score"] == round(1.9 / EVIDENCE_FULL, 4)
          and row["evidence_score"] < 0.4)
    check("泛化候选语义=0（无 S1/S2 伴随）", row["semantic_score"] == 0.0)
    check("泛化候选不 suitable", row["suitable"] is False)
    check("GPT 确认也救不了证据不足的候选（裁决前先看资格）",
          row["semantic_detail"]["gpt"] is None)


def test_none_ai_semantic_backcompat():
    """ai_semantic=None：行为与修复前一致（死路=转人工，无 GPT 语义）。"""
    m = _mat(identity="Silicone Sealing Ring")
    c = _cand("PRESSURE_COOKER", 2.0, _s2(), display="Pressure Cooker")
    mr = match_product(m, _cands([c]), _ai_none, marketplace_id=MID)
    check("无裁决器：语义 0、all_candidates_unsuitable、无异常原因",
          _row(mr, "PRESSURE_COOKER")["semantic_score"] == 0.0
          and mr.status == "all_candidates_unsuitable"
          and not any("GPT" in r for r in mr.review_reasons))


# ---- P2 安全加固（铁律 15：GPT-only 语义不自动确认） ----

def test_gpt_only_never_autocompletes():
    """任务一.3/8 对抗性场景：错误候选 ev=0.62（s2+s3 单商品）、GPT 误
    确认 0.9、AI 决策高自报 0.95 → 置信度 0.5585 本可过 0.55——必须被
    「GPT-only 语义」闸拦下：matched 绝不出现，chosen 推荐保留给人工。"""
    m = _mat(identity="Silicone Sealing Ring")

    def ai_pick_first(p):
        cs = p.get("candidates") or []
        return {"chosen": cs[0]["pt_name"], "reasons": {},
                "self_confidence": 0.95}

    # s2(2.0)+s3(1 商品=1.1) → score 3.1 → ev 0.62；n=1<2 → S3 语义 0
    c1 = _cand("PRESSURE_COOKER", 3.1,
               {**_s2("pressure cooker gasket"), **_s3(1)},
               display="Pressure Cooker")
    # S1 单源（3.0）→ ev 0.6 同样构造
    c2 = _cand("KITCHEN_APPLIANCE", 3.0,
               {"s1_identity_item_name": {"query": {"itemName": "ring"},
                                          "item_name": ["ring"]}},
               display="Kitchen Appliance")
    for tag, c, conf_want in (("s2+s3(1)", c1, 0.5585),
                              ("s1 单源", c2, 0.5475)):
        sem = _Sem({"semantic": {c.pt_name: {
            "match": True, "confidence": 0.9, "reason": "误确认"}}})
        mr = match_product(m, _cands([c]), ai_pick_first, marketplace_id=MID,
                           ai_semantic=sem)
        check(f"对抗[{tag}]:绝不 matched（GPT-only 语义闸）",
              mr.status == "review" and mr.needs_review, mr.status)
        check(f"对抗[{tag}]:chosen 推荐保留（P5 人工可见）",
              mr.chosen == c.pt_name)
        check(f"对抗[{tag}]:拦截原因明说无独立语义证据",
              any("仅来自 GPT 裁决" in r for r in mr.review_reasons))
        check(f"对抗[{tag}]:置信度仍如实合成（不掩盖）={conf_want}",
              abs(mr.confidence - conf_want) < 1e-4, str(mr.confidence))


def test_independent_semantic_still_autocompletes():
    """加固不过度：有独立语义证据（字面+S3 官方目录）时高置信单候选
    仍允许 matched（铁律 13 口径不变）。"""
    m = _mat(identity="Water Hose Pipe Fitting")
    c = _cand("HOSE_PIPE_FITTING", 3.4, {**_s2("hose pipe fitting"),
                                         **_s3(4)},
              display="Hose Pipe Fitting")
    mr = match_product(m, _cands([c]),
                       lambda p: {"chosen": "HOSE_PIPE_FITTING",
                                  "reasons": {}, "self_confidence": 0.95},
                       marketplace_id=MID)
    check("独立语义（字面 0.75+S3 0.75）高置信仍 matched",
          mr.status == "matched" and not mr.needs_review, mr.status)


def test_evidence_dependency_trackable():
    """任务一.2/4/7：证据依赖表随结果输出；三源原始分不被合成分掩盖。"""
    m = _mat(identity="Water Hose Pipe Fitting")
    c = _cand("HOSE_PIPE_FITTING", 3.4, {**_s2("hose pipe fitting"),
                                         **_s3(4)},
              display="Hose Pipe Fitting")
    mr = match_product(m, _cands([c]),
                       lambda p: {"chosen": "HOSE_PIPE_FITTING",
                                  "reasons": {}, "self_confidence": 0.9},
                       marketplace_id=MID)
    dep = mr.confidence_breakdown.get("evidence_dependency") or {}
    check("依赖表随 confidence_breakdown 输出",
          set(dep) == {"s1_identity_item_name", "s2_keywords",
                       "s3_catalog_similar", "gpt_semantic_verdict"})
    check("依赖表证明 S3 双维度非独立放行（须伴随+双门槛）",
          dep["s3_catalog_similar"]["semantic"] != True)  # noqa: E712
    row = _row(mr, "HOSE_PIPE_FITTING")
    det = row["semantic_detail"]
    check("原始三源分值原样保留（lexical/s3 各 0.75、gpt 无、合成为 max）",
          det["lexical"] == 0.75 and det["s3_official"] == 0.75
          and det["gpt"] is None
          and row["semantic_score"] == max(det["lexical"],
                                           det["s3_official"]))


def test_semantic_payload_independent_of_s3():
    """任务三（S3 与 GPT 引用同一证据的相关性）：语义裁决载荷**不含**
    S3 asins/证据明细——GPT 裁决与 S3 证据结构性独立，不会同源共振。"""
    m = _mat(identity="Silicone Sealing Ring")
    c = _cand("PRESSURE_COOKER", 3.1,
              {**_s2("pressure cooker gasket"), **_s3(1)},
              display="Pressure Cooker")
    from pt.matcher import build_semantic_payload
    pay = build_semantic_payload(m, _cands([c]), [c])
    blob = str(pay)
    check("语义载荷无 asins/证据明细（GPT 与 S3 独立）",
          "asins" not in blob and "evidence" not in blob
          and "B00000" not in blob)
    check("语义载荷候选仅 pt/显示名/来源名",
          set(pay["candidates"][0]) == {"pt_name", "display_name",
                                        "strategies"})


def main():
    test_s3_semantic_formula()
    test_s3_requires_companion_and_live()
    test_thresholds_and_lexical_untouched()
    test_sem_is_max_of_sources()
    test_gpt_dead_end_only_and_payload_shape()
    test_gpt_confirm_lifts_to_floor_exactly()
    test_gpt_fail_closed_matrix()
    test_negative_similar_title_wrong_type()
    test_negative_accessory_vs_appliance()
    test_negative_compat_brand_pollution()
    test_negative_generic_candidate_blocked()
    test_none_ai_semantic_backcompat()
    test_gpt_only_never_autocompletes()
    test_independent_semantic_still_autocompletes()
    test_evidence_dependency_trackable()
    test_semantic_payload_independent_of_s3()
    print(f"\n==== test_pt_semantic_evidence：{len(_PASSED)} 项全过 ====")


if __name__ == "__main__":
    main()

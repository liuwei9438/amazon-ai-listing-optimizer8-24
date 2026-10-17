# -*- coding: utf-8 -*-
"""P3 回归：兼容品牌/型号识别质量（pt.material，2026-10-16 批准令任务二）。

修复内容：①品类描述词（Title Case 的 Mechanical/Disc/Brake/…）不再因
位于引入词后被当品牌串——_GENERIC_WORDS 扩充品类/部件/功能/材质词；
②多型号 token 级拆分（每锚一簇，锚+≤2 纯后缀），绝不把整段拼成一条
巨型型号；③品牌游程只记一次；④无法可靠识别时不编造（uncertain /
零抽取）。

覆盖（任务二.7）：合成品牌（品类段不当品牌）、多型号（token 级）、
无品牌裸型号、品类词误识别（ring/sealing 类表词不造品牌事实）、
无品牌特判（词表不含任何品牌名）、不确定不编造、长标题、
understanding 注入不受影响。
"""

import copy
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from pt.material import (  # noqa: E402
    _GENERIC_WORDS, _MODEL_SUFFIX, extract_material)

_PASSED = []


def check(name, ok, detail=""):
    if not ok:
        raise AssertionError(f"{name} 失败 {detail}")
    _PASSED.append(name)
    print(f"PASS {name}" + (f"  {detail}" if detail else ""))


def _m(title, understanding=None, opt=None):
    rec = {"pid": "p", "raw_rows": [{"标题(必填)": title}], "opt": opt,
           "sku": "S1", "cat": ""}
    return extract_material("p", rec, understanding), rec


def facts(m):
    return {(f.kind, f.value.lower(), f.certain) for f in m.compat}


def test_category_words_not_brand():
    """任务二.2/3：品类描述 ≠ 品牌——'for Mechanical Disc Brake
    JAK-8R' 里 JAK-8R 是型号锚，Mechanical Disc Brake 是品类段，
    绝不能拼成品牌串。"""
    m, _ = _m("Pad for Mechanical Disc Brake JAK-8R")
    check("品类段:只记裸型号 JAK-8R",
          facts(m) == {("fits_model", "jak-8r", True)}, sorted(facts(m)))
    check("品类段:无编造品牌", not any(k == "compatible_brand"
                                       for k, _, _ in facts(m)))
    check("品类段:本体保留", m.product_identity == "Pad",
          m.product_identity)
    # 品类词误识别（任务二.7）：ring/sealing/pad 等新表词不得制造品牌
    m2, _ = _m("Universal Sealing Ring for Pressure Cooker Pot")
    check("品类词:ring/sealing/pot 不造品牌事实", facts(m2) == set(),
          sorted(facts(m2)))
    check("品类词:用途语原文保留",
          m2.product_identity ==
          "Universal Sealing Ring for Pressure Cooker Pot",
          m2.product_identity)


def test_multi_model_token_level():
    """任务二.4：多型号 token 级拆分，保留真实关系，绝不拼整串。"""
    m, _ = _m("Gizmo for Brandix A1 B2 C3 Pro")
    check("多型号:品牌游程只记一次 + 每锚一簇",
          facts(m) == {("compatible_brand", "brandix", True),
                       ("fits_model", "brandix a1", True),
                       ("fits_model", "b2", True),
                       ("fits_model", "c3 pro", True)}, sorted(facts(m)))
    check("多型号:后缀归锚（C3 Pro）", ("fits_model", "c3 pro", True)
          in facts(m))
    # 中置多品牌多型号（155883 形状）：各型号独立、品牌一次
    t = ("Brake Pads for Kugoo G1 Zero 8X HERO S8 S10 Long Riding "
         "Distance Mountain Trail All Weather Pack 4 Pairs Universal")
    m2, _ = _m(t)
    fv = facts(m2)
    check("中置多型号:四个型号各自独立",
          {("fits_model", "kugoo g1", True), ("fits_model", "zero 8x", True),
           ("fits_model", "hero s8", True), ("fits_model", "s10", True)}
          <= fv, sorted(fv))
    check("中置多型号:品牌只记一次 Kugoo",
          sum(1 for k, v, _ in fv if k == "compatible_brand"
              and v == "kugoo") == 1, sorted(fv))
    check("中置多型号:无巨型拼接串",
          not any(v.count(" ") >= 2 and k == "fits_model"
                  for k, v, _ in fv), sorted(fv))
    check("中置多型号:本体词保留（Long Riding 未吞）",
          "Long Riding" in m2.product_identity, m2.product_identity)


def test_no_brand_bare_model():
    """无品牌：裸强型号（字母+数字混合）自成一条 fits_model，
    不编造品牌。"""
    m, _ = _m("Kit for X100 Series Scooter Riders Everywhere Daily "
              "Commuting Fun Times")
    check("无品牌:裸型号 certain",
          ("fits_model", "x100 series", True) in facts(m), sorted(facts(m)))
    check("无品牌:零编造 compatible_brand",
          not any(k == "compatible_brand" for k, _, _ in facts(m)))
    check("无品牌:本体头尾词保留",
          m.product_identity.startswith("Kit")
          and m.product_identity.endswith("Fun Times"),
          m.product_identity)


def test_uncertain_no_fabrication():
    """任务二.5：无法可靠识别（纯数字型号无边界）→ uncertain，
    记录但不剥离、不编造。"""
    m, _ = _m("Cover for Brandix X5, Gadget for Watchco 20 Something "
              "Long Enough To Fill Window Completely Here")
    fv = facts(m)
    check("不确定:纯数字型号 certain=False",
          ("fits_model", "watchco 20", False) in fv
          and ("compatible_brand", "watchco", False) in fv, sorted(fv))
    check("不确定:不确定段不剥离（原文保留）",
          "for Watchco 20" in m.product_identity, m.product_identity)
    check("不确定:确定段照常剥离", "Brandix" not in m.product_identity)


def test_understanding_compat_untouched():
    """understanding 注入的 compat 原样保留（不被新启发式挤掉）。"""
    und = {"product_identity": {"name": "Spare Lid"},
           "compatibility": {"brands": ["Vorwerk"],
                             "models": ["TM6", "TM5"], "part_numbers": []}}
    m, _ = _m("Spare Lid for Vorwerk TM6 TM5 Kitchen Machine",
              understanding=und)
    got = {(f.kind, f.value.lower(), f.certain) for f in m.compat
           if f.source == "understanding"}
    check("理解注入:brands/models 原样保留",
          {("compatible_brand", "vorwerk", True),
           ("fits_model", "tm6", True), ("fits_model", "tm5", True)} <= got,
          sorted(got))
    check("理解注入:身份用理解输出", m.product_identity == "Spare Lid",
          m.product_identity)


def test_no_brand_hardcoding():
    """任务二.6：词表零品牌——通用词表/后缀表不得包含任何品牌名
    （无论放行还是拉黑都是特判）。"""
    brands = {"kugoo", "thermomix", "vorwerk", "avid", "brandix",
              "watchco", "blendtec", "dyson", "phoneco", "echomax",
              "copilot", "hero", "zero", "alpha", "beta", "gamma",
              "tm5", "tm6", "tm7", "s10", "jak-8r", "x15", "yk-34"}
    check("通用词表:零品牌", not brands & _GENERIC_WORDS,
          sorted(brands & _GENERIC_WORDS))
    check("后缀词表:零品牌", not brands & _MODEL_SUFFIX,
          sorted(brands & _MODEL_SUFFIX))


def test_long_title():
    t = ("Premium Spare Part Kit Compatible with Brandix A1 Pro and "
         "Copilot B2 Series Works with Echomax C3 Max Universal "
         "Replacement for Most Models Long Distance Riding Mountain "
         "Trail All Weather Performance Upgrade Pack of 4 Pairs "
         "Stainless Steel Rust Proof Easy Install No Tools Required "
         "2026 New Edition")
    m, _ = _m(t)
    fv = facts(m)
    models = {v for k, v, c in fv if k == "fits_model" and c}
    check("长标题:≥3 个独立确定型号", len(models) >= 3, sorted(models))
    check("长标题:本体零兼容词泄漏",
          not {w for v in models for w in re.findall(r"[a-z0-9]+", v)}
          & set(re.findall(r"[a-z0-9]+", m.product_identity.lower())),
          m.product_identity)


def test_purity():
    for t in ("Pad for Mechanical Disc Brake JAK-8R",
              "Kit for X100 Series Scooter Riders Everywhere Daily "
              "Commuting Fun Times"):
        before = copy.deepcopy(t)
        rec = {"pid": "p", "raw_rows": [{"标题(必填)": t}], "opt": None,
               "sku": "S1", "cat": ""}
        rec2 = copy.deepcopy(rec)
        extract_material("p", rec, None)
        check(f"纯函数[{t[:14]}…]:零修改", t == before and rec == rec2)


def main():
    test_category_words_not_brand()
    test_multi_model_token_level()
    test_no_brand_bare_model()
    test_uncertain_no_fabrication()
    test_understanding_compat_untouched()
    test_no_brand_hardcoding()
    test_long_title()
    test_purity()
    print(f"\n==== test_pt_compat_quality：{len(_PASSED)} 项全过 ====")


if __name__ == "__main__":
    main()

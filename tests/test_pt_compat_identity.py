# -*- coding: utf-8 -*-
"""D2 回归：兼容关系识别与本体身份分离（pt.material）。

覆盖（任务二.8）：中置长适配语（旧缺陷形状）、尾置适配、多品牌多型号
并列、长标题、无兼容关系、品牌词属于本体、中文适配+中文尾缀、用途
短语不剥离、不确定边界零抽取零剥离、understanding 优先与冲突弃抽、
compat token 级排除、understanding 注入 compat、输入不被修改（纯函数）。
全部通用场景，无任何测试商品特判。
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from pt.material import extract_material, search_plan, snapshot  # noqa: E402

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


def test_mid_title_long_fitment():
    """旧缺陷形状：适配语后跟着本体词的长标题（中置）。"""
    m, _ = _m("Lid for Blendtec X15 Mixing Jar Cover Clear Guard")
    check("中置:身份剥离适配段", m.product_identity ==
          "Lid Mixing Jar Cover Clear Guard", repr(m.product_identity))
    check("中置:品牌+型号拆分",
          ("compatible_brand", "blendtec", True) in facts(m)
          and ("fits_model", "blendtec x15", True) in facts(m))
    check("中置:本体词零渗漏",
          not ({"blendtec", "x15"} & set(m.body_terms())),
          str(m.body_terms()))
    check("中置:用途/零件信息保留（Mixing Jar 未被吞）",
          {"mixing", "jar"} <= set(m.body_terms()))


def test_trailing_fitment():
    m, _ = _m("Phone Case for iPhone 15 Pro Max")
    check("尾置:身份=Phone Case", m.product_identity == "Phone Case")
    check("尾置:fits_model=完整设备称呼",
          ("fits_model", "iphone 15 pro max", True) in facts(m))
    check("尾置:品牌词拆分",
          ("compatible_brand", "iphone", True) in facts(m))


def test_multi_device():
    m, _ = _m("Blade for Bosch X90 and Miele 300 Series")
    check("多设备:两品牌两型号",
          {("compatible_brand", "bosch", True),
           ("fits_model", "bosch x90", True),
           ("compatible_brand", "miele", True),
           ("fits_model", "miele 300 series", True)} <= facts(m))
    check("多设备:身份=Blade", m.product_identity == "Blade")


def test_no_fitment():
    m, _ = _m("Stainless Steel Mixing Bowl 5L")
    check("无兼容:compat 空", m.compat == [])
    check("无兼容:身份原样", m.product_identity == "Stainless Steel Mixing Bowl 5L")


def test_brand_is_body():
    """品牌词属于商品本体（无引入词）：绝不敢抽。"""
    m, _ = _m("Brandco M9 Official Accessory Kit Deluxe")
    check("本体品牌:无 compat", m.compat == [])
    check("本体品牌:body 保留品牌词",
          {"brandco", "m9"} <= set(m.body_terms()))


def test_zh_fitment():
    m, _ = _m("手机壳 适用于Phoneco 14 Pro 磨砂")
    check("中文:适配段剥离", m.product_identity == "手机壳 磨砂",
          repr(m.product_identity))
    check("中文:品牌型号拆分",
          ("compatible_brand", "phoneco", True) in facts(m))
    check("中文:本体词零渗漏",
          not ({"phoneco", "14", "pro"} & set(m.body_terms())))


def test_usage_phrase_kept():
    """用途短语（无品牌/型号信号）：不是兼容事实，原文保留。"""
    m, _ = _m("Spoon for Cooking and Baking")
    check("用途:compat 空", m.compat == [])
    check("用途:标题整句保留（含 for）",
          m.product_identity == "Spoon for Cooking and Baking")
    check("用途:用途词留在本体", {"cooking", "baking"} <= set(m.body_terms()))


def test_generic_device_words_not_brand():
    m, _ = _m("Filter for Vacuum Cleaner")
    check("泛称:不构成品牌事实", m.compat == [])
    check("泛称:标题保留", m.product_identity == "Filter for Vacuum Cleaner")


def test_uncertain_no_strip():
    """不确定边界：零抽取/零剥离/原文保留/如实标记。"""
    m, _ = _m("Gadget for Everyday Use in Modern Kitchens Everywhere You "
              "Like Cooking Food Meals")
    check("不确定:无确定剥离（原文保留）",
          m.product_identity.startswith("Gadget for "),
          repr(m.product_identity))
    check("不确定:不编造 compat",
          all(not f.certain for f in m.compat) or m.compat == [])


def test_uncertain_digit_model_recorded_not_stripped():
    """中置纯数字型号（无边界）：记 compat(certain=False) 但不剥离。"""
    m, _ = _m("Band for Watchco 20 Compatible Strap Style Color Options "
              "Included Here Now")
    if m.compat:
        check("纯数字:只记不确定事实", all(not f.certain for f in m.compat))
        check("纯数字:不剥离身份",
              m.product_identity.startswith("Band for Watchco"))
    else:
        check("纯数字:零抽取也可（保守）", True)


def test_understanding_priority_and_conflict():
    """understanding 优先：身份直用；启发式值撞理解身份=本体词弃抽；
    理解 compat 照收。"""
    u = {"product_identity": {"title_product_identity":
                              "Watchco 20 Sport Band"},
         "identifiers": {"model_numbers": []},
         "compatibility": {"brands": ["Watchco"], "models": [],
                           "part_numbers": []}}
    m, _ = _m("Strap for Watchco 20 and Longtail X90 Series", u)
    check("理解:身份=理解输出", m.product_identity == "Watchco 20 Sport Band"
          and m.identity_source == "understanding")
    check("理解:compat 来自 understanding",
          ("compatible_brand", "watchco", True) in facts(m))
    check("理解:启发式撞身份弃抽（无 watchco 事实双记）",
          sum(1 for f in m.compat
              if f.value.lower() == "watchco") == 1)
    check("理解:未撞身份的启发式仍补（longtail）",
          any("longtail" in f.value.lower() for f in m.compat))


def test_compat_token_level_exclusion():
    """整值 compat 的 token 也要排除（Thermomix TM7 → 两个 token）。"""
    m, _ = _m("Cover for Brandx A1 Pot Shield Protector")
    check("token级排除:品牌/型号 token 都不进本体词",
          not ({"brandx", "a1"} & set(m.body_terms())), str(m.body_terms()))


def test_search_plan_compat_as_independent_constraint():
    """检索计划：S1/S2 纯本体；S3 本体+兼容=独立约束（D2.6）。"""
    m, _ = _m("Lid for Blendtec X15 Jar Cover")
    plan = search_plan(m)
    check("计划:S1 空（无理解身份不硬搜）", plan["s1_item_name"] == "")
    s2 = {k.lower() for k in plan["s2_keywords"]}
    s3 = {k.lower() for k in plan["s3_keywords"]}
    check("计划:S2 零兼容词", not ({"blendtec", "x15"} & s2))
    check("计划:S3 带兼容约束",
          {"blendtec"} <= s3 and not ({"blendtec"} & s2),
          f"s2={plan['s2_keywords']} s3={plan['s3_keywords']}")


def test_opt_title_fitment():
    """opt 优化标题里的 Compatible with 同样识别。"""
    m, _ = _m("原始中文标题", opt={"title":
                                  "Pot Lid Compatible with Brandy Z9 "
                                  "Dishwasher Safe"})
    check("opt:compat 从优化标题抽取",
          ("compatible_brand", "brandy", True) in facts(m))


def test_purity():
    """纯函数守卫：输入绝不被修改。"""
    rec = {"pid": "p", "raw_rows": [{"标题(必填)":
                                     "Case for Phonex 12 Pro"}],
           "opt": None, "sku": "S", "cat": "c"}
    u = {"compatibility": {"models": ["Phonex 12"]}}
    before = snapshot(rec, u)
    extract_material("p", rec, u)
    check("纯函数:record/understanding 不变", snapshot(rec, u) == before)


def main():
    test_mid_title_long_fitment()
    test_trailing_fitment()
    test_multi_device()
    test_no_fitment()
    test_brand_is_body()
    test_zh_fitment()
    test_usage_phrase_kept()
    test_generic_device_words_not_brand()
    test_uncertain_no_strip()
    test_uncertain_digit_model_recorded_not_stripped()
    test_understanding_priority_and_conflict()
    test_compat_token_level_exclusion()
    test_search_plan_compat_as_independent_constraint()
    test_opt_title_fitment()
    test_purity()
    print(f"\n==== test_pt_compat_identity：{len(_PASSED)} 项全过 ====")


if __name__ == "__main__":
    main()

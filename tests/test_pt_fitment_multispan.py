# -*- coding: utf-8 -*-
"""P1 回归：多段兼容关系剥离（pt.material，2026-10-16 批准令任务一）。

旧缺陷（155883 刹车片）：span 是原文坐标，_strip_fitment 从左到右逐段
``t[:s]+" "+t[e:]`` 连续改串——第二段起坐标全部漂移，产生
「S10 High Quality→S1 ty」吞字；且全局删除引入词（_FIT_MARK_RE.sub）
误伤不确定段的引入词。修复=从右向左安全剥离 + 越界/重叠/重复 span
通用防护 + 残留兼容词检查。

覆盖：155883 原题吞字回归、合成三段、token 完整性（原文词集=
本体∪剥离段，零丢失零重复）、确定值不残留本体、不确定段原文保留
（含引入词）+ 残留引入词说明、越界/重叠/重复 span 防护（坐标注入）、
中文多段/纯中文值/中英混排坐标、纯函数（输入零修改）。
全部通用场景，无任何测试商品特判。
"""

import copy
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from pt import material as mat  # noqa: E402
from pt.material import (  # noqa: E402
    _fitment_analysis, _strip_fitment, extract_material)

_PASSED = []


def check(name, ok, detail=""):
    if not ok:
        raise AssertionError(f"{name} 失败 {detail}")
    _PASSED.append(name)
    print(f"PASS {name}" + (f"  {detail}" if detail else ""))


def _m(title, understanding=None):
    rec = {"pid": "p", "raw_rows": [{"标题(必填)": title}], "opt": None,
           "sku": "S1", "cat": ""}
    return extract_material("p", rec, understanding), rec


def _tok(s):
    return Counter(re.findall(r"[a-z0-9一-鿿]+", s.lower()))


def _span_texts(title):
    return [title[s:e] for a in _fitment_analysis(title)
            if a["span"] for s, e in [a["span"]]]


# ---- 155883 刹车片原题（任务一.8 重点回归）----
_T155883 = ("Electric Scooter Dis Brake Pads YK-34 Avid "
            "For Mechanical Disc Brake JAK-8R "
            "For Kugoo G1 Zero 8X HERO S8 S10 High Quality Bike")


def test_155883_no_swallow():
    m, _ = _m(_T155883)
    check("155883:无吞字（S10 High Quality 完整保留）",
          "High Quality" in m.product_identity
          and "S1 ty" not in m.product_identity,
          m.product_identity)
    check("155883:本体=去兼容后的标题",
          m.product_identity ==
          "Electric Scooter Dis Brake Pads YK-34 Avid High Quality Bike",
          m.product_identity)
    fv = {(f.kind, f.value.lower(), f.certain) for f in m.compat}
    check("155883:品类段只记裸型号 JAK-8R（Mechanical Disc Brake 不当品牌）",
          ("fits_model", "jak-8r", True) in fv
          and not any("mechanical" in v or "disc" in v or "brake" in v
                      for k, v, _ in fv if k == "compatible_brand"),
          sorted(fv))
    check("155883:多型号各自独立 fits_model（不拼整串）",
          {("fits_model", "kugoo g1", True), ("fits_model", "zero 8x", True),
           ("fits_model", "hero s8", True), ("fits_model", "s10", True),
           ("compatible_brand", "kugoo", True)} <= fv, sorted(fv))


def test_synthetic_three_segments():
    t = "Pad for Alpha X1, for Beta Y2, for Gamma Z3 Replacement"
    m, _ = _m(t)
    check("三段:本体只剩本体词", m.product_identity == "Pad Replacement",
          m.product_identity)
    fv = {(f.kind, f.value.lower(), f.certain) for f in m.compat}
    check("三段:三品牌三型号全 certain",
          {("compatible_brand", "alpha", True),
           ("fits_model", "alpha x1", True),
           ("compatible_brand", "beta", True),
           ("fits_model", "beta y2", True),
           ("compatible_brand", "gamma", True),
           ("fits_model", "gamma z3", True)} == fv, sorted(fv))


def test_token_integrity():
    """任务一.7：多段剥离后文本完整性——原文词集 = 本体 ∪ 剥离段。"""
    long_t = ("Bike Brake Pads Compatible with Brandix A1 "
              "and Spare Kit for Copilot B2 Y2 Z3 Pro "
              "Works with Echomax C3 Long Distance Riding Mountain Trail "
              "All Weather Performance Premium Quality Pack of 4 Pairs "
              "Universal Fit Most Models on The Market Today 2026 Edition")
    for name, t in [("155883", _T155883),
                    ("三段", "Pad for Alpha X1, for Beta Y2, "
                             "for Gamma Z3 Replacement"),
                    ("长标题", long_t)]:
        m, _ = _m(t)
        want = _tok(t)
        got = _tok(m.product_identity) + Counter()
        for sp in _span_texts(t):
            got += _tok(sp)
        check(f"完整性[{name}]:零丢失零重复", want == got,
              f"缺={list((want - got).elements())[:6]} "
              f"多={list((got - want).elements())[:6]}")


def test_certain_values_not_in_body():
    """任务一.7：残留兼容词检查——确定 compat 值不得残留在本体。"""
    for t in (_T155883, "Pad for Alpha X1, for Beta Y2, for Gamma Z3 "
                        "Replacement"):
        m, _ = _m(t)
        body = set(re.findall(r"[a-z0-9]+", m.product_identity.lower()))
        leak = set()
        for f in m.compat:
            if f.certain:
                leak |= {w for w in re.findall(r"[a-z0-9]+",
                                               f.value.lower())} & body
        check(f"残留[{t[:16]}…]:确定 compat 零泄漏", not leak, sorted(leak))


def test_uncertain_kept_verbatim():
    t = ("Cover for Brandix X5, Gadget for Watchco 20 Something Long "
         "Enough To Fill Window Completely Here")
    m, _ = _m(t)
    check("不确定段:原文整段保留（含引入词）",
          "for Watchco 20 Something" in m.product_identity,
          m.product_identity)
    check("不确定段:确定段照常剥离",
          "Brandix" not in m.product_identity
          and "Cover" in m.product_identity and "Gadget" in m.product_identity,
          m.product_identity)
    unc = {(f.kind, f.value.lower()) for f in m.compat if not f.certain}
    check("不确定段:记 compat 且 certain=False（不编造）",
          ("compatible_brand", "watchco") in unc
          and ("fits_model", "watchco 20") in unc, sorted(unc))
    check("不确定段:fitment_uncertain 标志置位", m.fitment_uncertain is True)
    _, u, note = _strip_fitment(t)
    check("残留引入词:说明里如实记录", u and "不确定" in note
          and "残留引入词" in note, note)


def test_span_guards():
    """越界/重叠/重复 span 通用防护（注入坐标，验证剥离层不猜坐标）。"""
    t = "Case for Brandix X5 and Copilot Y2 Daily"
    orig = mat._fitment_analysis
    try:
        mat._fitment_analysis = lambda _: [
            {"span": (500, 600), "facts": [("fits_model", "X9", True)],
             "note": ""},
        ]
        out, unc, note = mat._strip_fitment(t)
        check("越界 span:原文零修改", out == t, out)
        check("越界 span:记录原因", "越界" in note, note)

        mat._fitment_analysis = lambda _: [
            {"span": (8, 22), "facts": [("fits_model", "A", True)],
             "note": ""},
            {"span": (15, 30), "facts": [("fits_model", "B", True)],
             "note": ""},
        ]
        out, unc, note = mat._strip_fitment(t)
        # 右段 (15,30) 先剥；左段 (8,22) 与之重叠 → 跳过并记录
        check("重叠 span:只剥不重叠的右段",
              out == t[:15] + " " + t[30:], out)
        check("重叠 span:记录原因", "重叠" in note, note)

        mat._fitment_analysis = lambda _: [
            {"span": (8, 22), "facts": [("fits_model", "A", True)],
             "note": ""},
            {"span": (8, 22), "facts": [("fits_model", "A", True)],
             "note": ""},
        ]
        out, unc, note = mat._strip_fitment(t)
        check("重复 span:只剥一次",
              out == t[:8] + " " + t[22:], out)
        check("重复 span:记录原因", "重复" in note, note)
    finally:
        mat._fitment_analysis = orig


def test_cjk_multispan():
    t = "手机壳 适用于 Alpha X1 适用于 Beta Y2 磨砂"
    m, _ = _m(t)
    check("中文多段:本体=手机壳 磨砂", m.product_identity == "手机壳 磨砂",
          m.product_identity)
    fv = {(f.kind, f.value.lower(), f.certain) for f in m.compat}
    check("中文多段:两段品牌+型号全 certain",
          {("compatible_brand", "alpha", True),
           ("fits_model", "alpha x1", True),
           ("compatible_brand", "beta", True),
           ("fits_model", "beta y2", True)} == fv, sorted(fv))
    # 纯中文值（旧实现此路径 AttributeError）：首段为设备名，中文
    # 描述词留在本体
    m2, _ = _m("手机壳 适用于极米投影仪 磨砂")
    check("纯中文值:不崩溃且描述词留在本体",
          m2.product_identity == "手机壳 磨砂", m2.product_identity)
    check("纯中文值:设备名记 fits_model",
          any(f.value == "极米投影仪" and f.certain for f in m2.compat),
          [(f.kind, f.value, f.certain) for f in m2.compat])
    # 中文引入词边界（值不得吞掉下一个 适用于）
    spans = _span_texts(t)
    check("中文多段:span 精确（不吞下一段引入词）",
          spans == ["适用于 Alpha X1", "适用于 Beta Y2"], spans)


def test_purity():
    """原文绝不被修改（商品库原标题只读——任务一.5）。"""
    for t in (_T155883, "手机壳 适用于 Alpha X1 适用于 Beta Y2 磨砂",
              "Cover for Brandix X5, Gadget for Watchco 20 Something Long "
              "Enough To Fill Window Completely Here"):
        before = copy.deepcopy(t)
        rec = {"pid": "p", "raw_rows": [{"标题(必填)": t}], "opt": None,
               "sku": "S1", "cat": ""}
        rec2 = copy.deepcopy(rec)
        extract_material("p", rec, None)
        check(f"纯函数[{t[:12]}…]:标题与 record 零修改",
              t == before and rec == rec2)


def main():
    test_155883_no_swallow()
    test_synthetic_three_segments()
    test_token_integrity()
    test_certain_values_not_in_body()
    test_uncertain_kept_verbatim()
    test_span_guards()
    test_cjk_multispan()
    test_purity()
    print(f"\n==== test_pt_fitment_multispan：{len(_PASSED)} 项全过 ====")


if __name__ == "__main__":
    main()

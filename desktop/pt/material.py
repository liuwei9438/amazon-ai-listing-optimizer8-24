# -*- coding: utf-8 -*-
"""pt.material —— 第一阶段模块 3（甲）：只读取料层。

从现有商品系统抽取分类匹配所需的资料。铁律：
1. 只读：绝不修改原始商品记录（raw 行 / opt / 理解输出原样奉还）。
2. 不重新调用 AI 理解：理解结果（analyzer profile）作为输入注入，有就用、
   没有就按原始资料降级（degraded=True），缺什么记什么（missing），
   绝不编造缺失事实（空就是空，不填 "unknown"）。
3. 身份优先级（要求 3 + 2026-10-12 批复修正 1）：理解输出的
   product_identity（准确英文身份）> identifiers.model_numbers（本体
   型号）> 剥离适配语后的原始标题（最后兜底，identity_source 标明
   来源）。**SKU 永不充当商品身份或分类语义检索词**——只作商品标识；
   仅当 SKU 片段与标题/理解等语义语料重合（明确语义证据）时才可提取
   （见 sku_semantic_tokens，且这些词已经由语料进入检索词，SKU 不额外
   贡献检索词）。
4. 本体与兼容严格分开（要求 4/5 + 2026-10-12 批复修正 2）：
   compatibility{brands,models,part_numbers} 与标题里「适用于/兼容/
   for」启发式抽取全部进 compat 列表（带 kind 与 source），绝不混入
   产品本体身份；**兼容品牌绝不当作商品品牌**；适配设备类型与用途
   场景经 auxiliary_signals() 作为分类判断的辅助信息（模块 4 用）；
   S1/S2 检索词永远排除兼容词——「适用 iPhone 15 的手机壳」绝不能按
   iPhone 去搜。

输入形状（与现状逐字一致，不发明字段）：
  record: 桌面版产品记录 {pid, sku, title, cat, opt:{title,short_title,
         bullets[],description,...}|None, raw_rows:[{20 列采集器中文键}]}
         —— 20 列表 = main._COLLECTOR_HEADERS（父SKU(必填)/SKU/…/标题(必填)
         /要点1-5/简介/产品图/简介图/参考网址）。
  understanding: 引擎结构化输出（analyzer/product_profile_schema 形状）
         {product_identity{...}, identifiers{...}, basic_info{...},
          compatibility{brands[],models[],part_numbers[],...}} | None。
两者由 main 在装配期注入（模块 7/8 接线）；本模块不 import main、不联网。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

# 采集器 20 列（与 main._COLLECTOR_HEADERS 一致；只列本模块用到的）
COL_TITLE = "标题(必填)"
COL_SKU = "SKU"
COL_BULLETS = ("要点1", "要点2", "要点3", "要点4", "要点5")
COL_DESC = "简介"
COL_IMAGES = "产品图"
COL_DETAIL_IMAGES = "简介图"

_CJK_RE = re.compile(r"[一-鿿]")
# ======================================================================
# 兼容关系识别（D2 修复 2026-10-16：本体与 compatible_brand/fits_model/
# fits_part 分离；边界不确定=保留原文+标记，绝不强行剥离）。
#
# 旧实现的缺陷（本轮根因）：_FIT_EN_RE 固定窗口 41 字符 + 终止集过窄
# （$|[,;，。]|with|\d{3,}），「for Thermomix TM7 Mix Pot Cover PC
# Transparent Visualization」这类适配语后还跟着本体词的长标题整条
# 不匹配 → 既不抽 compat 也不剥离 → 兼容品牌/型号渗入身份与检索词。
#
# 新架构（通用规则，无任何商品特判）：
#   ① 引入词（适用于/兼容/compatible with/works with/made for/
#     fit(s) for/fits/for）触发扫描；
#   ② 短语层：适配语到结构边界（标点/结尾/下一个 with|for）= 确定
#     短语；短语内再做 品牌+型号 拆分（compatible_brand / fits_model）；
#     纯小写普通词=用途短语（D2.4：不是兼容事实，保留不剥）；
#   ③ 窗口耗尽无边界（适配语后面还跟着本体词）→ 窄规则：品牌词+型号
#     token（字母数字混合如 TM7/A2540，或 品牌词+纯数字 如 iPhone 15）
#     只抽品牌+型号、绝不吞后续本体词；纯数字型号（边界不明）按
#     「不确定」处理：记 compat（certain=False）但不从身份剥离；
#   ④ 连窄规则都没有可靠信号 → 不确定：零抽取、零剥离、原文保留，
#     material.fitment_uncertain/identity_note 如实标记。
# ======================================================================
_FIT_INTRO_RE = re.compile(
    r"适用于|兼容(?:于|用)?"
    r"|\bcompatible\s+with\b|\bworks\s+with\b|\bmade\s+for\b"
    r"|\bfit(?:s)?\s+for\b|\bfits\b|\bfor\b",
    re.IGNORECASE)
# 中文引入词短语（中文无空格，走独立短语正则；结尾补 with/for 边界；
# 引入词本身也是边界——P1：两个「适用于」连排时前段值不得吞掉后段
# 引入词（多段兼容关系各自成段））
_FIT_ZH_RE = re.compile(
    r"(适用于|兼容(?:于|用)?)([\w\s\-+×x\.]{2,40}?)"
    r"(?=$|[,;，。;；/（(]|with\b|for\b|适配|专用|适用于|兼容)", re.IGNORECASE)

# 检索词里必须剔除的虚词（中英）
_STOPWORDS = {"the", "for", "and", "with", "new", "hot", "a", "an", "of",
              "or", "的", "适用", "兼容", "通用", "新款", "爆款"}
# 残留的适配标记（短语没匹配上时兜底剥掉标记本身——只剥标记词，
# 绝不剥后面的文本）
_FIT_MARK_RE = re.compile(
    r"适用于|兼容(?:于|用)?(?=[\sA-Za-z0-9一-鿿])"
    r"|\bcompatible\s+with\b|\bworks\s+with\b|\bmade\s+for\b"
    r"|\bfit(?:s)?\s+for\b|\bfits\b|\bfor\b", re.IGNORECASE)

_EN_TOKEN_RE = re.compile(r"[A-Za-z0-9][\w\-\+\.]*|[^\sA-Za-z0-9]")
_FIT_MAX_TOKENS = 8          # 适配语窗口（token 数）——超过即视为
                              # 「后面还跟着本体词」，转窄规则/不确定
_BRAND_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z&\.\-]{1,}$")
# 通用器物/用途词（小写比较）——大写出现也不构成品牌证据（"Vacuum
# Cleaner"/"Cooking"）。通用英文词汇表，非任何商品特判。
# P3 扩充（2026-10-16 批准令任务二）：品类描述/部件/功能/材质词——
# Title Case 标题里这些词全部大写开头，「for Mechanical Disc Brake
# JAK-8R」的品类段绝不能因为位于引入词后就当品牌串。词表是通用
# 词汇层（无任何品牌特判；代价：单词品牌撞名（如 Ring）按保守处理
# 拒认，宁缺毋滥）。
_GENERIC_WORDS = {
    "cleaner", "cooker", "mixer", "oven", "blender", "kettle", "toaster",
    "machine", "vacuum", "series", "pro", "max", "plus", "deluxe",
    "edition", "cooking", "baking", "stand", "pot", "lid", "cover",
    "case", "filter", "bowl", "knife", "blade", "spoon", "jar", "bottle",
    "cup", "mug", "holder", "rack", "tray", "bag", "box", "set", "pack",
    "kit", "pair", "pcs", "large", "small", "new", "hot",
    # ---- P3 品类描述/部件/功能/材质（全小写比较）----
    "mechanical", "electric", "electronic", "digital", "manual",
    "automatic", "cordless", "disc", "disk", "brake", "pad", "pads",
    "sealing", "ring", "gasket", "seal", "washer", "valve", "elbow",
    "tee", "fitting", "fittings", "pipe", "pipes", "hose", "hoses",
    "tube", "tubes", "pump", "motor", "engine", "battery", "charger",
    "charging", "cable", "cord", "wire", "plug", "socket", "switch",
    "button", "strap", "band", "belt", "plate", "screw", "bolt", "nut",
    "wrench", "tool", "spare", "replacement", "upgrade", "universal",
    "stainless", "steel", "rubber", "silicone", "plastic", "copper",
    "brass", "foam", "sponge", "brush", "comb", "mirror", "frame",
    "panel", "doorbell", "adapter", "connector", "mount", "bracket",
    "handle", "knob", "wheel", "tire", "tyre", "chain", "gear", "spring",
    "shield", "guard", "grill", "grate", "basket", "crate", "caddy",
    "organizer", "dispenser", "sprayer", "nozzle", "funnel", "strainer",
    "colander", "peeler", "grater", "whisk", "spatula", "tongs",
    "chopper", "slicer", "shredder", "grinder", "presser", "steamer",
    "roaster", "fryer", "warmer", "cooler", "freezer", "heater",
    "kitchen", "bathroom", "bedroom", "office", "outdoor", "indoor",
    "garden", "garage", "sports", "fitness", "travel", "car", "auto",
    "bike", "bicycle", "scooter", "motorcycle",
}


def _is_brandword(tok: str) -> bool:
    """品牌词：纯字母词（≥2）、含大写（iPhone/Dyson 这类小写开头
    驼峰也算）、非虚词、非通用器物词。「cooking」「cleaner」=普通词。"""
    return (bool(_BRAND_WORD_RE.match(tok))
            and any(c.isupper() for c in tok)
            and tok.lower() not in _STOPWORDS
            and tok.lower() not in _GENERIC_WORDS)


def _is_model_token(tok: str) -> bool:
    """强型号 token：字母+数字混合（TM7/A2540/X90/HX6837）。"""
    return (bool(re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok))
            and len(tok) <= 12)


def _split_brand_model(phrase: str) -> list[tuple[str, str, bool]]:
    """适配短语 → [(kind, value, certain)]。

    品牌+型号拆分（D2.2 + P3 任务二）；「A and B」并列多设备分别拆；
    纯用途小写短语返回 []（不是兼容事实）。
    P3（2026-10-16 批准令）：
    - 位于引入词后 ≠ 品牌：品牌游程在品类词处停（_brand_start），
      「品类描述+型号」段只记型号、不编造品牌；
    - 多锚型号 token 级拆分（_anchor_facts）：各自独立 fits_model
      不拼整串（'Kugoo YK-34 JAK-8R G1 8X' ≠ 一条巨型型号）。"""
    phrase = str(phrase or "").strip()
    if not phrase:
        return []
    # 并列适配（for A X90 and Miele 300 / A & B / A / B）逐段拆再合并；
    # 全部子段都没有型号=用途并列（Cooking and Baking），不是兼容事实
    parts = re.split(r"\s+(?:and|&|/)\s+", phrase)
    if len(parts) > 1:
        facts: list[tuple[str, str, bool]] = []
        for p in parts:
            facts.extend(_split_brand_model(p))
        if not any(k == "fits_model" for k, _, _ in facts):
            return []
        return facts
    toks = [t for t in phrase.split() if t]
    if not toks:
        return []
    has_cjk = any(_CJK_RE.search(t) for t in toks)
    if has_cjk:
        # 中文设备名整段（无拉丁品牌/型号可拆）
        return [("fits_model", phrase, True)]
    anchors = _plain_anchors(toks)
    if not anchors:
        # 无型号锚：仅 ≤2 个品牌词才抽 compatible_brand（再长视为用途/
        # 描述短语——D2.4 用途信息绝不丢）
        if 1 <= len(toks) <= 2 and all(_is_brandword(t) for t in toks):
            return [("compatible_brand", " ".join(toks), True)]
        return []
    return _anchor_facts(toks, anchors)


def _zh_fit_value(value: str) -> str:
    """中文引入词后的值：拉丁设备名后跟中文描述词时，描述词留在本体。
    「iPhone 14 Pro 磨砂」→「iPhone 14 Pro」；纯中文值原样。P1：值带
    前导/尾随空白（「适用于 Alpha X1」）先整段去空白再匹配——旧实现
    re.match 直接撞 None。"""
    v = str(value or "").strip()
    m = re.match(r"([A-Za-z0-9][A-Za-z0-9\s\-+×x\.]*?)(?=[一-鿿]|$)", v)
    if m is None:
        # 纯中文/无拉丁头：取首个连续段（「极米投影仪 磨砂」→
        # 「极米投影仪」），后续中文描述词留在本体——与拉丁头规则
        # 对称（旧实现在此路径直接 AttributeError，从未生效过）。
        return v.split()[0] if v.split() else v
    return (m.group(1) or "").strip() or v


# 设备名后缀词（iPhone 15 Pro Max / Miele 300 Series 的 尾缀）——
# 型号锚定剥离时允许跟着型号走；与 _GENERIC_WORDS 分开（后者不当后缀）
_MODEL_SUFFIX = {"pro", "max", "plus", "ultra", "mini", "series", "sport",
                 "edge", "se", "5g", "4k"}


def _anchor_indices(toks: list, n: int) -> list[int]:
    """消费区内全部型号 token 下标（强混合型号 或 品牌词+纯数字）。"""
    out = []
    for i in range(min(n, len(toks))):
        w = toks[i].group(0)
        if _is_model_token(w):
            out.append(i)
        elif (re.fullmatch(r"\d{1,4}[A-Za-z]?", w) and i > 0
                and _is_brandword(toks[i - 1].group(0))):
            out.append(i)
    return out


def _anchor_end(toks: list, n: int, idx: int) -> int:
    """剥离终点：型号 token + ≤2 个设备名后缀（只认型号后缀/型号
    token——品牌词不算后缀，防把 Mixing 之类的本体词带进剥离段）。"""
    j = idx
    steps = 0
    while j + 1 < n and steps < 2:
        w = toks[j + 1].group(0)
        if w.lower() in ("and", "&", "/"):
            break
        if w.lower() in _MODEL_SUFFIX or _is_model_token(w):
            j += 1
            steps += 1
        else:
            break
    return toks[j].end()


def _brand_start(toks: list[str], mi: int) -> int:
    """型号锚 mi 前的品牌游程起点（P3：品类词/泛称词处停——品类描述
    不属于品牌，'Mechanical Disc Brake JAK-8R' 的品牌串为空）。"""
    bi = mi
    while (bi > 0 and mi - (bi - 1) <= 3
           and _is_brandword(toks[bi - 1])):
        bi -= 1
    return bi


def _plain_anchors(toks: list[str]) -> list[int]:
    """纯 str token 列表的型号锚下标（强混合型号 或 品牌词+纯数字）。"""
    out = []
    for i, w in enumerate(toks):
        if _is_model_token(w):
            out.append(i)
        elif (re.fullmatch(r"\d{1,4}[A-Za-z]?", w) and i > 0
                and _is_brandword(toks[i - 1])):
            out.append(i)
    return out


def _anchor_facts(toks: list[str], anchors: list[int]
                  ) -> list[tuple[str, str, bool]]:
    """锚序列 → compat facts（P3 任务二.4：token 级拆分，保留型号间
    真实关系，绝不拼成整串）：
    - 每锚一簇（锚 + ≤2 个纯后缀词）：多型号各自独立 fits_model
      （'Kugoo YK-34 JAK-8R G1 8X' → Kugoo + YK-34/JAK-8R/G1/8X 各条，
      不是一条整串）；
    - 锚前紧邻品牌游程 → 该簇型号带品牌（Kugoo G1 / Zero 8X），
      否则裸型号（TM5 / S10 / JAK-8R——品类段 'Mechanical Disc
      Brake JAK-8R' 品牌串为空，只记 JAK-8R，不编造品牌）；
    - 品牌游程只记一次 compatible_brand。"""
    facts: list[tuple[str, str, bool]] = []
    brand_done = False
    for a in anchors:
        end = a
        steps = 0
        while (end + 1 < len(toks) and steps < 2
               and toks[end + 1].lower() in _MODEL_SUFFIX):
            end += 1
            steps += 1
        bi = _brand_start(toks, a)
        if bi < a:
            if not brand_done:
                facts.append(("compatible_brand",
                              " ".join(toks[bi:a]), True))
                brand_done = True
            facts.append(("fits_model",
                          " ".join(toks[bi:end + 1]), True))
        else:
            facts.append(("fits_model", " ".join(toks[a:end + 1]), True))
    return facts


def _scan_en_fitment(text: str, m: "re.Match") -> dict:
    """拉丁引入词之后 → {span, facts, note}（见模块头 D2 架构说明）。
    span=(起,止) 仅在「确定且有事实」时给出（用于身份剥离）。
    剥离段一律「型号锚定」：品牌词+型号+受控后缀——旧实现的教训是
    把到边界的整段当适配语，长标题里会吞掉后面的本体词。"""
    seg = text[m.end():]
    toks = list(_EN_TOKEN_RE.finditer(seg))
    content_end = 0
    n_content = 0
    boundary = None
    consumed_all = True
    for i, t in enumerate(toks[:_FIT_MAX_TOKENS]):
        w = t.group(0)
        if not re.match(r"[A-Za-z0-9]", w):
            boundary = w
            consumed_all = False
            break
        if w.lower() in ("with", "for") and i > 0:
            boundary = w
            consumed_all = False
            break
        content_end = t.end()
        n_content = i + 1
    else:
        # 窗口走满仍无边界：只有「token 本来就少于窗口」才是真到结尾；
        # 恰好 = 窗口数时视为「后面还跟着词」，转窄规则（D2 旧缺陷根因：
        # 固定窗口把长尾当结尾整段吞掉）
        consumed_all = len(toks) < _FIT_MAX_TOKENS
    phrase = seg[:content_end].strip()

    # ①② 有边界（标点/结尾/with/for）→ 确定短语
    if boundary is not None or consumed_all:
        if not phrase:
            return {"span": None, "facts": [],
                    "note": "适配语后为空/紧邻边界——跳过"}
        facts = _split_brand_model(phrase)
        if not facts:
            return {"span": None, "facts": [],
                    "note": f"「{phrase[:24]}」无品牌/型号信号，按用途保留"}
        anchors = _anchor_indices(toks, n_content)
        if anchors:
            # 多设备并列剥到最后一个型号；单一/品牌-only 剥到短语尾。
            # 长短语的 fits_model 值也按锚定截断——「Blendtec X15 Mixing
            # Jar Cover」的设备名是 Blendtec X15，尾巴是本体词。
            end = _anchor_end(toks, n_content, anchors[-1])
            if end < content_end:
                trimmed = seg[:end].strip()
                if trimmed:
                    refacts = _split_brand_model(trimmed)
                    if refacts:
                        facts = refacts
        else:
            end = content_end       # 品牌-only（Bag for Dyson）：整短语
        return {"span": (m.start(), m.end() + end), "facts": facts,
                "note": ""}

    # ③ 窗口耗尽（后面还有本体词）→ 窄规则：品牌+型号（锚定）。
    # P3（任务二.4）：多锚 token 级处理——窗口内全部锚各自记
    # fits_model（_anchor_facts）；存在强锚（字母+数字混合）= 确定段，
    # 剥到窗口内最后一个锚的受控终点（含锚间夹词——连续适配段，如
    # 'Kugoo G1 Zero 8X HERO S8 S10'，锚后的本体词绝不动）；仅纯数字
    # 锚（边界弱）= 记 compat 不剥离（不确定）；无锚 = 零抽取零剥离。
    toks_plain = [t.group(0) for t in toks[:_FIT_MAX_TOKENS]]
    anchors = _plain_anchors(toks_plain)
    if not anchors:
        # ④ 无可靠信号 → 不确定：零抽取零剥离，原文保留
        return {"span": None, "facts": [],
                "note": f"「{m.group(0).strip()} …」边界不确定"
                        f"（后随本体词、无品牌/型号信号）——原文保留未剥离"}
    facts = _anchor_facts(toks_plain, anchors)
    strong = any(_is_model_token(toks_plain[a]) for a in anchors)
    if not strong:
        # 仅纯数字锚（iPhone 15 型）：中置无边界 → 记录但不剥离
        return {"span": None,
                "facts": [(k, v, False) for k, v, _ in facts],
                "note": "纯数字型号无边界——记 compat 不剥离（不确定）"}
    end = _anchor_end(toks, n_content, anchors[-1])
    return {"span": (m.start(), m.end() + end), "facts": facts,
            "note": "中置适配语：窄规则剥离到窗口内最后一个型号锚"}


def _fitment_analysis(text: str) -> list[dict]:
    """全文适配语扫描（中文短语 + 拉丁引入词），返回逐段分析结果。"""
    text = str(text or "")
    out: list[dict] = []
    taken: list[tuple[int, int]] = []

    def _overlaps(s, e):
        return any(s < te and e > ts for ts, te in taken)

    for m in _FIT_ZH_RE.finditer(text):
        raw = m.group(2)
        value = _zh_fit_value(raw)
        if len(value) < 2 or _overlaps(m.start(), m.end()):
            continue
        facts = _split_brand_model(value)
        span = None
        if facts and facts[0][2]:        # 确定才剥（含引入词+保留值）
            # P1：值经清洗（去空白/中文尾巴）后与原文组错位，终点必须
            # 用值在原文组内的真实偏移计算；定位不到（清洗变形）就
            # 不剥——无法确认的 span 不执行剥离，不猜坐标。
            off = raw.find(value)
            if off >= 0:
                span = (m.start(), m.start(2) + off + len(value))
        out.append({"span": span, "facts": facts,
                    "note": "" if facts else "适配语后无有效值"})
        taken.append((m.start(), m.end()))
    for m in _FIT_INTRO_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        r = _scan_en_fitment(text, m)
        if r["span"] is None and not r["facts"] and not r["note"]:
            continue
        if r["span"] is not None and _overlaps(*r["span"]):
            r = {"span": None, "facts": r["facts"],
                 "note": "与前段适配语重叠——保留不剥"}
        out.append(r)
        taken.append((m.start(), m.end()))
    return out


def _strip_fitment(text: str) -> tuple[str, bool, str]:
    """把「确定的」适配段从标题剥掉——剥完剩下的才是产品本体（要求 5：
    兼容设备绝不当本体；不确定段原文保留）。返回 (剥离后文本,
    是否存在不确定段, 说明)。零确定段时不做任何剥离（连引入词标记都
    保留——D2.7 不得强行剥离）。

    P1（2026-10-16 批准令任务一）：span 是原文坐标。旧实现从左到右
    逐段 ``t[:s]+" "+t[e:]`` 连续改串——第二段起坐标全部漂移，产生
    「S10 High Quality→S1 ty」式吞字，且全局删除引入词会连带不确定
    段的引入词。改为从右向左剥离（左侧坐标始终有效），并对重复/
    越界/重叠 span 通用防护（跳过并记录原因，不猜坐标）。"""
    t = str(text or "")
    notes: list[str] = []
    uncertain = False
    spans = []
    for a in _fitment_analysis(t):
        if a["span"] is not None:
            spans.append(a["span"])
        elif a["facts"] or "不确定" in a["note"]:
            uncertain = True
            if a["note"]:
                notes.append(a["note"])
    n = len(t)
    seen: set[tuple[int, int]] = set()
    uniq: list[tuple[int, int]] = []
    for s, e in spans:
        if (s, e) in seen:              # 重复段：只剥一次
            notes.append(f"重复段({s},{e})已合并")
            continue
        seen.add((s, e))
        if not (0 <= s < e <= n):       # 越界：坐标不可信 → 原文保留
            notes.append(f"越界段({s},{e})跳过未剥离")
            continue
        uniq.append((s, e))
    uniq.sort(key=lambda se: -se[0])    # 从右向左：后续段的坐标不失真
    prev_start = n
    stripped: list[str] = []
    for s, e in uniq:
        if e > prev_start:              # 与已剥离段重叠：跳过并记录
            notes.append(f"重叠段({s},{e})跳过未剥离")
            continue
        stripped.append(t[s:e])
        t = t[:s] + " " + t[e:]
        prev_start = s
    if not stripped:
        # 全部为用途短语/不确定段：原文整句保留（含引入词）
        return t, uncertain, "；".join(notes)[:200]
    t = re.sub(r"\s{2,}", " ", t)
    # 剥离后残留的孤立逗号/分号（原本分隔适配段的）清掉
    t = re.sub(r"\s*[,;，;]\s*(?=\s|$)", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—+/，,、")
    # P1 要求 7：多段剥离后的残留兼容词检查——确定段的 span 自带
    # 引入词，剥完仍残留的引入词只应属于不确定段/用途短语（原文保留
    # 是设计行为）；发现即记录说明，绝不二次改写原文。
    residual = _FIT_MARK_RE.findall(t)
    if residual:
        notes.append(f"残留引入词{len(residual)}处（不确定/用途段原文保留）")
    return t, uncertain, "；".join(notes)[:200]


@dataclass
class CompatFact:
    """兼容/适配事实——绝不当产品本体。
    certain=False = 边界不确定的保守记录（不参与身份剥离；检索时仍按
    独立兼容约束对待，绝不混入本体词——D2.7 标记不确定而非编造）。"""
    kind: str            # compatible_brand | fits_model | fits_part
    value: str
    source: str          # understanding | title_heuristic
    certain: bool = True


@dataclass
class ProductMaterial:
    pid: str
    sku: str = ""
    # ---- 本体资料 ----
    title: str = ""               # 最好的人类标题：opt 标题 > 原始标题
    title_raw: str = ""           # 原始标题（可能是中文）
    bullets: list[str] = field(default_factory=list)
    description: str = ""
    model: str = ""               # 本体型号（identifiers.model_numbers[0]）
    product_identity: str = ""    # 准确身份（英文优先）
    identity_source: str = ""     # understanding|model|title_raw（SKU 不参与）
    fitment_uncertain: bool = False   # 适配语边界不确定（原文保留未剥离）
    identity_note: str = ""       # 身份/适配剥离说明（不确定段原因）
    basic_info_product_type: str = ""   # 理解输出的旧线索（只做参考，不当候选）
    usage_scenarios: list[str] = field(default_factory=list)
    compat: list[CompatFact] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    language_guess: str = "en"    # en | zh | mixed
    degraded: bool = False        # True=没有理解结果，纯原始资料降级
    missing: list[str] = field(default_factory=list)

    def compat_values(self) -> list[str]:
        return [f.value.strip().lower() for f in self.compat if f.value.strip()]

    def body_terms(self) -> list[str]:
        """产品本体检索词（已排除全部兼容词；SKU 永不参与）——S1/S2 用。
        兼容词按 token 级排除（D2 回归：「Thermomix TM7」整值要能滤掉
        thermomix/tm7 两个 token，不能只滤整串）。"""
        seeds: list[str] = []
        if self.product_identity:
            seeds.append(self.product_identity)
        if self.model:
            seeds.append(self.model)
        if self.basic_info_product_type:
            seeds.append(self.basic_info_product_type)
        compat = {t.lower() for v in self.compat_values()
                  for t in _tokenize(v)}
        out: list[str] = []
        for seed in seeds:
            for tok in _tokenize(seed):
                low = tok.lower()
                if low in compat or low in _STOPWORDS or len(low) < 2:
                    continue
                if low not in out:
                    out.append(low)
        return out[:10]

    def sku_semantic_tokens(self) -> list[str]:
        """修正 1：SKU 默认只是商品标识。仅当 SKU 的分词片段与语义语料
        （标题/五点/详情/身份/型号/basic_info）重合——即有明确商品语义
        证据——才允许提取；这些词早已由语料进入 body_terms，SKU 本身
        不向检索词贡献任何新词。"""
        corpus = {t.lower() for src in (self.title_raw, self.title,
                                        self.product_identity, self.model,
                                        self.basic_info_product_type,
                                        " ".join(self.bullets),
                                        self.description)
                  for t in _tokenize(src)}
        return [t for t in _tokenize(self.sku) if t.lower() in corpus]

    def auxiliary_signals(self) -> dict:
        """修正 2：分类判断的辅助信息（模块 4 matcher 用）。用途场景与
        适配设备/型号可作辅助；兼容品牌（compatible_brand）只是适配
        关系，绝不当商品品牌或本体身份参与判断。"""
        return {
            "usage_scenarios": list(self.usage_scenarios),
            "fits": [{"kind": f.kind, "value": f.value}
                     for f in self.compat
                     if f.kind in ("fits_model", "fits_part")],
            "compatible_brands": [f.value for f in self.compat
                                  if f.kind == "compatible_brand"],
            "note": "compatible_brands 是适配关系不是商品品牌；分类判断"
                    "只可作配件语境参考，禁止当成本体品牌或身份",
        }


def _tokenize(text: str) -> list[str]:
    """极简分词：拉丁按空格/连字符切，中文按 2 字滑窗。"""
    text = str(text or "")
    out: list[str] = []
    latin = re.findall(r"[A-Za-z0-9][\w\-\+\.]*", text)
    out.extend(w for w in latin if len(w) >= 2)
    cjk = "".join(_CJK_RE.findall(text))
    for i in range(len(cjk) - 1):
        out.append(cjk[i:i + 2])
    seen, dedup = set(), []
    for t in out:
        if t.lower() not in seen:
            seen.add(t.lower())
            dedup.append(t)
    return dedup


def _first_text(*vals: Any) -> str:
    for v in vals:
        v = str(v or "").strip()
        if v:
            return v
    return ""


def _split_images(value: Any) -> list[str]:
    return [u for u in re.split(r"[,;，；\s]+", str(value or ""))
            if u.startswith(("http://", "https://"))]


def extract_material(pid: str, record: dict | None,
                     understanding: dict | None = None) -> ProductMaterial:
    """纯函数：record/understanding 绝不被修改（测试 M7 守卫）。"""
    record = record or {}
    understanding = understanding or None     # {} 也按无理解结果处理

    m = ProductMaterial(pid=str(pid or record.get("pid") or ""),
                        sku=_first_text(record.get("sku")))
    raw_rows = [r for r in (record.get("raw_rows") or [])
                if isinstance(r, dict)]
    opt = record.get("opt") if isinstance(record.get("opt"), dict) else None

    # ---- 原始资料 ----
    m.title_raw = next((str(r.get(COL_TITLE) or "").strip()
                        for r in raw_rows if str(r.get(COL_TITLE) or "").strip()),
                       "")
    raw_bullets = [str(r.get(k) or "").strip() for r in raw_rows
                   for k in COL_BULLETS]
    raw_bullets = [b for b in raw_bullets if b]
    raw_desc = next((str(r.get(COL_DESC) or "").strip() for r in raw_rows
                     if str(r.get(COL_DESC) or "").strip()), "")
    for r in raw_rows:
        for u in _split_images(r.get(COL_IMAGES)) + \
                _split_images(r.get(COL_DETAIL_IMAGES)):
            if u not in m.images:
                m.images.append(u)
    # SKU 列读进 m.sku 仅作商品标识（修正 1），不参与身份/检索词
    raw_sku = next((str(r.get(COL_SKU) or "").strip() for r in raw_rows
                    if str(r.get(COL_SKU) or "").strip()), "")
    if raw_sku and not m.sku:
        m.sku = raw_sku

    # ---- AI 优化结果（只读优先级：opt 标题五点 > 原始） ----
    m.title = _first_text((opt or {}).get("title"), m.title_raw)
    m.bullets = list((opt or {}).get("bullets") or raw_bullets)[:10]
    m.description = _first_text((opt or {}).get("description"), raw_desc)

    # ---- 理解输出（有就用；缺了降级，绝不现调 AI） ----
    ident = {}
    identifiers = {}
    basic = {}
    compat_src = {}
    if isinstance(understanding, dict):
        ident = understanding.get("product_identity") or {}
        identifiers = understanding.get("identifiers") or {}
        basic = understanding.get("basic_info") or {}
        compat_src = understanding.get("compatibility") or {}
        if not isinstance(ident, dict):
            ident = {}
        if not isinstance(identifiers, dict):
            identifiers = {}
        if not isinstance(basic, dict):
            basic = {}
        if not isinstance(compat_src, dict):
            compat_src = {}
    else:
        understanding = None
    m.degraded = understanding is None

    # 身份优先级（要求 3 + 修正 1：SKU 只是标识，永不充当身份）
    m.product_identity = _first_text(
        ident.get("title_product_identity"),
        ident.get("buyer_search_identity"),
        ident.get("name"))
    m.model = _first_text(
        (identifiers.get("model_numbers") or [""])[0]
        if isinstance(identifiers.get("model_numbers"), list) else "")
    if m.product_identity:
        m.identity_source = "understanding"
    elif m.model:
        m.product_identity, m.identity_source = m.model, "model"
    elif m.title_raw:
        # 剥掉适配语再当身份（「手机壳 适用于iPhone 15 Pro」→「手机壳」）；
        # 边界不确定的段原文整句保留并标记（D2.7：不得强行剥离）
        stripped, uncertain, note = _strip_fitment(m.title_raw)
        m.product_identity = stripped or m.title_raw
        m.identity_source = "title_raw"
        m.fitment_uncertain = uncertain
        m.identity_note = note
    m.basic_info_product_type = _first_text(basic.get("product_type"))
    m.usage_scenarios = [str(s) for s in
                         (ident.get("usage_scenarios") or []) if s][:8]

    # ---- 兼容事实（要求 4/5：与本体分开记） ----
    for b in (compat_src.get("brands") or [])[:20]:
        b = str(b).strip()
        if b:
            m.compat.append(CompatFact("compatible_brand", b, "understanding"))
    for v in (compat_src.get("models") or [])[:20]:
        v = str(v).strip()
        if v:
            m.compat.append(CompatFact("fits_model", v, "understanding"))
    for v in (compat_src.get("part_numbers") or [])[:20]:
        v = str(v).strip()
        if v:
            m.compat.append(CompatFact("fits_part", v, "understanding"))
    # 标题启发式（D2 新架构：见模块头）。冲突处理（D2.5）：understanding
    # 优先——启发式值若出现在理解输出的身份句/本体型号里，视为本体词，
    # 弃抽（understanding 已给出更可靠判断）。
    ident_corpus = " ".join(
        x for x in ((m.product_identity
                     if m.identity_source == "understanding" else ""),
                    m.model) if x).lower()
    for txt in (m.title_raw, m.title):
        for a in _fitment_analysis(txt):
            for kind, value, certain in a["facts"]:
                v = str(value).strip(" -–—+/，,、")
                if not v or len(v) < 2:
                    continue
                if ident_corpus and v.lower() in ident_corpus:
                    continue
                m.compat.append(CompatFact(kind, v, "title_heuristic",
                                           certain=certain))
    # 去重（kind+value 小写同形只留一条；understanding 先入=优先保留）
    seen = set()
    dedup = []
    for f in m.compat:
        k = (f.kind, f.value.lower())
        if k not in seen:
            seen.add(k)
            dedup.append(f)
    m.compat = dedup[:40]

    # ---- 语言判定（多语言标题：zh 标题走 PTD searchLocale 本地化搜索） ----
    zh = len(_CJK_RE.findall(m.title_raw or m.title))
    latin = len(re.findall(r"[A-Za-z]", m.title_raw or m.title))
    m.language_guess = ("zh" if zh and not latin else
                        "mixed" if zh and latin else "en")

    # ---- 缺失清单（不编造：缺什么记什么） ----
    miss = []
    if m.degraded:
        miss.append("understanding")
    if not m.model:
        miss.append("model")
    if not m.bullets:
        miss.append("bullets")
    if not m.description:
        miss.append("description")
    if not m.images:
        miss.append("images")
    if not m.product_identity:
        miss.append("identity")
    elif m.identity_source == "title_raw":
        miss.append("accurate_identity")
    m.missing = miss
    return m


def ptd_locale_for(material: ProductMaterial) -> str:
    """中文资料走官方 zh_CN 本地化搜索；英文/混合默认空（官方缺省）。"""
    return "zh_CN" if material.language_guess == "zh" else ""


def search_plan(material: ProductMaterial) -> dict:
    """三策略检索计划（candidates.py 的输入）：
    s1_item_name：本体身份（itemName 推荐；绝不含兼容词）
    s2_keywords：本体词列表（keywords 搜索；绝不含兼容词）
    s3_keywords：相似商品搜索词（本体词 + 兼容适配词——配件找同类
                 配件刊登时适配词有助于相似度，但仍标记 compat 附带）
    """
    body = material.body_terms()
    # 兼容适配词=独立检索约束（D2.6）：优先 understanding，其次确定启发式，
    # 最后才是不确定记录（certain=False——保守参与相似检索，绝不进本体）
    compat_vals = [f.value for f in material.compat
                   if f.source == "understanding"][:4] or \
                  [f.value for f in material.compat if f.certain][:4] or \
                  [f.value for f in material.compat][:4]
    s1 = material.product_identity if material.identity_source in (
        "understanding", "model") else ""
    s2 = body[:8]
    s3 = (body[:6] + [v for v in compat_vals
                      if v.lower() not in {b.lower() for b in body}])[:10]
    return {"s1_item_name": s1, "s2_keywords": s2, "s3_keywords": s3,
            "search_locale": ptd_locale_for(material)}


# 防御性说明：copy 仅为调用方想要副本时提供；extract_material 本身不改输入。
def snapshot(record: dict, understanding: dict | None) -> str:
    """测试守卫用：深拷贝快照（M7 只读断言）。"""
    return repr((copy.deepcopy(record), copy.deepcopy(understanding)))

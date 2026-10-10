# -*- coding: utf-8 -*-
"""pt.material —— 第一阶段模块 3（甲）：只读取料层。

从现有商品系统抽取分类匹配所需的资料。铁律：
1. 只读：绝不修改原始商品记录（raw 行 / opt / 理解输出原样奉还）。
2. 不重新调用 AI 理解：理解结果（analyzer profile）作为输入注入，有就用、
   没有就按原始资料降级（degraded=True），缺什么记什么（missing），
   绝不编造缺失事实（空就是空，不填 "unknown"）。
3. 身份优先级（要求 3）：理解输出的 product_identity（准确英文身份）
   > identifiers.model_numbers（本体型号）> SKU > 原始标题（最后兜底，
   identity_source 标明来源）。
4. 本体与兼容严格分开（要求 4/5）：compatibility{brands,models,
   part_numbers} 与标题里「适用于/兼容/for」启发式抽取全部进 compat 列表
   （带 kind 与 source），绝不混入产品本体身份；S1/S2 检索词永远排除
   兼容词——「适用 iPhone 15 的手机壳」绝不能按 iPhone 去搜。

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
# 兼容启发式：适用于X / 兼容X / for X（拉丁）。抽取不是编造——只在原文里
# 真实出现时才进 compat，source 标 title_heuristic。
_FIT_ZH_RE = re.compile(r"适用于([\w\s\-+×x\.]{2,30}?)(?=$|[，,。;；/（(]|适配|专用)")
_FIT_EN_RE = re.compile(r"\bfor\s+([A-Za-z0-9][\w\s\-+\.]{1,40}?)"
                        r"(?=$|[,;，。]|with\b|\d{3,})", re.IGNORECASE)

# 检索词里必须剔除的虚词（中英）
_STOPWORDS = {"the", "for", "and", "with", "new", "hot", "a", "an", "of",
              "的", "适用", "兼容", "通用", "新款", "爆款"}
# 残留的适配标记（「适用于X」的 X 没匹配上时兜底剥掉标记本身）
_FIT_MARK_RE = re.compile(r"适用于|兼容(?:于|用)?(?=[\sA-Za-z0-9一-鿿])"
                          r"|\bfor\s+(?=[A-Za-z0-9])", re.IGNORECASE)


def _strip_fitment(text: str) -> str:
    """把「适用于X / 兼容X / for X」从标题里剥掉——剥完剩下的才是产品
    本体（要求 5：兼容设备绝不当本体；compat 抽取仍看完整原文，
    这里只影响身份/本体检索词，不是销毁证据）。"""
    t = _FIT_ZH_RE.sub(" ", str(text or ""))
    t = _FIT_EN_RE.sub(" ", t)
    t = _FIT_MARK_RE.sub(" ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—+/，,、")
    return t


@dataclass
class CompatFact:
    """兼容/适配事实——绝不当产品本体。"""
    kind: str            # compatible_brand | fits_model | fits_part
    value: str
    source: str          # understanding | title_heuristic


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
    identity_source: str = ""     # understanding|model|sku|title_raw
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
        """产品本体检索词（已排除全部兼容词）——S1/S2 用。"""
        seeds: list[str] = []
        if self.product_identity:
            seeds.append(self.product_identity)
        if self.model:
            seeds.append(self.model)
        if self.basic_info_product_type:
            seeds.append(self.basic_info_product_type)
        compat = set(self.compat_values())
        out: list[str] = []
        for seed in seeds:
            for tok in _tokenize(seed):
                low = tok.lower()
                if low in compat or low in _STOPWORDS or len(low) < 2:
                    continue
                if low not in out:
                    out.append(low)
        return out[:10]


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
    raw_sku = next((str(r.get(COL_SKU) or "").strip() for r in raw_rows
                    if str(r.get(COL_SKU) or "").strip()), "")

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

    # 身份优先级（要求 3）
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
    elif m.sku or raw_sku:
        m.product_identity = m.sku or raw_sku
        m.identity_source = "sku"
    elif m.title_raw:
        # 剥掉适配语再当身份（「手机壳 适用于iPhone 15 Pro」→「手机壳」）
        stripped = _strip_fitment(m.title_raw)
        m.product_identity = stripped or m.title_raw
        m.identity_source = "title_raw"
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
    # 标题启发式（仅在原始标题真实出现「适用于/兼容/for」时抽取）
    for txt in (m.title_raw, m.title):
        for mt in _FIT_ZH_RE.finditer(txt):
            v = mt.group(1).strip(" -–—+/")
            if v and len(v) >= 2:
                m.compat.append(CompatFact("fits_model", v, "title_heuristic"))
        for mt in _FIT_EN_RE.finditer(txt):
            v = mt.group(1).strip(" -–—+/")
            if v and len(v) >= 2:
                m.compat.append(CompatFact("fits_model", v, "title_heuristic"))
    # 去重（kind+value+来源同形只留一条）
    seen = set()
    dedup = []
    for f in m.compat:
        k = (f.kind, f.value.lower(), f.source)
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
    if m.identity_source in ("sku", "title_raw"):
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
    compat_vals = [f.value for f in material.compat
                   if f.source == "understanding"][:4] or \
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

# -*- coding: utf-8 -*-
"""pt.facts —— S2a 商品事实提取层（纯函数）。

分层（S2 前置审计交付五的数据契约，2026-10-19 批复）：
  原始事实 origin="raw"   采集器 20 列直录 —— A 级证据基础
  规则事实 origin="rule"  确定性规则转换 —— B 级证据基础
  优化文案 origin="opt"   AI 优化结果 —— C 级证据基础（绝不 A/B）

铁律：
  1. 纯函数：绝不修改输入 record/item（测试 F2 守卫）；内部值一律
     拷贝（元组/列表快照），调用方改返回值不影响输入。
  2. 不猜测：多行互异值（非变体轴）= 冲突记录（Conflict），不是择一；
     采集器没有的列 = 缺失说明，绝不从别处补。
  3. 品牌与兼容品牌严格分离：采集器 20 列没有品牌列——brand 恒无来源
     （notes 说明；人工/店铺声明是唯一合法补充渠道）。兼容品牌来自
     material 适配启发式（确定性问题走 uncertain 列表），绝不反哺 brand。
  4. 变体边界沿用上传引擎既有约定（main.py _amz_build_messages）：
     行数>1 且每行都有「颜色」→ 颜色变体轴；否则多行差异按冲突上报。
     S2a 不另造标准。
  5. 业务敏感列（库存/成本价/运费/币种）只进 internal 留档——fill 层
     绝不把它们映射成亚马逊刊登属性。
  6. 采集器 20 列之外、有值但事实层无映射的列进 unknown_columns 留痕
     ——不猜测语义、不映射。

输入：normalize.normalize_product_record 的 record
  （{pid, raw_rows:[标准化行], opt, sku, cat, …}），可选 item=
  （索引/服务器商品顶层字段，目前只取 model——record 本身不携带）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from pt.material import extract_material

# ---- 采集器列（与 main.py _COLLECTOR_HEADERS / normalize.py 对齐） ----
COL_PARENT_SKU = "父SKU(必填)"
COL_SKU = "SKU"
COL_STOCK = "库存"
COL_CURRENCY = "币种"
COL_COST = "成本价(必填)"
COL_SHIPPING = "运费"
COL_MATERIAL = "材料"
COL_PACKAGING = "包装材料"
COL_LANGUAGE = "语言"
COL_TITLE = "标题(必填)"
COL_COLOR = "颜色"
COL_BULLETS = ("要点1", "要点2", "要点3", "要点4", "要点5")
COL_DESC = "简介"
COL_IMAGES = "产品图"
COL_DETAIL_IMAGES = "简介图"
COL_REF_URL = "参考网址"

# 事实层认识的全部采集器列（其余有值列 → unknown_columns）
_KNOWN_COLS = frozenset(
    (COL_PARENT_SKU, COL_SKU, COL_STOCK, COL_CURRENCY, COL_COST,
     COL_SHIPPING, COL_MATERIAL, COL_PACKAGING, COL_LANGUAGE, COL_TITLE,
     COL_COLOR, COL_DESC, COL_IMAGES, COL_DETAIL_IMAGES, COL_REF_URL)
    + COL_BULLETS)

# 简单直录列 → Facts 字段名（多行同值才算产品级事实）
_SIMPLE_COLS: tuple[tuple[str, str], ...] = (
    ("title", COL_TITLE),
    ("description", COL_DESC),
    ("material", COL_MATERIAL),
    ("packaging_material", COL_PACKAGING),
    ("language", COL_LANGUAGE),
    ("reference_url", COL_REF_URL),
    ("color", COL_COLOR),
    ("parent_sku", COL_PARENT_SKU),
    ("sku_fact", COL_SKU),     # 列证据独立字段——不覆盖 record 级 f.sku（str）
)

# 业务敏感列（留档不外映；fill 层禁止映射）
_INTERNAL_COLS: tuple[tuple[str, str], ...] = (
    ("stock", COL_STOCK),
    ("cost_price", COL_COST),
    ("shipping_fee", COL_SHIPPING),
    ("currency", COL_CURRENCY),
)

_MAX_UNKNOWN_COLS = 50          # unknown_columns 上限（防脏数据刷屏）
_MAX_CONFLICT_VALUES = 6        # 冲突值样本上限


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


@dataclass(frozen=True)
class FactValue:
    """单个事实值 + 完整出处（fill 层据此生成 source_type/reference）。"""
    value: Any
    origin: str                 # raw | rule | opt
    source: str                 # ascii 源标识（→ pt_attribute.source_type）
    reference: str              # 人读出处（→ source_reference，≤500 由模型截）
    rows: tuple[int, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Conflict:
    """同一字段多行互异（非变体轴）——不择一，交人工。"""
    field: str
    values: tuple[str, ...]
    rows: tuple[int, ...]
    note: str = ""


@dataclass(frozen=True)
class VariantRow:
    """一个变体行（行号 + 该行 SKU/父SKU/颜色 + 差异列快照）。"""
    index: int
    sku: str
    parent_sku: str
    color: str
    values: tuple[tuple[str, str], ...]   # 该行非空差异列（含颜色）


@dataclass
class Facts:
    pid: str = ""
    sku: str = ""
    cat: str = ""
    n_rows: int = 0
    # 原始直录（A 级基础）
    title: FactValue | None = None
    bullets: FactValue | None = None          # value=tuple[str,…]
    description: FactValue | None = None
    material: FactValue | None = None
    packaging_material: FactValue | None = None
    language: FactValue | None = None
    reference_url: FactValue | None = None
    color: FactValue | None = None            # 变体轴时为 None（逐行不同）
    model: FactValue | None = None            # 索引 model 字段（raw）
    parent_sku: FactValue | None = None       # raw；变体缺列时 rule 回退
    sku_fact: FactValue | None = None         # 列级 SKU 证据（f.sku=record 级串）
    images: FactValue | None = None           # value=tuple[url,…] 合并去重
    # AI 优化文案（C 级基础——绝不 A/B）
    opt_title: FactValue | None = None
    opt_bullets: FactValue | None = None      # value=tuple[str,…]
    opt_description: FactValue | None = None
    # 品牌与兼容（严格分离）
    brand: FactValue | None = None            # S2a 恒 None（见 notes）
    compatible_brands: FactValue | None = None    # value=tuple[str,…]（rule）
    fits_models: FactValue | None = None          # value=tuple[str,…]（rule）
    fits_parts: FactValue | None = None           # value=tuple[str,…]（rule）
    uncertain_compat: tuple[str, ...] = ()        # 边界不确定的适配值（待人工）
    # 变体 / 冲突 / 留痕
    variants: tuple[VariantRow, ...] = ()
    variant_axis: tuple[str, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    unknown_columns: tuple[str, ...] = ()
    internal: dict[str, FactValue] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def conflict_fields(self) -> set[str]:
        return {c.field for c in self.conflicts}


def _col_hits(rows: list[dict], col: str) -> list[tuple[int, str]]:
    """某列全部非空值 [(行号, 值)]（保持行序）。"""
    out: list[tuple[int, str]] = []
    for i, r in enumerate(rows):
        v = _s(r.get(col))
        if v:
            out.append((i, v))
    return out


def _single_fact(rows: list[dict], col: str, key: str,
                  facts: "Facts", *, quiet: bool = False
                  ) -> FactValue | None:
    """简单直录列：全行唯一非空值 → raw 事实；互异 → 冲突上报。

    quiet=True（业务敏感列）：互异也不报冲突——库存/成本类列各行不同
    属正常业务数据，不是刊登事实冲突。
    """
    hits = _col_hits(rows, col)
    if not hits:
        return None
    uniq = list(dict.fromkeys(v for _, v in hits))
    if len(uniq) == 1:
        return FactValue(
            value=uniq[0], origin="raw", source=f"column.{key}",
            reference=f"采集器列「{col}」行"
                      f"{','.join(str(i) for i, _ in hits)}")
    if not quiet:
        facts.conflicts = facts.conflicts + (Conflict(
            field=key, values=tuple(uniq[:_MAX_CONFLICT_VALUES]),
            rows=tuple(i for i, _ in hits),
            note=f"「{col}」多行互异（{len(uniq)} 个值）——不择一，交人工"),)
    return None


def extract_facts(record: dict | None, *,
                  item: dict | None = None) -> Facts:
    """record（normalize_product_record 输出）→ Facts。纯函数。"""
    record = record if isinstance(record, dict) else {}
    snap = copy.deepcopy(record)      # 只读快照——输入绝不被改
    rows = [r for r in (snap.get("raw_rows") or [])
            if isinstance(r, dict)]
    opt = snap.get("opt") if isinstance(snap.get("opt"), dict) else None
    f = Facts(pid=_s(snap.get("pid")), sku=_s(snap.get("sku")),
              cat=_s(snap.get("cat")), n_rows=len(rows))
    notes: list[str] = []

    # ---- 变体轴判定（上传引擎既有约定：行数>1 且每行都有颜色） ----
    is_variant = len(rows) > 1 and all(_s(r.get(COL_COLOR)) for r in rows)
    if len(rows) > 1 and not is_variant:
        notes.append("多行但不是每行都有「颜色」——不构成颜色变体轴，"
                     "差异列按冲突上报（与上传引擎判定一致）")
    f.variant_axis = ("颜色",) if is_variant else ()

    # ---- 简单直录列 ----
    for key, col in _SIMPLE_COLS:
        if is_variant and key in ("color", "sku_fact"):
            continue        # 变体轴：颜色/SKU 逐行不同，进 variants
                           # （产品级 sku 用 record.sku，见 f.sku）
        setattr(f, key, _single_fact(rows, col, key, f))
    if is_variant:
        # 产品级颜色=冲突语义（逐行不同）——记录成 Conflict 供 fill 出 D 行
        hits = _col_hits(rows, COL_COLOR)
        uniq = list(dict.fromkeys(v for _, v in hits))
        f.conflicts = f.conflicts + (Conflict(
            field="color", values=tuple(uniq[:_MAX_CONFLICT_VALUES]),
            rows=tuple(i for i, _ in hits),
            note="变体轴：颜色逐行不同——按 SKU 级填充，产品级不出单值"),)

    # ---- 父 SKU：变体缺列时按上传引擎约定回退 record.sku（rule/B 级） ----
    if f.parent_sku is None and is_variant and f.sku:
        f.parent_sku = FactValue(
            value=f.sku, origin="rule", source="rule.parent_from_sku",
            reference=f"采集器无「{COL_PARENT_SKU}」值，按上传引擎约定"
                      f"回退商品 SKU（record.sku={f.sku}）",
            note="规则回退——与 _amz_build_messages parent_sku 逻辑一致")

    # ---- 五点（多行合并去重保序；单行直录） ----
    bl: list[str] = []
    bl_rows: list[int] = []
    for i, r in enumerate(rows):
        for k in COL_BULLETS:
            v = _s(r.get(k))
            if v and v not in bl:
                bl.append(v)
                bl_rows.append(i)
    if bl:
        f.bullets = FactValue(
            value=tuple(bl), origin="raw", source="column.bullets",
            reference=f"采集器列「要点1-5」行"
                      f"{','.join(str(i) for i in dict.fromkeys(bl_rows))}"
            + ("（多行合并去重）" if len(rows) > 1 else ""))

    # ---- 图片（产品图+简介图 合并去重保序；http 数量留痕不过滤） ----
    imgs: list[str] = []
    img_rows: list[int] = []
    for i, r in enumerate(rows):
        for col in (COL_IMAGES, COL_DETAIL_IMAGES):
            for u in str(r.get(col) or "").split():
                if u.startswith(("http://", "https://")) and u not in imgs:
                    imgs.append(u)
                    img_rows.append(i)
    if imgs:
        n_http = sum(1 for u in imgs if u.startswith("http://"))
        f.images = FactValue(
            value=tuple(imgs), origin="raw", source="column.images",
            reference=f"采集器列「产品图+简介图」行"
                      f"{','.join(str(i) for i in dict.fromkeys(img_rows))}"
                      "（合并去重，第 1 张=主图）",
            note=(f"{n_http} 个 http:// 链接（上传引擎仅收 https——"
                  "事实层如实保留）" if n_http else ""))

    # ---- 型号：record 不携带，来自 item（索引/服务器商品顶层） ----
    mv = _s((item or {}).get("model"))
    if mv:
        f.model = FactValue(value=mv, origin="raw", source="index.model",
                            reference="商品索引 model 字段（服务器 /prod_list"
                                      " 顶层）")

    # ---- AI 优化文案（origin=opt；fill 层只能给 C 级） ----
    if opt:
        if _s(opt.get("title")):
            f.opt_title = FactValue(value=_s(opt.get("title")), origin="opt",
                                    source="opt.title",
                                    reference="AI 优化结果 opt.title")
        ob = [str(b) for b in (opt.get("bullets") or []) if _s(b)]
        if ob:
            f.opt_bullets = FactValue(value=tuple(ob), origin="opt",
                                      source="opt.bullets",
                                      reference="AI 优化结果 opt.bullets")
        if _s(opt.get("description")):
            f.opt_description = FactValue(
                value=_s(opt.get("description")), origin="opt",
                source="opt.description", reference="AI 优化结果"
                                                   " opt.description")

    # ---- 品牌与兼容品牌（复用 material 适配启发式；绝不反哺 brand） ----
    f.brand = None
    notes.append("采集器 20 列无品牌字段——brand 无来源；兼容品牌绝不充数，"
                 "唯一合法补充=人工录入/店铺声明（S2a 未接店铺声明机制）")
    try:
        m = extract_material(f.pid or "facts", snap, None)
        for kind, dest in (("compatible_brand", "compatible_brands"),
                           ("fits_model", "fits_models"),
                           ("fits_part", "fits_parts")):
            vals = [c.value for c in m.compat
                    if c.kind == kind and c.certain]
            if vals:
                setattr(f, dest, FactValue(
                    value=tuple(vals), origin="rule",
                    source="material.compat",
                    reference=f"标题/文案适配语启发式"
                              f"（material._fitment_analysis，{kind}）",
                    note=f"{len(vals)} 条（确定性剥离）"))
        unc = [c.value for c in m.compat if not c.certain]
        f.uncertain_compat = tuple(unc)
        if unc:
            notes.append(f"{len(unc)} 条适配值边界不确定（原文保留未剥离）"
                         "——待人工确认，不作确定事实")
    except Exception:              # noqa: BLE001 —— 适配启发式失败不拦事实层
        notes.append("兼容品牌启发式提取失败——compat 事实缺省（不影响其余"
                     "事实）")

    # ---- 变体行快照（差异列 = 多行间互异的已知列） ----
    if is_variant:
        diff_cols: list[str] = []
        for col in sorted(_KNOWN_COLS - {COL_SKU, COL_PARENT_SKU}):
            vals = [_s(r.get(col)) for r in rows]
            nonempty = [v for v in vals if v]
            if nonempty and len(set(nonempty)) > 1:
                diff_cols.append(col)
        vs: list[VariantRow] = []
        for i, r in enumerate(rows):
            vs.append(VariantRow(
                index=i, sku=_s(r.get(COL_SKU)),
                parent_sku=_s(r.get(COL_PARENT_SKU)),
                color=_s(r.get(COL_COLOR)),
                values=tuple((c, _s(r.get(c))) for c in diff_cols
                             if _s(r.get(c)))))
        f.variants = tuple(vs)

    # ---- 业务敏感列留档（互异不报冲突——业务列非刊登事实） ----
    for key, col in _INTERNAL_COLS:
        fv = _single_fact(rows, col, key, f, quiet=True)
        if fv is not None:
            f.internal[key] = fv

    # ---- 未知列留痕（有值但无映射——不猜测语义） ----
    unknown: list[str] = []
    for r in rows:
        for k in r:
            if k.startswith("_") or k in _KNOWN_COLS or k in unknown:
                continue
            if _s(r.get(k)):
                unknown.append(str(k))
    f.unknown_columns = tuple(sorted(unknown)[:_MAX_UNKNOWN_COLS])
    if unknown:
        notes.append(f"{min(len(unknown), _MAX_UNKNOWN_COLS)} 个有值列事实层"
                     "无映射（不猜测语义）：" + "、".join(f.unknown_columns))

    f.notes = tuple(notes)
    return f

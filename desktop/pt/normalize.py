# -*- coding: utf-8 -*-
"""pt.normalize —— 商品行标准化（D1 修复：统一数据适配层）。

字段流（任务一.1 审计结论）：
  服务器 /prod_list(pid) → prod{pid,sku,cat,opt,title,raw[n],…}
    raw[i] 有三种历史形状：
      A 真实服务器形状：顶层英文键（row_number/sku/parent_sku/title/
        bullets[]/description/image_urls[]/detail_image_urls[]/language/
        raw_data{…}），中文采集器 20 列嵌在 raw_data 里；
      B 历史测试/桩形状：顶层直接就是中文 20 列（mock 与旧桩喂的形状）；
      C 混合/残缺：两者都有或都缺一部分（类型异常按「缺」处理，绝不抛）。
  pt_api._product_record → 本模块 normalize_product_record → record
  → TaskEngine.load_product → material.extract_material（只认行顶层
  中文 20 列键）。

统一出口（任务一.2）：每行顶层 = 采集器 20 列中文键 + 全部 raw_data
原生键；原始行整体挂 "_orig" 供追踪（任务一.5：原始数据保留，下游
统一消费标准化结构）。

来源优先级（任务一.3，按字段逐列合并、非整行二选一）：
  raw_data（采集器原始层）> 顶层英文键（服务器归一层）> 顶层中文键
  （历史形状，等同原始层直接通过）。冲突时原始层赢——英文键是服务器
  从 raw_data 派生的归一结果，不是新事实。

不丢失保证（任务一.4）：pid/sku/cat/opt 在 record 顶层原样保留；
行内非采集器键（language/row_number/…）留在 "_orig"；店铺隔离信息
（store_id/marketplace）不经 record——由 pt_api/引擎层独立携带，
本模块不触碰。

生产路径与 mock 测试同一个函数（任务一.6）：pt_api._product_record
是唯一 record 装配点，live/mock 的 prod_list 响应都走这里。
幂等：已标准化行（"_normalized" 标记）原样返回（防重复套娃）。
"""

from __future__ import annotations

import copy
from typing import Any

# 采集器列（与 material.COL_* 对齐；只映射 material 消费的列）
COL_TITLE = "标题(必填)"
COL_SKU = "SKU"
COL_PARENT_SKU = "父SKU(必填)"
COL_BULLETS = ("要点1", "要点2", "要点3", "要点4", "要点5")
COL_DESC = "简介"
COL_IMAGES = "产品图"
COL_DETAIL_IMAGES = "简介图"

# 顶层英文键 → 采集器列（服务器归一层 → 原始层键名）
_EN_TO_COL = {
    "title": COL_TITLE,
    "sku": COL_SKU,
    "parent_sku": COL_PARENT_SKU,
    "description": COL_DESC,
    "image_urls": COL_IMAGES,
    "detail_image_urls": COL_DETAIL_IMAGES,
}

_NORMALIZED_MARK = "_normalized"
_NORMALIZED_VER = 1


def _as_str(v: Any) -> str:
    """None/数字/带空白的值 → 干净字符串；绝不抛。"""
    if v is None:
        return ""
    return str(v).strip()


def _as_str_list(v: Any) -> list[str]:
    """list[str] 容错：None→[]；单字符串→[str]（类型异常不丢数据）；
    列表内非串元素逐个 str()。"""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (list, tuple)):
        return [_as_str(x) for x in v if _as_str(x)]
    return [_as_str(v)] if _as_str(v) else []


def normalize_row(row: dict) -> dict:
    """单行标准化：三形状 → 统一采集器中文列（顶层）。

    返回行 = {**raw_data 原生键, 缺列由英文键补, "_orig": 原行深拷贝,
    "_shape": 形状标记, "_anomalies": [类型异常记录], "_normalized": 1}。
    """
    if not isinstance(row, dict):
        raise TypeError(f"normalize_row 只接受 dict，得到 {type(row).__name__}")

    # 幂等：已标准化行原样返回（防重复套娃 _orig 嵌套）
    if row.get(_NORMALIZED_MARK) == _NORMALIZED_VER:
        return dict(row)

    anomalies: list[str] = []
    raw_data = row.get("raw_data")
    if raw_data is not None and not isinstance(raw_data, dict):
        anomalies.append(f"raw_data:{type(raw_data).__name__}")
        raw_data = None

    # 形状标记：A=en_rawdata / B=zh_toplevel / C=mixed / en_only
    _ZH_COLS = (COL_TITLE, COL_SKU, COL_PARENT_SKU, COL_DESC, COL_IMAGES,
                COL_DETAIL_IMAGES) + COL_BULLETS
    has_en = any(k in row for k in _EN_TO_COL)
    has_zh = any(k in row for k in _ZH_COLS)
    shape = ("mixed" if raw_data and has_zh else
             "en_rawdata" if raw_data else
             "mixed" if has_en and has_zh else
             "zh_toplevel" if has_zh else
             "en_only" if has_en else "unknown")

    # 基底：A 形状=raw_data 原生键全量保留（成本价/库存等业务列不丢）；
    # B/C 形状（无 raw_data）=行自身（顶层中文键直接通过，英文键随后
    # 逐列映射补缺，不丢任何既有键）。C 形状（raw_data+顶层中文键并
    # 存）时顶层中文键只补 raw_data 缺的列——原始层仍最高优先。
    if raw_data:
        out: dict[str, Any] = dict(raw_data)
        for col in _ZH_COLS:
            if col in row and not _as_str(out.get(col)):
                out[col] = _as_str(row[col])
    else:
        out = {k: v for k, v in row.items() if k != "raw_data"}

    # 英文归一层 → 逐列补缺（原始层已有的列绝不被覆盖）。图片列例外：
    # 图片是增量信息不是语义冲突——英文层 URL 追加进原始层（去重）。
    for en, col in _EN_TO_COL.items():
        v = row.get(en)
        if v is None:
            continue
        if en in ("image_urls", "detail_image_urls"):
            urls = _as_str_list(v)
            have = {u for u in str(out.get(col) or "").split()
                    if u.startswith(("http://", "https://"))}
            new = [u for u in urls if u not in have]
            if new:
                merged = _as_str(out.get(col))
                out[col] = (merged + " " + " ".join(new)).strip()
            continue
        if col in out and _as_str(out.get(col)):
            continue
        s = _as_str(v)
        if s and not _as_str(out.get(col)):
            out[col] = s

    # bullets → 要点1..5（英文列表 → 中文位置列；已有中文列不覆盖）
    bullets = _as_str_list(row.get("bullets"))
    for i, b in enumerate(bullets[:5]):
        col = COL_BULLETS[i]
        if b and not _as_str(out.get(col)):
            out[col] = b

    # 键类型异常防御：核心列一律转字符串
    for col in (COL_TITLE, COL_SKU, COL_PARENT_SKU, COL_DESC,
                COL_IMAGES, COL_DETAIL_IMAGES):
        if col in out:
            out[col] = _as_str(out[col])

    # SKU 数字型（如 12345 int）转串
    out[COL_SKU] = _as_str(out.get(COL_SKU))
    out[COL_PARENT_SKU] = _as_str(out.get(COL_PARENT_SKU))
    # 契约：核心采集器列恒存在（缺=空串，下游不再 KeyError）
    for col in (COL_TITLE, COL_DESC, COL_IMAGES, COL_DETAIL_IMAGES):
        if col not in out or out[col] is None:
            out[col] = ""
    for col in COL_BULLETS:
        if col not in out or out[col] is None:
            out[col] = ""

    out[_NORMALIZED_MARK] = _NORMALIZED_VER
    out["_shape"] = shape
    out["_anomalies"] = anomalies
    out["_orig"] = copy.deepcopy(row)     # 原始行整行保留（追踪）
    return out


def normalize_product_record(prod: dict | None, pid: str = "") -> dict:
    """prod_list 商品 → material 契约 record（pt_api._product_record 调用）。

    record = {pid, raw_rows:[标准化行], opt, sku, cat,
              _raw_original:[原始行], _row_shapes:[形状]}。
    顶层商品身份/业务字段全保留；raw 非列表按空处理并记异常。
    """
    prod = prod if isinstance(prod, dict) else {}
    raw_rows = prod.get("raw")
    if raw_rows is None:
        raw_rows = []
    elif not isinstance(raw_rows, list):
        raw_rows = []
        prod = dict(prod)
        prod["_raw_anomaly"] = f"raw:{type(prod.get('raw')).__name__}"

    rows = []
    for r in raw_rows:
        if isinstance(r, dict):
            rows.append(normalize_row(r))
        # 非 dict 行剔除（raw 里混入字符串/None 等脏数据不拖垮整体）

    rec = {"pid": _as_str(pid) or _as_str(prod.get("pid")),
           "raw_rows": rows,
           "opt": prod.get("opt") if isinstance(prod.get("opt"), dict)
           else None,
           "sku": _as_str(prod.get("sku")),
           "cat": _as_str(prod.get("cat"))}
    if rows:
        rec["_raw_original"] = [r.get("_orig") for r in rows]
        rec["_row_shapes"] = [r.get("_shape") for r in rows]
    if prod.get("_raw_anomaly"):
        rec["_raw_anomaly"] = prod["_raw_anomaly"]
    return rec

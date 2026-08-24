from __future__ import annotations

import re
from collections.abc import Iterable

import pandas as pd

from .models import FieldMap

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "产品id", "product id", "item id", "asin"),
    "sku": ("sku", "seller sku", "卖家sku", "商品sku", "item sku"),
    "parent_sku": ("父sku", "父 sku", "parent sku", "parent_sku"),
    "child_sku": ("子sku", "子 sku", "child sku", "child_sku"),
    "title": ("标题(必填)", "商品标题", "产品标题", "标题", "product title", "item title", "title"),
    "short_title": ("短标题", "亮点短标题", "short title", "short_title", "item highlights", "highlights"),
    "description": ("简介", "详情描述", "产品描述", "商品描述", "详情", "描述", "product description", "description"),
    "images": (
        "产品图片", "产品图", "商品图片", "图片", "图片链接", "主图", "主图链接",
        "image url", "image urls", "product image", "product images", "main image",
        "main images", "images", "image",
    ),
    "detail_images": ("简介图", "详情图", "详情图片", "描述图片", "detail images", "description images"),
    "reference_url": ("参考网址", "参考链接", "产品链接", "商品链接", "source url", "reference url", "product url"),
    "language": ("语言", "language", "locale"),
    "category": ("分类", "类目", "category", "product category"),
    "brand": ("品牌", "brand", "brand name"),
    "material": ("材料", "材质", "material"),
    "packaging_material": ("包装材料", "包装材质", "packaging material"),
    "color": ("颜色", "色彩", "color", "colour"),
    "weight": ("产品重量", "商品重量", "重量", "净重", "毛重", "weight", "item weight"),
    "dimensions": ("产品尺寸", "商品尺寸", "包装尺寸", "尺寸", "规格", "dimensions", "dimension", "size"),
    "variants": ("变体", "变体信息", "规格变体", "variants", "variation", "variations"),
    "currency": ("币种", "货币", "currency"),
    "price": ("售价", "销售价", "价格", "price", "sale price"),
    "cost": ("成本价(必填)", "成本价", "成本", "cost price", "cost"),
    "stock": ("库存", "库存数量", "stock", "quantity", "inventory"),
}

BULLET_GROUPS = tuple(
    (
        f"要点{i}", f"五点{i}", f"卖点{i}", f"bullet{i}", f"bullet {i}",
        f"bullet point {i}", f"key feature {i}",
    )
    for i in range(1, 6)
)

_IMAGE_URL_RE = re.compile(
    r"https?://[^\s|,;]+(?:\.(?:jpe?g|png|webp|gif|bmp|avif)(?:\?[^\s|,;]*)?|/[^\s|,;]*)",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s|,;]+", re.I)


def normalize(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s_\-（）()【】\[\]:：/\\]+", "", text)
    return text


def find_named_column(columns: Iterable[object], candidates: Iterable[str]) -> str | None:
    cols = [str(c) for c in columns]
    normalized_columns = {normalize(c): c for c in cols}

    # Exact normalized aliases always win.
    for candidate in candidates:
        match = normalized_columns.get(normalize(candidate))
        if match is not None:
            return match

    # Conservative partial matching: avoid matching language-prefixed outputs
    # such as “英语-标题” when the source title column “标题” exists or is absent.
    candidate_norms = [normalize(c) for c in candidates if len(normalize(c)) >= 4]
    for col in cols:
        norm = normalize(col)
        if any(norm.startswith(cand) or norm.endswith(cand) for cand in candidate_norms):
            return col
    return None


def content_score(series: pd.Series, pattern: re.Pattern[str]) -> float:
    values = [str(v).strip() for v in series.tolist() if str(v).strip()]
    if not values:
        return 0.0
    sample = values[:100]
    return sum(bool(pattern.search(v)) for v in sample) / len(sample)


def detect_url_column(
    df: pd.DataFrame,
    aliases: Iterable[str],
    pattern: re.Pattern[str],
    threshold: float,
) -> tuple[str | None, dict[str, float], str]:
    named = find_named_column(df.columns, aliases)
    scores = {str(col): content_score(df[col], pattern) for col in df.columns}
    if named:
        return named, scores, "列名匹配"
    if scores:
        best_col, best_score = max(scores.items(), key=lambda item: item[1])
        if best_score >= threshold:
            return best_col, scores, f"内容识别（{best_score:.0%}）"
    return None, scores, "未识别"


def detect_fields(df: pd.DataFrame) -> tuple[FieldMap, dict[str, object]]:
    images, image_scores, image_method = detect_url_column(
        df, FIELD_ALIASES["images"], _IMAGE_URL_RE, 0.35
    )
    reference_url, url_scores, url_method = detect_url_column(
        df, FIELD_ALIASES["reference_url"], _URL_RE, 0.60
    )

    bullets: list[str] = []
    for group in BULLET_GROUPS:
        col = find_named_column(df.columns, group)
        if col and col not in bullets:
            bullets.append(col)

    detected: dict[str, str | None] = {
        key: find_named_column(df.columns, aliases)
        for key, aliases in FIELD_ALIASES.items()
        if key not in {"images", "reference_url"}
    }
    detected["images"] = images
    detected["reference_url"] = reference_url

    fields = FieldMap(**detected, bullets=tuple(bullets))
    matched_columns = {v for v in detected.values() if v}
    matched_columns.update(bullets)
    unmatched = [str(col) for col in df.columns if str(col) not in matched_columns]

    diagnostics = {
        "column_count": len(df.columns),
        "row_count": len(df),
        "columns": [str(c) for c in df.columns],
        "matched_field_count": sum(bool(v) for v in detected.values()) + (1 if bullets else 0),
        "unmatched_columns": unmatched,
        "image_content_scores": image_scores,
        "url_content_scores": url_scores,
        "image_detection_method": image_method,
        "reference_url_detection_method": url_method,
    }
    return fields, diagnostics

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class FieldMap:
    id: str | None = None
    sku: str | None = None
    parent_sku: str | None = None
    child_sku: str | None = None
    title: str | None = None
    short_title: str | None = None
    description: str | None = None
    bullets: tuple[str, ...] = ()
    images: str | None = None
    detail_images: str | None = None
    reference_url: str | None = None
    language: str | None = None
    category: str | None = None
    brand: str | None = None
    material: str | None = None
    packaging_material: str | None = None
    color: str | None = None
    weight: str | None = None
    dimensions: str | None = None
    variants: str | None = None
    currency: str | None = None
    price: str | None = None
    cost: str | None = None
    stock: str | None = None

    def as_dict(self) -> dict[str, str | tuple[str, ...] | None]:
        return {
            "ID": self.id,
            "SKU": self.sku,
            "父SKU": self.parent_sku,
            "子SKU": self.child_sku,
            "标题": self.title,
            "短标题": self.short_title,
            "五点": self.bullets,
            "详情": self.description,
            "产品图片": self.images,
            "详情图片": self.detail_images,
            "参考网址": self.reference_url,
            "语言": self.language,
            "分类": self.category,
            "品牌": self.brand,
            "材料": self.material,
            "包装材料": self.packaging_material,
            "颜色": self.color,
            "重量": self.weight,
            "尺寸": self.dimensions,
            "变体": self.variants,
            "币种": self.currency,
            "价格": self.price,
            "成本": self.cost,
            "库存": self.stock,
        }


@dataclass(frozen=True)
class ProductRecord:
    row_number: int
    sku: str = ""
    parent_sku: str = ""
    child_sku: str = ""
    title: str = ""
    short_title: str = ""
    bullets: tuple[str, ...] = ()
    description: str = ""
    image_urls: tuple[str, ...] = ()
    detail_image_urls: tuple[str, ...] = ()
    language: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkbookEnvelope:
    filename: str
    raw_bytes: bytes
    dataframe: pd.DataFrame
    sheet_name: str
    fields: FieldMap
    records: tuple[ProductRecord, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)

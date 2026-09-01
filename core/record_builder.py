from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .models import FieldMap, ProductRecord

_SPLIT_URLS_RE = re.compile(r"[\s|,;]+")


def _text(row: pd.Series, column: str | None) -> str:
    if not column:
        return ""
    value = row.get(column, "")
    if value is None:
        return ""
    return str(value).strip()


def _urls(value: str) -> tuple[str, ...]:
    found = []
    for part in _SPLIT_URLS_RE.split(value.strip()):
        if part.lower().startswith(("http://", "https://")) and part not in found:
            found.append(part)
    return tuple(found)


def build_product_records(df: pd.DataFrame, fields: FieldMap) -> tuple[ProductRecord, ...]:
    records: list[ProductRecord] = []
    for index, row in df.iterrows():
        bullet_values = tuple(
            value for column in fields.bullets
            if (value := _text(row, column))
        )
        raw_data: dict[str, Any] = {str(k): v for k, v in row.to_dict().items()}
        records.append(
            ProductRecord(
                row_number=int(index) + 2,
                sku=_text(row, fields.sku),
                parent_sku=_text(row, fields.parent_sku),
                child_sku=_text(row, fields.child_sku),
                title=_text(row, fields.title),
                short_title=_text(row, fields.short_title),
                bullets=bullet_values,
                description=_text(row, fields.description),
                image_urls=_urls(_text(row, fields.images)),
                detail_image_urls=_urls(_text(row, fields.detail_images)),
                language=_text(row, fields.language),
                raw_data=raw_data,
            )
        )
    return tuple(records)

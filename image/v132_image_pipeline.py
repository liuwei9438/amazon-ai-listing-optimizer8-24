from __future__ import annotations

from typing import Any, Mapping

from image.v132_image_pipeline import (
    IMAGE_SEPARATOR,
    download_image,
    image_bytes,
    process_main_image,
    stable_shuffle_secondary,
)
from image.image_storage import upload_main_image_to_cloudinary


def optimize_record_images(
    record: Any,
    *,
    image_column: str | None,
    cloudinary_config: Mapping[str, str] | None,
) -> dict:
    """Run the frozen V1.3.2 image baseline for one product.

    Image failure is deliberately represented as metadata instead of raising
    into the text pipeline. This keeps text optimization successful even if an
    external image URL or Cloudinary is temporarily unavailable.
    """
    images = list(getattr(record, "image_urls", ()) or ())
    sku = str(getattr(record, "sku", "") or getattr(record, "child_sku", "") or "").strip()
    source_row_index = getattr(record, "row_number", None)

    base = {
        "version": "v1.3.2-integrated-adapter-v1",
        "enabled": True,
        "column": image_column or "",
        "source_row_index": source_row_index,
        "status": "skipped",
        "optimized_main_url": "",
        "image_value": "",
        "strategy": "",
        "error": "",
    }

    if not image_column:
        base["error"] = "未识别到产品图片列"
        return base
    if not images:
        base["error"] = "当前产品没有可处理的图片 URL"
        return base

    try:
        ordered = stable_shuffle_secondary(images, sku or str(source_row_index or "product"))
        source_main = ordered[0]
        source_image = download_image(source_main)
        processed, strategy = process_main_image(
            source_image,
            seed_text=f"{sku}|{source_main}",
        )
        optimized_bytes = image_bytes(processed)
        optimized_url = upload_main_image_to_cloudinary(
            optimized_bytes,
            sku or str(source_row_index or "product"),
            source_main,
            cloudinary_config or {},
        )
        ordered[0] = optimized_url

        base.update({
            "status": "success",
            "optimized_main_url": optimized_url,
            "image_value": IMAGE_SEPARATOR.join(ordered),
            "strategy": strategy,
            "original_main_url": source_main,
            "image_count": len(ordered),
        })
        return base
    except Exception as exc:
        base.update({
            "status": "failed",
            "error": str(exc),
        })
        return base

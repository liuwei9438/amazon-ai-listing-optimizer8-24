from __future__ import annotations

from typing import Any

from image.v132_image_pipeline import (
    IMAGE_SEPARATOR,
    download_image,
    image_bytes,
    process_main_image,
    stable_shuffle_secondary,
)
from image.image_storage import upload_main_image


def optimize_record_images(record: Any, profile: dict | None = None) -> dict:
    """Optimize only the main image. Failure is returned as data, never raised."""
    urls = list(getattr(record, "image_urls", ()) or ())
    sku = str(getattr(record, "sku", "") or "")
    row_number = getattr(record, "row_number", None)
    base = {
        "version": "v1.3.2-integrated",
        "source_row_index": row_number,
        "sku": sku,
        "original_images": urls,
        "optimized_images": urls,
        "main_image_original": urls[0] if urls else "",
        "main_image_optimized": "",
        "status": "skipped" if not urls else "pending",
        "transform": "",
        "error": "",
    }
    if not urls:
        base["error"] = "NO_IMAGE_URL"
        return base

    try:
        source_url = urls[0]
        img = download_image(source_url)
        optimized, transform = process_main_image(img, seed_text=sku or source_url)
        new_url = upload_main_image(image_bytes(optimized), sku, source_url)
        images = [new_url, *urls[1:]]
        images = stable_shuffle_secondary(images, sku or source_url)
        base.update({
            "status": "success",
            "main_image_optimized": new_url,
            "optimized_images": images,
            "transform": transform,
            "joined_images": IMAGE_SEPARATOR.join(images),
        })
        return base
    except Exception as exc:
        base.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return base

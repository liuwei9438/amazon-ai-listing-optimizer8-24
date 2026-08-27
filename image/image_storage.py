from __future__ import annotations

import hashlib
import io
import re
from typing import Mapping


def cloudinary_ready(config: Mapping[str, str] | None) -> bool:
    config = config or {}
    required = ("cloud_name", "api_key", "api_secret")
    return all(bool(str(config.get(k, "")).strip()) for k in required)


def upload_main_image_to_cloudinary(
    data: bytes,
    sku: str,
    source_url: str,
    config: Mapping[str, str],
) -> str:
    """Upload the processed main image and return a persistent HTTPS URL.

    Public IDs are deterministic so the same SKU/source image overwrites the
    previous optimized asset instead of creating unlimited duplicates.
    """
    if not cloudinary_ready(config):
        raise RuntimeError("未配置 Cloudinary Secrets")

    try:
        import cloudinary
        import cloudinary.uploader
    except ImportError as exc:
        raise RuntimeError("缺少 cloudinary 依赖，请安装 cloudinary") from exc

    cloudinary.config(
        cloud_name=str(config["cloud_name"]).strip(),
        api_key=str(config["api_key"]).strip(),
        api_secret=str(config["api_secret"]).strip(),
        secure=True,
    )

    safe_sku = re.sub(r"[^A-Za-z0-9_-]+", "_", sku or "product").strip("_") or "product"
    source_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:10]
    public_id = f"{safe_sku}_{source_hash}"

    result = cloudinary.uploader.upload(
        io.BytesIO(data),
        resource_type="image",
        asset_folder="optimized_main_images",
        public_id=public_id,
        overwrite=True,
        unique_filename=False,
        format="jpg",
        invalidate=True,
        tags=["amazon-main-image", safe_sku],
    )

    secure_url = str(result.get("secure_url", "")).strip()
    if not secure_url.startswith("https://"):
        raise RuntimeError("Cloudinary 未返回有效 HTTPS 图片链接")
    return secure_url

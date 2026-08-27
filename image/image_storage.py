from __future__ import annotations

import hashlib
import io
import os
import re


def _secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        import streamlit as st
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:
        return ""


def cloudinary_ready() -> bool:
    return all(_secret(k) for k in (
        "CLOUDINARY_CLOUD_NAME",
        "CLOUDINARY_API_KEY",
        "CLOUDINARY_API_SECRET",
    ))


def upload_main_image(data: bytes, sku: str, source_url: str) -> str:
    if not cloudinary_ready():
        raise RuntimeError("未配置 Cloudinary Secrets")
    import cloudinary
    import cloudinary.uploader

    cloudinary.config(
        cloud_name=_secret("CLOUDINARY_CLOUD_NAME"),
        api_key=_secret("CLOUDINARY_API_KEY"),
        api_secret=_secret("CLOUDINARY_API_SECRET"),
        secure=True,
    )
    safe_sku = re.sub(r"[^A-Za-z0-9_-]+", "_", sku or "product").strip("_") or "product"
    source_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:10]
    result = cloudinary.uploader.upload(
        io.BytesIO(data),
        resource_type="image",
        asset_folder="optimized_main_images",
        public_id=f"{safe_sku}_{source_hash}",
        overwrite=True,
        unique_filename=False,
        format="jpg",
        invalidate=True,
        tags=["amazon-main-image", safe_sku],
    )
    url = str(result.get("secure_url", "")).strip()
    if not url.startswith("https://"):
        raise RuntimeError("Cloudinary 未返回有效 HTTPS 图片链接")
    return url

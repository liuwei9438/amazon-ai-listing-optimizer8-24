"""V1.3.2 IMAGE BASELINE — FROZEN.

This module is intentionally not connected in V2.2.1. It preserves the tested
V1.3.2 image-processing contract so later versions can integrate it without
rewriting it. Do not modify unless the user explicitly requests image changes.
"""
from __future__ import annotations

import hashlib
import io
import random
from typing import Any

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

IMAGE_SIZE = (1600, 1600)
IMAGE_SEPARATOR = " | "


def split_images(value: Any) -> list[str]:
    import re
    if value is None:
        return []
    parts = [p.strip() for p in re.split(r"\s*\|\s*|\n+", str(value)) if p.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for url in parts:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def stable_shuffle_secondary(images: list[str], sku: str) -> list[str]:
    if len(images) <= 2:
        return images
    first, rest = images[0], images[1:]
    seed_text = sku or first
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    rng.shuffle(rest)
    return [first, *rest]


def download_image(url: str) -> Image.Image:
    response = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content)).convert("RGBA")


def _near_white_bbox(img: Image.Image):
    rgb = img.convert("RGB")
    mask = rgb.point(lambda value: 255 if value < 242 else 0).convert("L")
    return mask.getbbox()


def _prepare_product(img: Image.Image) -> Image.Image:
    background = Image.new("RGBA", img.size, "white")
    background.alpha_composite(img)
    rgb = background.convert("RGB")
    bbox = _near_white_bbox(rgb)
    if bbox:
        left, top, right, bottom = bbox
        pad_x = max(8, int((right - left) * 0.035))
        pad_y = max(8, int((bottom - top) * 0.035))
        rgb = rgb.crop((
            max(0, left - pad_x), max(0, top - pad_y),
            min(rgb.width, right + pad_x), min(rgb.height, bottom + pad_y),
        ))
    return rgb


def _enhance_clarity(img: Image.Image) -> Image.Image:
    img = ImageOps.autocontrast(img, cutoff=0.35)
    img = ImageEnhance.Contrast(img).enhance(1.07)
    img = ImageEnhance.Brightness(img).enhance(1.015)
    img = ImageEnhance.Color(img).enhance(1.025)
    img = ImageEnhance.Sharpness(img).enhance(1.40)
    return img.filter(ImageFilter.UnsharpMask(radius=1.55, percent=145, threshold=2))


def process_main_image(img: Image.Image, seed_text: str = "") -> tuple[Image.Image, str]:
    product = _prepare_product(img)
    w, h = product.size
    aspect = max(w, h) / max(1, min(w, h))
    seed = int(hashlib.sha256((seed_text or f"{w}x{h}").encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    if aspect <= 1.85:
        angle = rng.choice([-10, -8, 8, 10])
    elif aspect <= 2.8:
        angle = rng.choice([-6, -5, 5, 6])
    else:
        angle = rng.choice([-4, 4])
    product = product.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    bbox = _near_white_bbox(product)
    if bbox:
        product = product.crop(bbox)
    product = ImageOps.contain(product, (1460, 1460), Image.Resampling.LANCZOS)
    product = _enhance_clarity(product)
    canvas = Image.new("RGB", IMAGE_SIZE, "white")
    canvas.paste(product, ((1600 - product.width) // 2, (1600 - product.height) // 2))
    return canvas, f"旋转{angle}°+高清增强"


def image_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=95, optimize=True)
    return buffer.getvalue()

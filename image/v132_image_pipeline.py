"""V1.3.2 IMAGE BASELINE — FROZEN.

Restored from the previously validated V1.3.2 image workflow. Integration code
may call this module, but the transformation rules should not be changed unless
the image baseline is intentionally revised.
"""
from __future__ import annotations

import hashlib
import io
import random
import re
from typing import Any

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

IMAGE_SIZE = (1600, 1600)
IMAGE_SEPARATOR = " | "


def split_images(value: Any) -> list[str]:
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
        return list(images)
    first, rest = images[0], list(images[1:])
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


def _edge_density(img: Image.Image) -> float:
    """Small heuristic used to avoid mirroring images that probably contain text/labels."""
    small = ImageOps.contain(img.convert("L"), (420, 420), Image.Resampling.LANCZOS)
    edges = small.filter(ImageFilter.FIND_EDGES)
    hist = edges.histogram()
    strong = sum(hist[70:])
    total = max(1, small.width * small.height)
    return strong / total


def _symmetry_score(img: Image.Image) -> float:
    """Lower values mean the object is more horizontally symmetric and safer to mirror."""
    small = ImageOps.contain(img.convert("L"), (360, 360), Image.Resampling.LANCZOS)
    flipped = ImageOps.mirror(small)
    from PIL import ImageChops
    delta = ImageChops.difference(small, flipped)
    hist = delta.histogram()
    weighted = sum(i * count for i, count in enumerate(hist))
    return weighted / max(1, 255 * small.width * small.height)


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
    """Create a differentiated 1600x1600 white-background main image.

    The same product/source gets deterministic treatment. Safe images can use
    mirroring; text/label-heavy images avoid mirroring. Otherwise a shape-aware
    rotation is used. Product pixels are not redrawn or generatively altered.
    """
    product = _prepare_product(img)
    w, h = product.size
    aspect = max(w, h) / max(1, min(w, h))
    occupancy = (w * h) / max(1, img.width * img.height)
    edge_density = _edge_density(product)
    symmetry = _symmetry_score(product)

    seed = int(hashlib.sha256((seed_text or f"{w}x{h}").encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    likely_text = edge_density > 0.115
    mirror_safe = (not likely_text) and (symmetry < 0.19 or edge_density < 0.075)

    if aspect <= 1.85:
        angle = rng.choice([-10, -8, 8, 10])
    elif aspect <= 2.8:
        angle = rng.choice([-6, -5, 5, 6])
    else:
        angle = rng.choice([-4, 4])

    rotation_awkward = occupancy > 0.78 or (aspect > 3.2 and abs(angle) > 4)
    use_mirror = rotation_awkward and mirror_safe
    if mirror_safe and rng.random() < 0.30:
        use_mirror = True

    transform_parts = []
    if use_mirror:
        product = ImageOps.mirror(product)
        transform_parts.append("镜像")
        if aspect < 2.5 and rng.random() < 0.35:
            tiny = rng.choice([-3, 3])
            product = product.rotate(tiny, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            transform_parts.append(f"旋转{tiny}°")
    else:
        product = product.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
        transform_parts.append(f"旋转{angle}°")

    bbox = _near_white_bbox(product)
    if bbox:
        product = product.crop(bbox)
    product = ImageOps.contain(product, (1460, 1460), Image.Resampling.LANCZOS)
    product = _enhance_clarity(product)

    canvas = Image.new("RGB", IMAGE_SIZE, "white")
    x = (IMAGE_SIZE[0] - product.width) // 2
    y = (IMAGE_SIZE[1] - product.height) // 2
    canvas.paste(product, (x, y))
    return canvas, "+".join(transform_parts) + "+高清增强"


def image_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, optimize=True)
    return buf.getvalue()

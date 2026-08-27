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


def _near_white_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Return the bounding box of the visible product on a white/near-white background."""
    rgb = img.convert("RGB")
    # A pixel belongs to the product when at least one channel is clearly below white.
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
    diff = ImageOps.grayscale(Image.blend(small.convert("RGB"), flipped.convert("RGB"), 0.5))
    # Compare original and flipped through a difference image without NumPy.
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
        # Keep a small safety margin around the detected product.
        left, top, right, bottom = bbox
        pad_x = max(8, int((right - left) * 0.035))
        pad_y = max(8, int((bottom - top) * 0.035))
        bbox = (
            max(0, left - pad_x), max(0, top - pad_y),
            min(rgb.width, right + pad_x), min(rgb.height, bottom + pad_y),
        )
        rgb = rgb.crop(bbox)
    return rgb


def _enhance_clarity(img: Image.Image) -> Image.Image:
    # Stronger than P1.2, but still avoids inventing product details.
    img = ImageOps.autocontrast(img, cutoff=0.35)
    img = ImageEnhance.Contrast(img).enhance(1.07)
    img = ImageEnhance.Brightness(img).enhance(1.015)
    img = ImageEnhance.Color(img).enhance(1.025)
    img = ImageEnhance.Sharpness(img).enhance(1.40)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.55, percent=145, threshold=2))
    return img


def process_main_image(img: Image.Image, seed_text: str = "") -> tuple[Image.Image, str]:
    """Create a visibly differentiated 1600x1600 white-background main image.

    The strategy is deterministic for the same SKU/source. It prefers a tasteful angle
    change when the product shape allows it. If a stronger rotation would look awkward,
    it uses a horizontal mirror only when the image appears safe (low text-like edge
    density or high left/right symmetry). Product facts are never generated or redrawn.
    """
    product = _prepare_product(img)
    w, h = product.size
    aspect = max(w, h) / max(1, min(w, h))
    occupancy = (w * h) / max(1, img.width * img.height)
    edge_density = _edge_density(product)
    symmetry = _symmetry_score(product)

    seed = int(hashlib.sha256((seed_text or f"{w}x{h}").encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)

    # Text/label-heavy images should not be mirrored. Elongated items get a smaller angle.
    likely_text = edge_density > 0.115
    mirror_safe = (not likely_text) and (symmetry < 0.19 or edge_density < 0.075)

    if aspect <= 1.85:
        angle = rng.choice([-10, -8, 8, 10])
    elif aspect <= 2.8:
        angle = rng.choice([-6, -5, 5, 6])
    else:
        angle = rng.choice([-4, 4])

    # Large, already tightly composed objects can look awkward with rotation.
    rotation_awkward = occupancy > 0.78 or (aspect > 3.2 and abs(angle) > 4)
    use_mirror = rotation_awkward and mirror_safe
    # Create more visible differentiation for a subset of safe images.
    if mirror_safe and rng.random() < 0.30:
        use_mirror = True

    transform_parts = []
    if use_mirror:
        product = ImageOps.mirror(product)
        transform_parts.append("镜像")
        # Mirror-only is visually clear; add only a tiny angle for natural composition.
        if aspect < 2.5 and rng.random() < 0.35:
            tiny = rng.choice([-3, 3])
            product = product.rotate(tiny, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            transform_parts.append(f"旋转{tiny}°")
    else:
        product = product.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
        transform_parts.append(f"旋转{angle}°")

    # Re-crop the white corners created by rotation, then fit to a larger product area.
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


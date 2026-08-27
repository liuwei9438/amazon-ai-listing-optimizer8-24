from __future__ import annotations

import pandas as pd

from services.listing_exporter import ListingExporter


def _profile(image_result):
    return {
        "source_identity": {"source_row_index": 2, "sku": "SKU-1"},
        "generated_title": {"title": "Optimized Title"},
        "image_result": image_result,
    }


def test_exporter_writes_successful_optimized_image(tmp_path):
    df = pd.DataFrame([{
        "SKU": "SKU-1",
        "产品图片": "https://old/main.jpg | https://old/second.jpg",
    }])
    out = ListingExporter.export(df, [_profile({
        "status": "success",
        "column": "产品图片",
        "image_value": "https://cdn/new.jpg | https://old/second.jpg",
    })])
    exported = pd.read_excel(out)
    assert exported.loc[0, "产品图片"] == "https://cdn/new.jpg | https://old/second.jpg"


def test_exporter_preserves_original_image_when_image_failed():
    original = "https://old/main.jpg | https://old/second.jpg"
    df = pd.DataFrame([{"SKU": "SKU-1", "产品图片": original}])
    out = ListingExporter.export(df, [_profile({
        "status": "failed",
        "column": "产品图片",
        "error": "network error",
    })])
    exported = pd.read_excel(out)
    assert exported.loc[0, "产品图片"] == original


def test_v132_main_image_is_1600_square():
    from PIL import Image
    from image.v132_image_pipeline import process_main_image

    source = Image.new("RGBA", (800, 600), "white")
    # add a dark product-like rectangle so crop logic has content
    for x in range(200, 600):
        for y in range(180, 420):
            source.putpixel((x, y), (80, 80, 80))
    result, strategy = process_main_image(source, seed_text="SKU-1")
    assert result.size == (1600, 1600)
    assert "高清增强" in strategy


def test_image_pipeline_failure_is_metadata_not_exception(monkeypatch):
    from types import SimpleNamespace
    import image.image_pipeline as pipeline

    record = SimpleNamespace(
        image_urls=("https://example.invalid/main.jpg",),
        sku="SKU-1",
        child_sku="",
        row_number=2,
    )
    monkeypatch.setattr(pipeline, "download_image", lambda url: (_ for _ in ()).throw(RuntimeError("download failed")))
    result = pipeline.optimize_record_images(
        record,
        image_column="产品图片",
        cloudinary_config={},
    )
    assert result["status"] == "failed"
    assert "download failed" in result["error"]

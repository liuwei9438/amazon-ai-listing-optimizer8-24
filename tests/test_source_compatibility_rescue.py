from core.cross_layer_fact_guard import CrossLayerFactGuard


def _profile(title, notes=None, detected=None):
    return {
        "brand_info": {
            "detected_brands": detected or [],
            "third_party_brands": [],
        },
        "compatibility": {
            "brands": [],
            "models": [],
            "part_numbers": [],
            "compatibility_notes": notes or [],
        },
        "identifiers": {
            "model_numbers": [],
            "part_numbers": [],
            "unknown_codes": [],
        },
        "fact_lock": {
            "compatible_models": [],
            "part_numbers": [],
        },
        "search_strategy": {},
        "product_knowledge": {
            "relationship": {},
        },
        "source_fact_ledger": {
            "source_snapshot": {
                "title": title,
                "description": "",
                "bullets": [],
            },
        },
    }


def _empty_normalized():
    return {
        "identity": {"text": "Replacement Part"},
        "compatibility": {
            "phrase": "",
            "brands": [],
        },
        "models": {
            "all": [],
            "primary": "",
            "secondary": [],
        },
    }


def test_explicit_compatibility_note_can_rescue_source_supported_brand():
    p = _profile(
        "2 Pcs Buffing Wheels for KDT Edge Banding Machine",
        notes=["Compatible with KDT Edge Banding Machine"],
    )

    result = CrossLayerFactGuard.reconcile(
        p,
        _empty_normalized(),
    )

    assert result["compatibility"]["brands"] == ["KDT"]


def test_detected_brand_requires_explicit_for_grammar():
    p = _profile(
        "For Sam-sung Washing Machine Valve",
        detected=["Sam-sung"],
    )

    result = CrossLayerFactGuard.reconcile(
        p,
        _empty_normalized(),
    )

    assert result["compatibility"]["brands"] == ["Sam-sung"]


def test_detected_brand_is_not_rescued_without_compatibility_grammar():
    p = _profile(
        "Generic cooling fan with BrandLike text",
        detected=["BrandLike"],
    )

    result = CrossLayerFactGuard.reconcile(
        p,
        _empty_normalized(),
    )

    assert result["compatibility"]["brands"] == []


def test_generic_note_target_is_not_promoted_as_brand():
    p = _profile(
        "Replacement parts for fully automatic washing machine",
        notes=["Compatible with fully automatic washing machine"],
    )

    result = CrossLayerFactGuard.reconcile(
        p,
        _empty_normalized(),
    )

    assert result["compatibility"]["brands"] == []


def test_generic_device_note_target_is_not_promoted_as_brand():
    p = _profile(
        "Replacement parts for juicer models 8005 8004",
        notes=["Compatible with juicer models 8005, 8004"],
    )

    result = CrossLayerFactGuard.reconcile(
        p,
        _empty_normalized(),
    )

    assert result["compatibility"]["brands"] == []

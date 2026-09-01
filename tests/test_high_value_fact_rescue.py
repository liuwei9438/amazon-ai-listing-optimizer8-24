from core.cross_layer_fact_guard import CrossLayerFactGuard


def _base_profile(title):
    return {
        "product_identity": {},
        "identifiers": {
            "model_numbers": [],
            "part_numbers": [],
            "unknown_codes": [],
        },
        "brand_info": {},
        "compatibility": {
            "brands": [],
            "models": [],
            "part_numbers": [],
            "compatibility_notes": [],
        },
        "fact_lock": {
            "compatible_models": [],
            "part_numbers": [],
        },
        "search_strategy": {},
        "product_knowledge": {
            "relationship": {
                "brands": [],
                "models": [],
                "part_numbers": [],
                "model_priority": {
                    "primary_model": "",
                    "secondary_models": [],
                },
            },
        },
        "source_fact_ledger": {
            "source_snapshot": {
                "title": title,
                "description": "",
                "bullets": [],
            },
        },
    }


def test_locked_numeric_models_survive_when_normalization_erased_all_models():
    p = _base_profile(
        "10pcs OPC Drum for Canon iR 2520 2525 2530 2535"
    )
    p["fact_lock"]["compatible_models"] = [
        "2520", "2525", "2530", "2535",
    ]

    normalized = {
        "identity": {"text": "OPC Drum"},
        "compatibility": {
            "phrase": "Compatible with Canon",
            "brands": ["Canon"],
        },
        "models": {
            "all": [],
            "primary": "",
            "secondary": [],
        },
    }

    result = CrossLayerFactGuard.reconcile(p, normalized)

    assert result["models"]["primary"] == ""
    assert result["models"]["all"] == [
        "2520", "2525", "2530", "2535",
    ]


def test_rescue_does_not_expand_healthy_model_view():
    p = _base_profile(
        "Part compatible with Brand ABC100 ABC200 ABC300"
    )
    p["fact_lock"]["compatible_models"] = [
        "ABC100", "ABC200", "ABC300",
    ]

    normalized = {
        "identity": {"text": "Replacement Part"},
        "compatibility": {
            "phrase": "Compatible with Brand",
            "brands": ["Brand"],
        },
        "models": {
            "all": ["ABC100"],
            "primary": "",
            "secondary": [],
        },
    }

    result = CrossLayerFactGuard.reconcile(p, normalized)

    assert result["models"]["all"] == ["ABC100"]
    assert result["cross_layer_fact_guard"]["changed"] is False

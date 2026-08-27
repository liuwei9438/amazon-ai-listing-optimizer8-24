from core.cross_layer_fact_guard import CrossLayerFactGuard
from core.strategy_input_builder import StrategyInputBuilder


def _nanxing_profile():
    return {
        "product_identity": {
            "buyer_search_identity": "VCBL-B TV Suction Cup",
        },
        "identifiers": {
            "model_numbers": [],
            "part_numbers": [],
            "unknown_codes": [
                "VCBL-B",
                "10.01.12.03166",
            ],
        },
        "brand_info": {
            "third_party_brands": [],
            "detected_brands": [],
        },
        "compatibility": {
            "brands": [],
            "models": [],
            "part_numbers": [],
            "compatibility_notes": [
                "Compatible with Nanxing CNC Machining Center Router Pod Vacuum Block",
            ],
        },
        "fact_lock": {
            "compatible_models": [
                "10.01.12.03166",
            ],
            "part_numbers": [],
        },
        "search_strategy": {
            "primary_model": "",
        },
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
            "identity": {},
            "title_information": {},
            "feature_classification": {},
            "facts": {},
            "purpose": {},
            "seo": {},
            "compliance": {},
        },
        "title_information": {},
        "source_fact_audit": {},
        "source_fact_ledger": {
            "source_snapshot": {
                "title": (
                    "For VCBL-B 125x75x29 TV Suction Cup for Nanxing "
                    "CNC Machining Center Router Pod Vacuum Block 10.01.12.03166"
                ),
                "description": "",
                "bullets": [],
            },
            "raw_fields": {},
        },
    }


def test_restores_only_source_supported_missing_core_facts():
    profile = _nanxing_profile()
    normalized = {
        "schema_version": "1.0",
        "identity": {
            "text": "Suction Cup",
            "source": "synthesized",
            "confidence": 95,
        },
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

    result = CrossLayerFactGuard.reconcile(
        profile,
        normalized,
    )

    assert result["compatibility"]["brands"] == ["Nanxing"]
    assert result["compatibility"]["phrase"] == "Compatible with Nanxing"
    assert result["models"]["primary"] == "VCBL-B"
    assert result["models"]["secondary"] == ["10.01.12.03166"]
    assert result["cross_layer_fact_guard"]["changed"] is True


def test_strategy_input_uses_reconciled_brand_when_raw_compatibility_is_empty():
    profile = _nanxing_profile()
    profile["normalized_knowledge"] = {
        "identity": {
            "text": "Suction Cup",
            "source": "synthesized",
            "confidence": 95,
        },
        "compatibility": {
            "phrase": "Compatible with Nanxing",
            "brands": ["Nanxing"],
        },
        "models": {
            "all": [],
            "primary": "VCBL-B",
            "secondary": ["10.01.12.03166"],
        },
    }

    built = StrategyInputBuilder.build(profile)

    assert built["compatibility_facts"]["brands"] == ["Nanxing"]
    assert built["locked"]["models"]["primary"] == "VCBL-B"


def test_does_not_expand_healthy_normalized_model_lists():
    profile = _nanxing_profile()
    profile["identifiers"]["model_numbers"] = ["VCBL-B"]
    normalized = {
        "identity": {"text": "Suction Cup"},
        "compatibility": {
            "phrase": "Compatible with Nanxing",
            "brands": ["Nanxing"],
        },
        "models": {
            "all": [],
            "primary": "VCBL-B",
            "secondary": [],
        },
    }

    result = CrossLayerFactGuard.reconcile(
        profile,
        normalized,
    )

    assert result["models"]["primary"] == "VCBL-B"
    assert result["models"]["secondary"] == []
    assert result["cross_layer_fact_guard"]["changed"] is False


def test_note_only_generic_or_nationality_word_is_not_promoted_as_brand():
    profile = _nanxing_profile()
    profile["product_identity"]["buyer_search_identity"] = ""
    profile["identifiers"]["unknown_codes"] = []
    profile["fact_lock"]["compatible_models"] = []
    profile["compatibility"]["compatibility_notes"] = [
        "Compatible with Chinese chainsaw parts",
    ]
    profile["source_fact_ledger"]["source_snapshot"]["title"] = (
        "Gas Fuel Tank Rear Handle Assembly For Chinese Chainsaw"
    )

    normalized = {
        "identity": {"text": "Fuel Tank Handle"},
        "compatibility": {"phrase": "", "brands": []},
        "models": {"all": [], "primary": "", "secondary": []},
    }

    result = CrossLayerFactGuard.reconcile(
        profile,
        normalized,
    )

    assert result["compatibility"]["brands"] == []
    assert result["cross_layer_fact_guard"]["changed"] is False

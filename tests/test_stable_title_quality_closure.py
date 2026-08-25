from core.stable_title_pipeline import StableTitlePipeline


def _profile():
    return {
        "target_language": "English",
        "source_fact_ledger": {
            "source_snapshot": {
                "title": (
                    "Washer Drain Pump WP6-2022030 "
                    "202203 6-2022030 AP6009844"
                ),
                "description": "",
                "bullets": [],
            },
        },
        "title_strategy_input": {
            "target_language": "English",
            "locked": {
                "identity": {
                    "text": "Washer Drain Pump",
                    "source": "test",
                    "confidence": 95,
                },
                "compatibility": {
                    "phrase": "",
                    "brands": [],
                },
                "models": {
                    "all": [
                        "WP6-2022030",
                        "202203",
                        "6-2022030",
                        "AP6009844",
                    ],
                    "primary": "WP6-2022030",
                    "secondary": [
                        "202203",
                        "6-2022030",
                        "AP6009844",
                    ],
                },
            },
            "compatibility_facts": {
                "brands": [],
                "models": [],
                "part_numbers": [],
                "notes": [],
                "important_compatibility": [],
                "third_party_brands": [],
                "seller_brand": "",
            },
            "candidate_facts": {
                "priority_attributes": [],
                "important_specifications": [],
                "important_quantity": "",
                "important_context": [],
                "design_features": [],
                "functional_features": [],
                "usage_scenarios": [],
                "specifications": [],
                "search_primary_keywords": [],
                "search_secondary_keywords": [],
                "locked_all_models": [
                    "WP6-2022030",
                    "202203",
                    "6-2022030",
                    "AP6009844",
                ],
                "locked_secondary_models": [
                    "202203",
                    "6-2022030",
                    "AP6009844",
                ],
                "source_identifier_candidates": [],
                "source_specifications": [],
                "source_title_segments": [],
                "source_for_phrases": [],
                "unresolved_source_facts": [],
                "compatibility_models": [],
                "compatibility_part_numbers": [],
            },
            "confirmed_facts": {
                "quantity": "",
                "material": [],
                "color": "",
                "dimensions": "",
                "voltage": "",
                "power": "",
                "weight": "",
                "part_numbers": [],
                "package_contents": [],
            },
            "purpose": {},
            "search": {},
            "source_evidence": {},
        },
    }


def test_pipeline_exposes_quality_closure_stages_without_ai():
    result = StableTitlePipeline.run(
        _profile(),
        api_key="",
        use_ai_planner=False,
    )

    assert "deterministic_repair" in result
    assert "post_repair_completion" in result
    assert "quality_validation" in result
    assert result["quality_validation"]["status"] == "PASS"
    assert len(result["title"]) <= 75

from core.title_fact_resolver import TitleFactResolver


def test_source_title_quantity_overrides_wrong_intermediate_quantity():
    profile = {
        "source_fact_ledger": {
            "source_snapshot": {
                "title": "5SET X 302HN06080 Separation Feed Pickup Roller",
                "bullets": [],
                "description": "",
            },
        },
        "title_strategy_input": {
            "locked": {
                "identity": {
                    "text": "Separation Feed Pickup Roller",
                },
                "models": {
                    "all": [],
                    "primary": "302HN06080",
                    "secondary": [],
                },
            },
            "compatibility_facts": {
                "brands": [],
                "models": [],
                "part_numbers": [],
            },
            "candidate_facts": {
                "important_quantity": "5pcs",
            },
            "source_evidence": {},
        },
    }

    resolved = TitleFactResolver.resolve(profile)

    quantities = [
        fact["text"]
        for fact in resolved["approved_facts"]
        if fact["type"] == "QUANTITY"
    ]

    assert quantities == ["5sets"]


def test_earlier_secondary_model_gets_higher_priority():
    profile = {
        "source_fact_ledger": {
            "source_snapshot": {
                "title": (
                    "Pressure Regulator 0821302409 "
                    "4-011-04-0319 4011040319"
                ),
                "bullets": [],
                "description": "",
            },
        },
        "title_strategy_input": {
            "locked": {
                "identity": {"text": "Pressure Regulator"},
                "models": {
                    "all": [],
                    "primary": "0821302409",
                    "secondary": [
                        "4-011-04-0319",
                        "4011040319",
                    ],
                },
            },
            "compatibility_facts": {
                "brands": [],
                "models": [],
                "part_numbers": [],
            },
            "candidate_facts": {},
            "source_evidence": {},
        },
    }

    resolved = TitleFactResolver.resolve(profile)

    model_facts = [
        fact
        for fact in resolved["approved_facts"]
        if fact["type"] == "MODEL"
    ]

    priorities = {
        fact["text"]: fact["priority"]
        for fact in model_facts
    }

    assert priorities["4-011-04-0319"] > priorities["4011040319"]

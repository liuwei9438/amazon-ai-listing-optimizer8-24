from generator.title_budget_composer import TitleBudgetComposer


def _fact(
    fid,
    text,
    typ,
    rank,
    value,
    required=False,
):
    return {
        "fact_id": fid,
        "text": text,
        "type": typ,
        "priority": int(value),
        "required": required,
        "source_key": "test",
        "source_traceable": True,
        "full_text": text,
        "short_text": "",
        "value_score": float(value),
        "selection_rank": rank,
        "order_index": rank * 10,
        "language": "English",
    }


def test_model_can_replace_lower_priority_specification():
    plan = {
        "facts": [
            _fact(
                "F1",
                "Pressure Regulator Valve",
                "IDENTITY",
                1,
                95,
                True,
            ),
            _fact(
                "F2",
                "Compatible with Homag",
                "COMPATIBILITY_BRAND",
                2,
                90,
                True,
            ),
            _fact(
                "F3",
                "0821302409",
                "MODEL",
                3,
                85,
                True,
            ),
            _fact(
                "F4",
                "4-011-04-0319",
                "MODEL",
                3,
                80,
                False,
            ),
            _fact(
                "F5",
                "16 Bar Pressure",
                "SPECIFICATION",
                6,
                55,
                False,
            ),
        ]
    }

    composed = TitleBudgetComposer.compose(plan)

    assert composed["status"] == "READY"
    assert "4-011-04-0319" in composed["title"]
    assert len(composed["title"]) <= 75


def test_high_value_promotion_never_removes_required_fact():
    plan = {
        "facts": [
            _fact(
                "F1",
                "Replacement Assembly",
                "IDENTITY",
                1,
                95,
                True,
            ),
            _fact(
                "F2",
                "Compatible with Brand",
                "COMPATIBILITY_BRAND",
                2,
                90,
                True,
            ),
            _fact(
                "F3",
                "ABC123",
                "MODEL",
                3,
                85,
                True,
            ),
        ]
    }

    composed = TitleBudgetComposer.compose(plan)

    assert "Replacement Assembly" in composed["title"]
    assert "Compatible with Brand" in composed["title"]
    assert "ABC123" in composed["title"]

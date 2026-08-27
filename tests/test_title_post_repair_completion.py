from generator.title_post_repair_completion import TitlePostRepairCompletion
from generator.title_quality_gate import TitleQualityGate


def _fact(fid, text, typ, rank, value):
    return {
        "fact_id": fid,
        "text": text,
        "type": typ,
        "required": False,
        "full_text": text,
        "short_text": "",
        "selection_rank": rank,
        "value_score": value,
        "order_index": rank * 10,
    }


def test_refills_budget_with_approved_fact_after_cleanup():
    title = "Commercial Ice Maker Main Control Board HCDM2347"

    plan = {
        "facts": [
            _fact(
                "F1",
                "Commercial Ice Maker Main Control Board",
                "IDENTITY",
                1,
                95,
            ),
            _fact(
                "F2",
                "HCDM2347",
                "MODEL",
                3,
                85,
            ),
            _fact(
                "F3",
                "Universal Replacement",
                "CONTEXT",
                7,
                50,
            ),
        ]
    }

    result = TitlePostRepairCompletion.complete(
        title,
        plan,
        {"used_facts": []},
        "English",
    )

    assert result["status"] == "COMPLETED"
    assert "Universal Replacement" in result["title"]
    assert 61 <= len(result["title"]) <= 75
    assert TitleQualityGate.validate(
        result["title"],
        "English",
    )["status"] == "PASS"


def test_never_readds_noise_or_duplicate_that_quality_gate_rejects():
    title = "Washer Drain Pump WP6-2022030 202203 6-2022030"

    plan = {
        "facts": [
            _fact(
                "F1",
                "as shown",
                "CONTEXT",
                7,
                50,
            ),
            _fact(
                "F2",
                "WP6-2022030",
                "MODEL",
                3,
                85,
            ),
        ]
    }

    result = TitlePostRepairCompletion.complete(
        title,
        plan,
        {"used_facts": []},
        "English",
    )

    assert "as shown" not in result["title"].casefold()
    assert result["title"].casefold().count(
        "wp6-2022030"
    ) == 1


def test_short_title_is_accepted_when_no_safe_fact_can_fit():
    title = "Replacement Sensor Compatible with Brand"

    plan = {
        "facts": [
            _fact(
                "F1",
                "Extremely Long Source Supported Context That Cannot Fit In Remaining Budget Safely",
                "CONTEXT",
                7,
                50,
            ),
        ]
    }

    result = TitlePostRepairCompletion.complete(
        title,
        plan,
        {"used_facts": []},
        "English",
    )

    assert result["status"] == "SOURCE_FACTS_INSUFFICIENT"
    assert result["title"] == title

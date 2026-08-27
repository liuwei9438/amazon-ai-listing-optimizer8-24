import pytest

from analyzer.title_required_core_repair import TitleRequiredCoreRepair
from generator.title_budget_composer import TitleBudgetComposer


def _fact(
    fid,
    text,
    typ,
    required,
    score,
    order,
    short="",
):
    return {
        "fact_id": fid,
        "text": text,
        "type": typ,
        "priority": score,
        "required": required,
        "source_key": "real-regression",
        "source_traceable": True,
        "full_text": text,
        "short_text": short,
        "value_score": float(score),
        "selection_rank": order // 10,
        "order_index": order,
        "language": "English",
    }


CASES = [
    # Original failed rows.
    (
        "Rear Spring Suspension Axle and Rear Spring Rubber PU Bar",
        "Rear Spring Suspension Axle & Spring Rubber PU Bar",
        ["Compatible with Kaabo"],
    ),
    (
        "Metal Balance Chassis Board Seesaw Spring Plate Drive Shaft Upgrade Kit",
        "Metal Chassis Board Spring Drive Shaft Upgrade Kit",
        ["Compatible with WPL"],
    ),
    (
        "3D printer extruder front cover with cooling fan assembly",
        "Extruder Front Cover with Cooling Fan Assembly",
        ["Compatible with Flashforge"],
    ),
    # New stricter-fact-coverage conflicts.
    (
        "Cassette Bypass Pickup Separation Roller",
        "Pickup Separation Roller",
        [
            "20pcs",
            "Compatible with Canon",
            "FC6-6661-000",
        ],
    ),
    (
        "Chainsaw Brake Clutch Side Cover and Front Handle Kit",
        "Brake Clutch Side Cover Handle Kit",
        [
            "Compatible with Husqvarna",
            "503850901",
        ],
    ),
    (
        "Washing Machine Water Inlet Solenoid Valve",
        "Water Inlet Solenoid Valve",
        [
            "Compatible with Sam-sung",
            "DC62-00233D",
        ],
    ),
]


@pytest.mark.parametrize(
    "full_identity, short_identity, other_required",
    CASES,
)
def test_real_overflow_identity_repairs_remain_safe_and_composable(
    full_identity,
    short_identity,
    other_required,
):
    facts = [
        _fact(
            "I",
            full_identity,
            "IDENTITY",
            True,
            95,
            10,
            short_identity,
        )
    ]

    order = 20
    for index, text in enumerate(other_required):
        if text.endswith("pcs"):
            typ = "QUANTITY"
        elif text.startswith("Compatible with"):
            typ = "COMPATIBILITY_BRAND"
        else:
            typ = "MODEL"

        facts.append(
            _fact(
                f"R{index}",
                text,
                typ,
                True,
                90 - index,
                order,
            )
        )
        order += 10

    plan = {"facts": facts}

    budget = TitleRequiredCoreRepair.identity_budget(
        plan,
        max_length=75,
    )

    assert TitleRequiredCoreRepair.validate_short_identity(
        full_identity,
        short_identity,
        budget,
    )

    composed = TitleBudgetComposer.compose(plan)

    assert composed["status"] != "TITLE_BUDGET_CONFLICT"
    assert composed["title"]
    assert len(composed["title"]) <= 75

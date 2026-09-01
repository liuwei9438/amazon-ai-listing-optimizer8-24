from analyzer.title_required_core_repair import TitleRequiredCoreRepair
from generator.title_budget_composer import TitleBudgetComposer


def _fact(fid, text, fact_type, required, score, order, short=""):
    return {
        "fact_id": fid,
        "text": text,
        "type": fact_type,
        "priority": score,
        "required": required,
        "source_key": "test",
        "source_traceable": True,
        "full_text": text,
        "short_text": short,
        "value_score": float(score),
        "selection_rank": order // 10,
        "order_index": order,
        "language": "English",
    }


def test_identity_budget_reserves_immutable_required_brand():
    plan = {
        "facts": [
            _fact("F001", "A" * 57, "IDENTITY", True, 95, 10),
            _fact("F002", "Compatible with Kaabo", "COMPATIBILITY_BRAND", True, 90, 20),
        ]
    }
    assert TitleRequiredCoreRepair.identity_budget(plan, 75) == 53


def test_repair_guard_accepts_deletion_only_complete_identity():
    full = "3D printer extruder front cover with cooling fan assembly"
    short = "Extruder Front Cover with Cooling Fan Assembly"
    assert TitleRequiredCoreRepair.validate_short_identity(full, short, 48)


def test_repair_guard_rejects_invented_synonym():
    full = "3D printer extruder front cover with cooling fan assembly"
    short = "Extruder Housing with Cooling Fan Assembly"
    assert not TitleRequiredCoreRepair.validate_short_identity(full, short, 48)


def test_composer_recovers_when_safe_required_identity_short_exists():
    plan = {
        "facts": [
            _fact(
                "F001",
                "Rear Spring Suspension Axle and Rear Spring Rubber PU Bar",
                "IDENTITY",
                True,
                95,
                10,
                "Rear Spring Suspension Axle & Spring Rubber PU Bar",
            ),
            _fact("F002", "Compatible with Kaabo", "COMPATIBILITY_BRAND", True, 90, 20),
        ]
    }
    result = TitleBudgetComposer.compose(plan)
    assert result["status"] == "READY"
    assert result["character_count"] <= 75
    assert "Compatible with Kaabo" in result["title"]


def test_composer_still_fails_when_no_safe_required_variant_fits():
    plan = {
        "facts": [
            _fact("F001", "X" * 60, "IDENTITY", True, 95, 10),
            _fact("F002", "Compatible with Flashforge", "COMPATIBILITY_BRAND", True, 90, 20),
        ]
    }
    result = TitleBudgetComposer.compose(plan)
    assert result["status"] == "TITLE_BUDGET_CONFLICT"

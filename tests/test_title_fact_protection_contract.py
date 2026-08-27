from generator.title_deterministic_repair import TitleDeterministicRepair


def test_quantity_unit_semantics_must_not_change():
    assert not TitleDeterministicRepair.quantity_semantics_match(
        "5SET X 302HN06080 Separation Feed Pickup Roller",
        "5pcs Printer Separation Feed Roller Compatible with Kyocera 302HN06080",
    )


def test_quantity_abbreviation_preserves_piece_semantics():
    assert TitleDeterministicRepair.quantity_semantics_match(
        "10 Pieces Conveyor Track Chain Pads",
        "10pcs Conveyor Track Chain Pad",
    )


def test_no_source_quantity_does_not_force_one():
    assert TitleDeterministicRepair.quantity_semantics_match(
        "Projector Replacement Lamp POA-LMP109",
        "Projector Replacement Lamp POA-LMP109",
    )

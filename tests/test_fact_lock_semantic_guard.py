from analyzer.fact_lock import _extract_models


def test_semantic_specs_do_not_become_models():
    text = "5pcs 145x145mm Pickup 240W 8-9k 4-Wires 52mm"
    assert _extract_models(text) == []


def test_dotted_and_slash_identifiers_are_preserved():
    text = "1PC 4-008-40-0145 SENSOR R3E-5/12 30MM 4008400145"
    models = _extract_models(text)
    assert "4-008-40-0145" in models
    assert "R3E-5/12" in models
    assert "4008400145" in models
    assert "30MM" not in models


def test_numeric_model_lists_are_preserved_without_dimension_noise():
    text = "Suitable For Chainsaw 340 345 346 350 351 353 357 359 362 365 372"
    models = _extract_models(text)
    for value in ("340", "345", "346", "350", "351", "353", "357", "359", "362", "365", "372"):
        assert value in models


def test_xerox_brand_suffix_x_does_not_trigger_dimension_guard():
    text = "4pcs OPC Drum for Xerox 7228 7235 7245 C240 C320"
    models = _extract_models(text)
    assert "7228" in models
    assert "7235" in models


def test_degree_value_is_not_model():
    text = "For JP ER120 Electric Retract ER-010 ER-120 12mm 100°"
    models = _extract_models(text)
    assert "ER120" in models
    assert "ER-010" in models
    assert "ER-120" in models
    assert "100" not in models

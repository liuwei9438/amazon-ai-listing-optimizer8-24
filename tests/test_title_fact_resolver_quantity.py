from core.title_fact_resolver import TitleFactResolver


def test_piece_quantity_can_use_pcs_abbreviation():
    assert TitleFactResolver._quantity("10 Pieces") == "10pcs"
    assert TitleFactResolver._quantity("10PCS") == "10pcs"


def test_set_quantity_remains_set_semantics():
    assert TitleFactResolver._quantity("5SET X") == "5sets"
    assert TitleFactResolver._quantity("5 sets") == "5sets"


def test_pair_and_pack_are_not_converted_to_pieces():
    assert TitleFactResolver._quantity("2 pairs") == "2pairs"
    assert TitleFactResolver._quantity("3 packs") == "3packs"

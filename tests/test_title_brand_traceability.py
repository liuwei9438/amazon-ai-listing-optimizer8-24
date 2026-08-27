from core.title_fact_resolver import TitleFactResolver


def test_brand_trace_allows_punctuation_equivalence():
    assert TitleFactResolver._brand_traceable(
        "Canon",
        "Pickup Roller For Can-on Ir1730 2520",
    )

    assert TitleFactResolver._brand_traceable(
        "TRAXXAS",
        "Spiral Diffs Gear Assembly for TR-AXXAS Maxx",
    )


def test_brand_trace_allows_for_connector_in_multiword_brand():
    assert TitleFactResolver._brand_traceable(
        "KONICA MINOLTA",
        "Drum Unit For KONICA for MINOLTA Bizhub C258",
    )


def test_brand_trace_rejects_unrelated_brand():
    assert not TitleFactResolver._brand_traceable(
        "Canon",
        "OPC Drum for Xerox C240",
    )

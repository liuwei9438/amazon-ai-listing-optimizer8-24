from generator.title_generator import TitleGenerator


def _candidate(text, kind="FEATURE", priority="B", required=False, short_text="",
               new_information=80, redundancy_penalty=0, selection_value=70):
    return {
        "text": text,
        "short_text": short_text,
        "type": kind,
        "priority": priority,
        "required": required,
        "incremental_value": {
            "new_information": new_information,
            "redundancy_penalty": redundancy_penalty,
            "selection_value": selection_value,
        },
    }


def test_completion_skips_oversized_candidate_and_uses_later_verified_fact():
    profile = {
        "title_strategy": {
            "title_candidates": [
                _candidate("Front Arm Holder", "IDENTITY", "S", True),
                _candidate(
                    "Compatible with Dualtron Storm Limited Thunder II Victor Luxury Plus",
                    "COMPATIBILITY", "A", True,
                ),
                _candidate("Achilleus", "MODEL", "A", False),
                _candidate("with Hole", "FEATURE", "B", False),
            ]
        }
    }

    result = TitleGenerator.generate(profile)

    assert result["title"].startswith("Front Arm Holder")
    # The oversized compatibility phrase must not stop completion.
    assert "Achilleus" in result["title"]
    assert "with Hole" in result["title"]
    assert len(result["title"]) <= 75
    assert result["generator_version"] == "V3.2-title-completion"


def test_completion_does_not_force_padding_when_no_verified_fact_fits():
    profile = {
        "title_strategy": {
            "title_candidates": [
                _candidate("Steering Multi-Function Switch", "IDENTITY", "S", True),
                _candidate("Compatible with Nami", "COMPATIBILITY", "A", True),
            ]
        }
    }

    result = TitleGenerator.generate(profile)

    assert result["title"] == "Steering Multi-Function Switch Compatible with Nami"
    assert len(result["title"]) < 65
    assert len(result["title"]) <= 75


def test_completion_preserves_candidate_order_and_75_char_limit():
    profile = {
        "title_strategy": {
            "title_candidates": [
                _candidate("OPC Drum", "IDENTITY", "S", True),
                _candidate("Compatible with Canon", "COMPATIBILITY", "A", True),
                _candidate("Extremely Long Verified Candidate That Cannot Fit In Remaining Budget",
                           "MODEL", "A", False),
                _candidate("iR 2520", "MODEL", "A", False),
                _candidate("2525", "MODEL", "B", False),
                _candidate("2530", "MODEL", "B", False),
            ]
        }
    }

    result = TitleGenerator.generate(profile)

    assert "iR 2520" in result["title"]
    assert "2525" in result["title"]
    assert "2530" in result["title"]
    assert len(result["title"]) <= 75

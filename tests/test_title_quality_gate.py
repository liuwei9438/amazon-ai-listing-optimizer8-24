import pytest

from generator.title_quality_gate import TitleQualityGate


@pytest.mark.parametrize(
    "title, expected_error",
    [
        (
            "Scooter Brake Disc Compatible with Dualtron Compatible with Storm",
            "REPEATED_COMPATIBILITY_QUALIFIER",
        ),
        (
            "Handlebar Grip Compatible with Kaabo Kaabo Handle Bar Cover Handlebar Grip",
            "ADJACENT_TOKEN_DUPLICATION",
        ),
        (
            "Scooter Horn 7260R Teverun Fighter Supreme 7260R Horn Suit",
            "REPEATED_IDENTIFIER_OR_SPEC",
        ),
        (
            "Brake Disc Teverun Fighter 11/11+ 160mm Brake Disc Brake Disc 160mm",
            "REPEATED_IDENTIFIER_OR_SPEC",
        ),
        (
            "Washer Drain Pump WP6-2022030 202203 6-2022030 AP6009844 as shown",
            "NOISE_AS_SHOWN",
        ),
        (
            "Commercial Ice Maker Main Control Board HCDM2347 Model number HCDM2347",
            "NOISE_MODEL_NUMBER",
        ),
        (
            "3D Printer Extruder Stepper Motor 3D 3D Printer Parts Low noise",
            "ADJACENT_TOKEN_DUPLICATION",
        ),
        (
            "3D printer cooling fan blower 5020 CC1 24V 12000 Rpm CC1 3D Printer Parts",
            "REPEATED_IDENTIFIER_OR_SPEC",
        ),
        (
            "Chainsaw Ignition Coil 340 345 346 350 351 353 357 359 362 365 372 Suitable",
            "ORPHAN_LOW_VALUE_ENDING",
        ),
    ],
)
def test_real_regression_patterns_are_blocked(title, expected_error):
    result = TitleQualityGate.validate(title, "English")
    assert result["status"] == "FAIL"
    assert expected_error in result["errors"]


@pytest.mark.parametrize(
    "title",
    [
        "Glue Level Sensor Compatible with Homag 4-008-40-0145 4008400145 R3E-5/12",
        "Vacuum Block Suction Cup Compatible with Homag VCBL-K2 10.01.12.00447",
        "Headlight for Electric Scooter Compatible with OBARTER X5 60V",
        "Platen Roller for Barcode Label Printer Compatible with Toshiba TEC B-SX4T",
    ],
)
def test_known_clean_titles_are_not_blocked(title):
    result = TitleQualityGate.validate(title, "English")
    assert result["status"] == "PASS"

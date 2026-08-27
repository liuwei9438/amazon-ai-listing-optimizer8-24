import pytest

from generator.title_deterministic_repair import TitleDeterministicRepair
from generator.title_quality_gate import TitleQualityGate


BAD_CASES = [
    "Scooter Brake Disc Compatible with Dualtron Compatible with Storm",
    "Handlebar Grip Compatible with Kaabo Kaabo Handle Bar Cover Handlebar Grip",
    "Scooter Horn 7260R Teverun Fighter Supreme 7260R Horn Suit",
    "Brake Disc Teverun Fighter 11/11+ 160mm Brake Disc Brake Disc 160mm",
    "Washer Drain Pump WP6-2022030 202203 6-2022030 AP6009844 as shown",
    "Commercial Ice Maker Main Control Board HCDM2347 Model number HCDM2347",
    "3D Printer Extruder Stepper Motor 3D 3D Printer Parts Low noise",
    "Chainsaw Ignition Coil 340 345 346 350 351 353 357 359 362 365 372 Suitable",
]


@pytest.mark.parametrize("title", BAD_CASES)
def test_low_risk_repairs_improve_known_bad_cases(title):
    repaired = TitleDeterministicRepair.repair(title, "English")
    assert repaired["changed"] is True
    quality = TitleQualityGate.validate(repaired["title"], "English")
    assert quality["status"] == "PASS", (
        repaired["title"],
        quality["errors"],
    )


@pytest.mark.parametrize(
    "title",
    [
        "Glue Level Sensor Compatible with Homag 4-008-40-0145 4008400145 R3E-5/12",
        "Vacuum Block Suction Cup Compatible with Homag VCBL-K2 10.01.12.00447",
        "Headlight for Electric Scooter Compatible with OBARTER X5 60V",
    ],
)
def test_clean_titles_are_unchanged(title):
    repaired = TitleDeterministicRepair.repair(title, "English")
    assert repaired["changed"] is False
    assert repaired["title"] == title

from core.specification_dominance import SpecificationDominance
from core.entity_role_resolver import EntityRoleResolver
from core.compatibility_relationship_map import CompatibilityRelationshipMap
from core.semantic_containment import SemanticContainment
from core.source_compatibility_series_extractor import SourceCompatibilitySeriesExtractor
from generator.title_fact_traceability_gate import TitleFactTraceabilityGate
from generator.title_deterministic_repair import TitleDeterministicRepair


def test_specification_dominance_and_equivalence():
    assert SpecificationDominance.equivalent("150X25X50mm","150×25×50 mm")
    assert SpecificationDominance.dominates("65*12*28mm","65mm")
    assert SpecificationDominance.dominates("600W/900W","600W")
    assert not SpecificationDominance.dominates("220V","50Hz")


def test_model_evidence_blocks_weak_brand_inference():
    profile={
        "identifiers":{"unknown_codes":["CCE016"],"model_numbers":[],"part_numbers":[]},
        "compatibility":{"models":["CCE016"],"part_numbers":[],"brands":[]},
        "fact_lock":{"compatible_models":["CCE016"],"part_numbers":[]},
        "brand_info":{"detected_brands":[],"third_party_brands":[]},
        "product_knowledge":{"relationship":{}},
        "normalized_knowledge":{"models":{"all":[],"secondary":[],"primary":""}},
    }
    assert EntityRoleResolver.brand_candidate_decision("CCE016",profile)["accepted"] is False


def test_relationship_map_keeps_brand_model_binding():
    profile={
        "brand_info":{"third_party_brands":["Singer","Necchi"]},
        "compatibility":{"brands":["Singer","Necchi"],"models":["S14-78","4537"]},
        "identifiers":{"model_numbers":["S14-78","4537"]},
        "source_fact_ledger":{"source_snapshot":{
            "title":"Needle Plate for Singer S14-78 / Necchi 4537",
            "description":"for Singer Sewing Machine Models:S14-78. for Necchi Sewing Machine Models:4537.",
            "bullets":[],
        }},
    }
    m=CompatibilityRelationshipMap.build(profile)
    assert m["bindings"]["Singer"]==["S14-78"]
    assert m["bindings"]["Necchi"]==["4537"]


def test_range_endpoint_is_provable_containment_only():
    assert SemanticContainment.dominates("AC220-240V","240V")
    assert not SemanticContainment.dominates("AC220-240V","110V")


def test_traceability_accepts_description_and_rejects_unknown():
    profile={"source_fact_ledger":{"source_snapshot":{
        "title":"Brake Cover Kit","bullets":[],"description":"OEM ref 503850901"
    }}}
    ok=TitleFactTraceabilityGate.validate(profile,{"used_facts":[{"fact_id":"P","type":"PART_NUMBER","text":"503850901"}]})
    bad=TitleFactTraceabilityGate.validate(profile,{"used_facts":[{"fact_id":"P","type":"PART_NUMBER","text":"999999999"}]})
    assert ok["status"]=="PASS"
    assert bad["status"]=="FAIL"


def test_series_extractor_is_bounded_and_conservative():
    p={
        "compatibility":{"brands":["Kaabo"],"compatibility_notes":["Compatible with Kaabo Wolf King GT Pro electric scooter"]},
        "source_fact_ledger":{"source_snapshot":{"title":"Controller for Kaabo Wolf King GT Pro Electric Scooter","description":"","bullets":[]}},
    }
    assert SourceCompatibilitySeriesExtractor.extract(p)["recovered_models"]==["Wolf King GT Pro"]
    p2={
        "compatibility":{"brands":["Kaabo"],"compatibility_notes":[]},
        "source_fact_ledger":{"source_snapshot":{"title":"Grip for Kaabo Wolf Warrior Wolf King Wolf X E-Scooter","description":"","bullets":[]}},
    }
    assert SourceCompatibilitySeriesExtractor.extract(p2)["recovered_models"]==[]


def test_final_repair_removes_only_provable_redundancy():
    out=TitleDeterministicRepair.repair("Valve AC220-240V 240V 50Hz","English")["title"]
    assert out=="Valve AC220-240V 50Hz"
    out2=TitleDeterministicRepair.repair("Garden Weeder Head for garden use","English")["title"]
    assert out2=="Garden Weeder Head for garden use"

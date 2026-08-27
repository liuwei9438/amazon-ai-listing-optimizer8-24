from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from typing import Any


COMPATIBILITY_PATTERNS = (
    r"\bcompatible\s+with\b",
    r"\bcompatible\s+for\b",
    r"\breplacement\s+for\b",
    r"\breplaces\b",
    r"\bdesigned\s+for\b",
    r"\bmade\s+for\b",
    r"\bworks?\s+with\b",
    r"\bfit(?:s|ting)?\b",
    r"\bfor\s+[A-Z][A-Za-z0-9&.-]{1,30}\b",
    r"\bpara\b",
    r"\bcompatible\s+con\b",
    r"\bkompatibel\s+mit\b",
    r"\bcompatibile\s+con\b",
    r"\bcompatible\s+avec\b",
)

ORIGINAL_CLAIM_PATTERNS = (
    r"\boriginal\b",
    r"\bgenuine\b",
    r"\bofficial\b",
    r"\bauthentic\b",
    r"\bauthorized\b",
    r"\bfactory\s+original\b",
)

OEM_PATTERNS = (
    r"\boem\b",
    r"\boriginal\s+equipment\s+manufacturer\b",
)

NO_BRAND_PATTERNS = (
    r"\bno\s+brand\b",
    r"\bunbranded\b",
    r"\bgeneric\b",
    r"\bbrandless\b",
)

HIGH_RISK_BRANDS = {
    "apple", "dyson", "dji", "samsung", "lg", "epson", "hp",
    "canon", "sony", "microsoft", "nintendo", "thermomix",
}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, (tuple, set)):
        values = list(value)
    elif value:
        values = [value]
    else:
        values = []

    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _record_dict(record: Any) -> dict[str, Any]:
    if is_dataclass(record):
        return asdict(record)
    if isinstance(record, dict):
        return dict(record)
    if hasattr(record, "__dict__"):
        return dict(vars(record))
    return {}


def _source_text(record: Any) -> str:
    raw = _record_dict(record)
    parts: list[str] = []
    for key in (
        "title", "description", "details", "brand", "seller_brand",
        "manufacturer", "color", "variant",
    ):
        parts.append(_clean(raw.get(key)))
    parts.extend(_as_list(raw.get("bullets")))
    return " ".join(part for part in parts if part)


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _merge_brands(profile: dict[str, Any]) -> list[str]:
    brand_info = profile.get("brand_info", {})
    compatibility = profile.get("compatibility", {})
    candidates = [
        *_as_list(brand_info.get("third_party_brands")),
        *_as_list(compatibility.get("brands")),
    ]
    result: list[str] = []
    seen: set[str] = set()
    for brand in candidates:
        key = brand.casefold()
        if key not in seen:
            seen.add(key)
            result.append(brand)
    return result


def classify_brand_relationship(
    record: Any,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """
    Apply deterministic brand/compliance rules after AI analysis.

    The project assumes products are third-party compatible unless the source
    explicitly and credibly identifies an owned seller brand. Original/OEM
    claims are treated as high-risk claims, not accepted as verified facts.
    """
    source = _source_text(record)
    source_lower = source.casefold()
    brands = _merge_brands(profile)
    seller_brand = _clean(profile.get("brand_info", {}).get("seller_brand"))

    has_compatibility_wording = _contains_any(source, COMPATIBILITY_PATTERNS)
    has_original_claim = _contains_any(source, ORIGINAL_CLAIM_PATTERNS)
    has_oem_claim = _contains_any(source, OEM_PATTERNS)
    explicitly_generic = _contains_any(source, NO_BRAND_PATTERNS)

    detected_high_risk_brand = any(
        brand.casefold() in HIGH_RISK_BRANDS for brand in brands
    )

    relationship = "unknown"
    risk_level = "low"
    rewrite_strategy = "no_brand"
    reasons: list[str] = []
    forbidden_terms: list[str] = []

    if has_original_claim:
        relationship = "high_risk_brand_usage"
        risk_level = "high"
        rewrite_strategy = "compatible_with"
        reasons.append("存在未经证实的原装、正品、官方或授权声明")
        forbidden_terms.extend(
            term for term in ("original", "genuine", "official", "authentic", "authorized")
            if term in source_lower
        )
    elif has_oem_claim:
        relationship = "high_risk_brand_usage"
        risk_level = "high"
        rewrite_strategy = "compatible_with"
        reasons.append("存在未经证实的 OEM 声明")
        forbidden_terms.append("OEM")
    elif brands and has_compatibility_wording:
        relationship = "unbranded_compatible"
        risk_level = "low"
        rewrite_strategy = "compatible_with"
    elif brands:
        relationship = "high_risk_brand_usage"
        risk_level = "high" if detected_high_risk_brand else "medium"
        rewrite_strategy = "compatible_with"
        reasons.append("出现第三方品牌，但缺少明确兼容表达")
    elif explicitly_generic or not brands:
        relationship = "generic"
        risk_level = "low"
        rewrite_strategy = "no_brand"
    elif seller_brand:
        relationship = "own_brand"
        risk_level = "low"
        rewrite_strategy = "own_brand"

    brand_info = profile.setdefault("brand_info", {})
    compatibility = profile.setdefault("compatibility", {})
    compliance = profile.setdefault("compliance", {})

    brand_info["third_party_brands"] = brands
    brand_info["detected_brands"] = brands
    brand_info["relationship"] = relationship
    brand_info["risk_level"] = risk_level
    brand_info["rewrite_strategy"] = rewrite_strategy

    compatibility["brands"] = brands

    existing_reasons = _as_list(compliance.get("risk_reasons"))
    existing_forbidden = _as_list(compliance.get("forbidden_or_risky_terms"))
    compliance["risk_level"] = risk_level
    compliance["risk_reasons"] = _as_list([*existing_reasons, *reasons])
    compliance["forbidden_or_risky_terms"] = _as_list(
        [*existing_forbidden, *forbidden_terms]
    )
    compliance["compatibility_wording_required"] = relationship in {
        "unbranded_compatible",
        "high_risk_brand_usage",
    }

    return profile

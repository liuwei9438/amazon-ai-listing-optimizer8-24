
from __future__ import annotations

import re
from typing import Any

PROHIBITED_TERMS = {
    "original", "genuine", "official", "oem",
    "authentic", "best seller", "#1", "premium quality"
}

DEFAULT_BRANDS = {
    "LG", "Dyson", "Epson", "Samsung",
    "Bosch", "Philips", "Whirlpool"
}


def remove_prohibited_terms(text: str) -> str:
    result = text
    for term in PROHIBITED_TERMS:
        result = re.sub(
            rf"\\b{re.escape(term)}\\b",
            "",
            result,
            flags=re.IGNORECASE,
        )
    return re.sub(r"\\s+", " ", result).strip()


def protect_text(text: str, detected_brands: list[str] | None = None) -> dict[str, Any]:
    cleaned = remove_prohibited_terms(text)

    brands = detected_brands or []

    if not brands:
        for brand in DEFAULT_BRANDS:
            if re.search(rf"\\b{re.escape(brand)}\\b", cleaned, re.IGNORECASE):
                brands.append(brand)

    brands = list(dict.fromkeys(brands))
    changes = []

    if brands and not re.search(r"compatible\\s+with", cleaned, re.IGNORECASE):
        cleaned = cleaned.rstrip() + f" Compatible with {', '.join(brands)} models"
        changes.append("Added compatibility wording")

    if cleaned != text:
        changes.append("Removed prohibited terms")

    return {
        "text": cleaned.strip(),
        "changes": changes,
        "risk": "low",
        "detected_brands": brands,
    }

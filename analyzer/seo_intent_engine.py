from __future__ import annotations

from typing import Any
import re

CATEGORY_MAP = {
    "washing machine part": "washing machine",
    "printer part": "printer",
    "shaver part": "electric shaver",
}

REMOVE_WORDS = {
    "compatible", "replacement", "replace", "for",
    "function", "power", "drive", "part"
}

def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text).lower())

def _category(product_type: str) -> str:
    value = str(product_type).lower()
    for key, result in CATEGORY_MAP.items():
        if key in value:
            return result
    return value.strip()

def generate_primary_search(profile: dict[str, Any]) -> dict[str, Any]:
    basic = profile.get("basic_info", {})
    category = _category(basic.get("product_type", ""))
    function = basic.get("main_function", "")

    words = [w for w in _words(function) if w not in REMOVE_WORDS]

    if "start" in words and "button" in words:
        keyword = "start button"
    elif "print" in words and "head" in words:
        keyword = "print head"
    elif "head" in words:
        keyword = "head"
    else:
        keyword = " ".join(words[:3])

    result = " ".join(dict.fromkeys((category + " " + keyword).split()))

    return {"primary_search": [result.strip()]}

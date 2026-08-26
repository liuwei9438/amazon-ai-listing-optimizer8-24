from __future__ import annotations

from typing import Any
import re


BLOCKED = {
    "best", "premium", "original", "genuine", "official", "authentic",
    "cheap", "sale", "discount", "oem", "#1",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ,;:")


def _safe(value: str) -> str:
    text = _clean(value)
    if any(word in text.casefold() for word in BLOCKED):
        return ""
    return text


def generate_primary_search(profile: dict[str, Any]) -> dict[str, Any]:
    """Choose a source-backed primary search phrase; do not synthesize fragments."""
    profile = profile if isinstance(profile, dict) else {}

    title_plan = profile.get("title_plan", {}) or {}
    candidates = title_plan.get("search_terms", []) or []
    if not isinstance(candidates, list):
        candidates = []

    for candidate in candidates:
        text = _safe(candidate)
        if text:
            return {"primary_search": [text]}

    normalized = profile.get("normalized_knowledge", {}) or {}
    identity = _clean((normalized.get("identity", {}) or {}).get("text", ""))
    if not identity:
        basic = profile.get("basic_info", {}) or {}
        identity = _clean(basic.get("product_name") or basic.get("product_type"))

    return {"primary_search": [identity] if _safe(identity) else []}

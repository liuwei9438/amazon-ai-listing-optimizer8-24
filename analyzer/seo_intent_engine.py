from __future__ import annotations

from typing import Any
import re


BLOCKED = {
    "best", "premium", "original", "genuine", "official", "authentic",
    "cheap", "sale", "discount", "oem", "#1",
}

NOISE_WORDS = {
    "for", "with", "and", "the", "a", "an", "of", "to", "from",
    "replacement", "part", "parts", "accessory", "accessories",
    "compatible", "compatibility", "model", "models",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ,;:")


def _safe(value: str) -> str:
    text = _clean(value)
    if any(word in text.casefold() for word in BLOCKED):
        return ""
    return text


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def _tokens(value: str) -> set[str]:
    out = set()
    for t in _norm(value).split():
        if t in NOISE_WORDS or len(t) <= 1:
            continue
        if t.endswith("s") and len(t) > 4 and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return out


def _identity(profile: dict[str, Any]) -> str:
    normalized = profile.get("normalized_knowledge", {}) or {}
    text = _clean((normalized.get("identity", {}) or {}).get("text", ""))
    if text:
        return text
    basic = profile.get("basic_info", {}) or {}
    return _clean(basic.get("product_name") or basic.get("product_type"))


def _entities(profile: dict[str, Any]) -> set[str]:
    normalized = profile.get("normalized_knowledge", {}) or {}
    brands = (normalized.get("compatibility", {}) or {}).get("brands", []) or []
    models = normalized.get("models", {}) or {}
    values = []
    if isinstance(brands, list):
        values.extend(brands)
    elif isinstance(brands, str):
        values.append(brands)
    values.extend(models.get("all", []) or [])
    values.extend(models.get("secondary", []) or [])
    if models.get("primary"):
        values.append(models.get("primary"))
    return {_norm(v) for v in values if _norm(v)}


def _identity_bearing(candidate: str, identity: str, entities: set[str]) -> bool:
    candidate = _safe(candidate)
    if not candidate or _norm(candidate) in entities:
        return False
    it, ct = _tokens(identity), _tokens(candidate)
    if not it or not ct:
        return False
    overlap = it & ct
    # One meaningful shared identity token is enough for a qualified product
    # search phrase; entity-only terms were rejected before this check.
    return len(overlap) >= 1


COMMON_COLORS = {
    "black", "white", "red", "blue", "green", "yellow", "orange", "grey",
    "gray", "silver", "gold", "brown", "purple", "pink", "beige"
}

GENERIC_HEAD_WORDS = {
    "set", "assembly", "part", "parts", "accessory", "accessories",
    "component", "components", "machine", "equipment", "unit", "kit"
}


def _identity_head(identity: str) -> str:
    ordered = [t for t in _norm(identity).split() if t not in NOISE_WORDS]
    for token in reversed(ordered):
        if token not in GENERIC_HEAD_WORDS:
            return token
    return ordered[-1] if ordered else ""


def _candidate_score(candidate: str, identity: str) -> float:
    ct = _tokens(candidate)
    it = _tokens(identity)
    overlap = len(ct & it)
    score = overlap * 20.0 + min(len(ct), 6) * 2.0
    head = _identity_head(identity)
    if head and head in ct:
        score += 32.0
    if re.search(r"\d", candidate):
        score += 15.0
    if ct & COMMON_COLORS:
        score -= 12.0
    return score


def generate_primary_search(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Choose a source-backed, product-bearing primary search phrase.
    A brand-only or model-only token is never a valid primary keyword.
    """
    profile = profile if isinstance(profile, dict) else {}
    identity = _identity(profile)
    entities = _entities(profile)

    title_plan = profile.get("title_plan", {}) or {}
    candidates = title_plan.get("search_terms", []) or []
    if not isinstance(candidates, list):
        candidates = []

    valid = []
    for index, candidate in enumerate(candidates):
        text = _safe(candidate)
        if _identity_bearing(text, identity, entities):
            valid.append((_candidate_score(text, identity), -index, text))

    if valid:
        valid.sort(reverse=True)
        return {"primary_search": [valid[0][2]]}

    return {"primary_search": [identity] if _safe(identity) else []}

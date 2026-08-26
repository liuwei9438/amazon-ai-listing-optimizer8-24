from __future__ import annotations

from typing import Any
import re


class SEOKeywordEngine:
    VERSION = "v2.0-source-backed-keywords"

    BLOCKED_WORDS = [
        "best", "premium", "original", "genuine", "official", "authentic",
        "cheap", "sale", "discount", "oem", "#1",
    ]

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip(" ,;:")

    @staticmethod
    def clean_keyword(keyword: Any) -> str:
        text = SEOKeywordEngine._clean(keyword)
        lower = text.casefold()
        if not text:
            return ""
        if any(word in lower for word in SEOKeywordEngine.BLOCKED_WORDS):
            return ""
        return text

    @staticmethod
    def unique(items):
        result, seen = [], set()
        for item in items:
            text = SEOKeywordEngine.clean_keyword(item)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    @staticmethod
    def _flatten(value: Any) -> list[str]:
        out = []
        if isinstance(value, str):
            text = SEOKeywordEngine.clean_keyword(value)
            if text:
                out.append(text)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                out.extend(SEOKeywordEngine._flatten(item))
        elif isinstance(value, dict):
            # Keep only scalar meaningful values. Never stringify Python dicts.
            for key, item in value.items():
                if key in {"unit"} and not value.get("value"):
                    continue
                if isinstance(item, (str, int, float)) and str(item).strip():
                    out.extend(SEOKeywordEngine._flatten(str(item)))
        elif isinstance(value, (int, float)):
            out.append(str(value))
        return out

    @staticmethod
    def _identity(profile: dict) -> str:
        normalized = profile.get("normalized_knowledge", {}) or {}
        identity = SEOKeywordEngine._clean((normalized.get("identity", {}) or {}).get("text", ""))
        if identity:
            return identity
        basic = profile.get("basic_info", {}) or {}
        return SEOKeywordEngine._clean(basic.get("product_name") or basic.get("product_type"))

    @staticmethod
    def _brand(profile: dict) -> str:
        normalized = profile.get("normalized_knowledge", {}) or {}
        brands = (normalized.get("compatibility", {}) or {}).get("brands", []) or []
        if isinstance(brands, list) and brands:
            return SEOKeywordEngine._clean(brands[0])
        return ""

    @staticmethod
    def _search_intent(profile: dict) -> str:
        basic = profile.get("basic_info", {}) or {}
        text = " ".join([
            SEOKeywordEngine._clean(basic.get("product_type", "")),
            SEOKeywordEngine._clean(basic.get("category", "")),
        ]).casefold()
        if "part" in text or "component" in text:
            return "replacement part"
        if "filter" in text or "cartridge" in text or "blade" in text:
            return "replacement consumable"
        return "replacement accessory"

    @staticmethod
    def generate(profile: dict[str, Any]):
        profile = profile if isinstance(profile, dict) else {}
        seo_intent = profile.get("seo_intent", {}) or {}
        primary = seo_intent.get("primary_search", []) or []
        primary = primary if isinstance(primary, list) else []

        title_plan = profile.get("title_plan", {}) or {}
        plan_terms = title_plan.get("search_terms", []) or []
        if not isinstance(plan_terms, list):
            plan_terms = []

        identity = SEOKeywordEngine._identity(profile)
        brand = SEOKeywordEngine._brand(profile)

        primary_keywords = SEOKeywordEngine.unique(primary or ([identity] if identity else []))

        secondary = []
        secondary.extend(plan_terms)
        if identity:
            secondary.append(identity)
        if brand and identity:
            secondary.append(f"{brand} {identity}")

        # Re-use facts already classified upstream; do not create marketing claims.
        knowledge = profile.get("product_knowledge", {}) or {}
        ident = knowledge.get("identity", {}) or {}
        classification = knowledge.get("feature_classification", {}) or {}
        feature_keywords = []
        for value in [
            ident.get("functional_features", []),
            classification.get("materials", []),
            classification.get("specifications", []),
            ident.get("design_features", []),
        ]:
            feature_keywords.extend(SEOKeywordEngine._flatten(value))

        strategy = profile.get("search_strategy", {}) or {}
        model_ids = []
        for key in ("title_identifiers", "bullet_identifiers", "backend_identifiers"):
            values = strategy.get(key, []) or []
            if isinstance(values, list):
                model_ids.extend(values)

        if not model_ids:
            normalized = profile.get("normalized_knowledge", {}) or {}
            models = normalized.get("models", {}) or {}
            primary_model = SEOKeywordEngine._clean(models.get("primary", ""))
            if primary_model:
                model_ids.append(primary_model)
            model_ids.extend(models.get("secondary", []) or [])
            model_ids.extend(models.get("all", []) or [])

        model_keywords = []
        for model in SEOKeywordEngine.unique(model_ids)[:15]:
            if brand:
                model_keywords.append(f"{brand} {model}")
            else:
                model_keywords.append(model)

        backend = []
        backend.extend(primary_keywords)
        backend.extend(plan_terms)
        backend.extend(model_keywords)
        backend.extend(feature_keywords)

        return {
            "version": SEOKeywordEngine.VERSION,
            "primary_keywords": SEOKeywordEngine.unique(primary_keywords),
            "secondary_keywords": SEOKeywordEngine.unique(secondary),
            "model_keywords": SEOKeywordEngine.unique(model_keywords),
            "feature_keywords": SEOKeywordEngine.unique(feature_keywords),
            "backend_search_terms": SEOKeywordEngine.unique(backend),
            "search_intent": SEOKeywordEngine._search_intent(profile),
        }

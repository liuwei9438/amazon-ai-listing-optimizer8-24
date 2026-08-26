from __future__ import annotations

from typing import Any
import re


class SEOKeywordEngine:
    VERSION = "v2.1-identity-gated-search-keywords"

    BLOCKED_WORDS = [
        "best", "premium", "original", "genuine", "official", "authentic",
        "cheap", "sale", "discount", "oem", "#1",
    ]

    NOISE_WORDS = {
        "for", "with", "and", "the", "a", "an", "of", "to", "from",
        "replacement", "part", "parts", "accessory", "accessories",
        "compatible", "compatibility", "model", "models",
        "size", "dimension", "dimensions", "material", "materials",
        "color", "colour", "feature", "features",
    }

    SEARCH_WEAK_PHRASES = {
        "easy to clean", "durable", "safe for travel", "safe for carrying",
        "complete seal with the cup", "high quality", "good quality",
    }

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
            key = SEOKeywordEngine._normalized(text)
            if text and key and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    @staticmethod
    def _normalized(value: str) -> str:
        value = SEOKeywordEngine._clean(value).casefold().replace("×", "x").replace("*", "x")
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    @staticmethod
    def _tokens(value: str) -> set[str]:
        out = set()
        for t in SEOKeywordEngine._normalized(value).split():
            if t in SEOKeywordEngine.NOISE_WORDS or len(t) <= 1:
                continue
            if t.endswith("s") and len(t) > 4 and not t.endswith("ss"):
                t = t[:-1]
            out.add(t)
        return out

    @staticmethod
    def _numeric_atoms(value: str) -> set[str]:
        text = SEOKeywordEngine._clean(value).casefold().replace("×", "x").replace("*", "x")
        atoms: set[str] = set()
        for m in re.finditer(r"(\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)+)\s*(mm|cm|m|in|inch|kg|g|lb)?", text):
            values = m.group(1).split("x")
            unit = m.group(2) or ""
            for number in values:
                atoms.add(number + unit)
        for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(mm|cm|m|in|inch|oz|ml|l|v|w|kw|rpm|hz|kg|g|lb|mah|ah|ohm|k)\b", text):
            atoms.add(number + unit)
        return atoms

    @staticmethod
    def semantic_unique(items):
        result = []
        for item in SEOKeywordEngine.unique(items):
            tokens = SEOKeywordEngine._tokens(item)
            atoms = SEOKeywordEngine._numeric_atoms(item)
            redundant = False
            for accepted in result:
                atokens = SEOKeywordEngine._tokens(accepted)
                aatoms = SEOKeywordEngine._numeric_atoms(accepted)
                if tokens and tokens == atokens:
                    redundant = True
                    break
                if atoms and aatoms and atoms.issubset(aatoms) and len(aatoms) > len(atoms):
                    redundant = True
                    break
            if not redundant:
                result.append(item)
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
            for key, item in value.items():
                if key == "unit" and not value.get("value"):
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
    def _brands(profile: dict) -> list[str]:
        normalized = profile.get("normalized_knowledge", {}) or {}
        brands = (normalized.get("compatibility", {}) or {}).get("brands", []) or []
        if isinstance(brands, str):
            brands = [brands]
        return SEOKeywordEngine.unique(brands)

    @staticmethod
    def _all_models(profile: dict) -> list[str]:
        strategy = profile.get("search_strategy", {}) or {}
        model_ids = []
        for key in ("title_identifiers", "bullet_identifiers", "backend_identifiers"):
            values = strategy.get(key, []) or []
            if isinstance(values, list):
                model_ids.extend(values)

        normalized = profile.get("normalized_knowledge", {}) or {}
        models = normalized.get("models", {}) or {}
        primary_model = SEOKeywordEngine._clean(models.get("primary", ""))
        if primary_model:
            model_ids.append(primary_model)
        model_ids.extend(models.get("secondary", []) or [])
        model_ids.extend(models.get("all", []) or [])
        return SEOKeywordEngine.unique(model_ids)

    @staticmethod
    def _is_brand_or_model_only(term: str, brands: list[str], models: list[str]) -> bool:
        n = SEOKeywordEngine._normalized(term)
        if not n:
            return True
        entities = brands + models
        return any(n == SEOKeywordEngine._normalized(x) for x in entities if x)

    @staticmethod
    def _is_identity_search_phrase(term: str, identity: str, brands: list[str], models: list[str]) -> bool:
        term = SEOKeywordEngine.clean_keyword(term)
        if not term or SEOKeywordEngine._is_brand_or_model_only(term, brands, models):
            return False

        it = SEOKeywordEngine._tokens(identity)
        tt = SEOKeywordEngine._tokens(term)
        if not it or not tt:
            return False

        overlap = it & tt
        # A primary keyword must carry the product noun/identity semantics.
        # One meaningful shared token is enough for short category identities;
        # longer identities require either 2 shared terms or >=40% coverage.
        # One meaningful shared identity token is sufficient. This preserves
        # strong qualified searches such as "Alnico 5 pickups" while brand-only
        # and model-only candidates have already been rejected above.
        return len(overlap) >= 1

    @staticmethod
    def _searchable_feature(term: str) -> bool:
        text = SEOKeywordEngine.clean_keyword(term)
        if not text:
            return False
        lower = text.casefold()
        if any(p in lower for p in SEOKeywordEngine.SEARCH_WEAK_PHRASES):
            return False
        if re.search(r"\b(?:function|shape|design|used|use|allowing|construction)\b", lower):
            return False

        words = SEOKeywordEngine._tokens(text)
        if re.search(r"\d", text):
            return True
        # Compact material / structural attributes can carry search value.
        if 1 <= len(words) <= 3 and not re.search(r"\b(?:used|use|allowing|designed|safe|easy)\b", lower):
            return True
        return False

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
        identity = SEOKeywordEngine._identity(profile)
        brands = SEOKeywordEngine._brands(profile)
        models = SEOKeywordEngine._all_models(profile)

        seo_intent = profile.get("seo_intent", {}) or {}
        primary_candidates = seo_intent.get("primary_search", []) or []
        if not isinstance(primary_candidates, list):
            primary_candidates = []

        title_plan = profile.get("title_plan", {}) or {}
        plan_terms = title_plan.get("search_terms", []) or []
        if not isinstance(plan_terms, list):
            plan_terms = []

        # Primary keyword is product-first. Brand-only/model-only/generic category
        # candidates cannot displace the actual product identity.
        primary_keyword = ""
        for candidate in primary_candidates + plan_terms:
            if SEOKeywordEngine._is_identity_search_phrase(candidate, identity, brands, models):
                primary_keyword = SEOKeywordEngine.clean_keyword(candidate)
                break
        if not primary_keyword:
            primary_keyword = SEOKeywordEngine.clean_keyword(identity)

        primary_keywords = [primary_keyword] if primary_keyword else []

        # Secondary terms may include alternative identity-bearing phrases and
        # useful category/context queries, but never duplicate brand/model-only tokens.
        secondary = []
        for term in plan_terms:
            clean = SEOKeywordEngine.clean_keyword(term)
            if not clean or SEOKeywordEngine._is_brand_or_model_only(clean, brands, models):
                continue
            secondary.append(clean)
        if identity:
            secondary.append(identity)
        for brand in brands[:3]:
            if identity:
                secondary.append(f"{brand} {identity}")

        # Models belong in their own lane. Add identity context when no brand is
        # available so a bare code does not become the only search phrase.
        model_keywords = []
        for model in models[:15]:
            if brands:
                model_keywords.append(f"{brands[0]} {model}")
            elif identity:
                model_keywords.append(f"{model} {identity}")
            else:
                model_keywords.append(model)

        knowledge = profile.get("product_knowledge", {}) or {}
        ident = knowledge.get("identity", {}) or {}
        classification = knowledge.get("feature_classification", {}) or {}
        raw_features = []
        for value in [
            classification.get("specifications", []),
            classification.get("materials", []),
            ident.get("functional_features", []),
            ident.get("design_features", []),
        ]:
            raw_features.extend(SEOKeywordEngine._flatten(value))

        feature_keywords = [x for x in SEOKeywordEngine.semantic_unique(raw_features) if SEOKeywordEngine._searchable_feature(x)]

        # Backend search terms are search-oriented, not a dump of every feature.
        backend = []
        backend.extend(primary_keywords)
        backend.extend(secondary)
        backend.extend(model_keywords)
        backend.extend(feature_keywords)

        return {
            "version": SEOKeywordEngine.VERSION,
            "primary_keywords": SEOKeywordEngine.unique(primary_keywords),
            "secondary_keywords": SEOKeywordEngine.unique(secondary),
            "model_keywords": SEOKeywordEngine.unique(model_keywords),
            "feature_keywords": SEOKeywordEngine.unique(feature_keywords),
            "backend_search_terms": SEOKeywordEngine.semantic_unique(backend),
            "search_intent": SEOKeywordEngine._search_intent(profile),
        }

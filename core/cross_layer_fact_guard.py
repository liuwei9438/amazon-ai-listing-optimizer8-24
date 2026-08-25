from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


class CrossLayerFactGuard:
    VERSION = "v1.2-source-compatibility-rescue"

    _GENERIC_BRAND_TOKENS = {
        "for", "with", "use", "used", "compatible", "replacement",
        "cnc", "router", "machine", "machining", "center", "parts",
        "part", "suction", "vacuum", "sensor", "motor", "switch",
        "printer", "projector", "scooter", "mower", "chainsaw",
        "chinese", "china", "japanese", "japan", "american", "european",
        "german", "french", "italian", "spanish", "british", "korean",
        "fully", "automatic", "edgebanders", "edgebander", "lockstitch",
        "sewing", "machine", "most", "juicer", "juicers", "printer",
        "projectors", "projector", "blender", "chainsaw", "woodworking",
    }

    @staticmethod
    def _clean(value: Any) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _dict(value: Any) -> dict:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _list(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        out, seen = [], set()
        for item in value:
            text = CrossLayerFactGuard._clean(item)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        out, seen = [], set()
        for value in values:
            text = CrossLayerFactGuard._clean(value)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    @staticmethod
    def _source_text(profile: dict) -> str:
        ledger = CrossLayerFactGuard._dict(profile.get("source_fact_ledger"))
        snap = CrossLayerFactGuard._dict(ledger.get("source_snapshot"))
        values = [snap.get("title", ""), snap.get("description", "")]
        values.extend(CrossLayerFactGuard._list(snap.get("bullets", [])))
        raw = CrossLayerFactGuard._dict(ledger.get("raw_fields"))
        values.extend(raw.values())
        return " ".join(
            CrossLayerFactGuard._clean(v)
            for v in values
            if CrossLayerFactGuard._clean(v)
        )

    @staticmethod
    def _supported(value: str, source_text: str) -> bool:
        value = CrossLayerFactGuard._clean(value)
        return bool(value and source_text and value.casefold() in source_text.casefold())

    @staticmethod
    def _brand_from_notes(profile: dict, source_text: str) -> list[str]:
        compatibility = CrossLayerFactGuard._dict(profile.get("compatibility"))
        notes = CrossLayerFactGuard._list(
            compatibility.get("compatibility_notes", [])
        )

        patterns = (
            r"^\s*compatible\s+with\s+([A-Za-z][A-Za-z0-9._+\-]{1,39})\b",
            r"^\s*for\s+use\s+with\s+([A-Za-z][A-Za-z0-9._+\-]{1,39})\b",
            r"^\s*fits?\s+([A-Za-z][A-Za-z0-9._+\-]{1,39})\b",
        )

        out = []
        for note in notes:
            for pattern in patterns:
                m = re.search(pattern, note, flags=re.IGNORECASE)
                if not m:
                    continue
                candidate = CrossLayerFactGuard._clean(m.group(1))
                if (
                    len(candidate) >= 4
                    and candidate.casefold() not in CrossLayerFactGuard._GENERIC_BRAND_TOKENS
                    and (
                        candidate[0].isupper()
                        or candidate.isupper()
                    )
                    and CrossLayerFactGuard._supported(candidate, source_text)
                ):
                    out.append(candidate)
                break

        return CrossLayerFactGuard._dedupe(out)

    @staticmethod
    def _source_compatibility_brand_candidates(
        profile: dict,
        source_text: str,
    ) -> list[str]:
        """
        Recover brand-like compatibility targets only from explicit upstream
        compatibility notes, never from arbitrary source tokens.

        Supported evidence:
        - "Compatible with KDT Edge Banding Machine"
        - "Compatible with NANXING ..."
        - "Compatible with Kaabo Wolf King GT Pro ..."

        The candidate must occur in the original source text and must not be a
        generic device/category/nationality word.
        """
        compatibility = CrossLayerFactGuard._dict(
            profile.get("compatibility")
        )
        notes = CrossLayerFactGuard._list(
            compatibility.get(
                "compatibility_notes",
                [],
            )
        )

        result = []

        for note in notes:
            match = re.match(
                r"^\s*(?:compatible\s+with|for\s+use\s+with|fits?)\s+"
                r"([A-Za-z][A-Za-z0-9._+\-]{1,39})\b",
                note,
                flags=re.IGNORECASE,
            )

            if not match:
                continue

            candidate = CrossLayerFactGuard._clean(
                match.group(1)
            )

            brand_info = CrossLayerFactGuard._dict(
                profile.get("brand_info")
            )

            corroborated = {
                value.casefold()
                for value in (
                    CrossLayerFactGuard._list(
                        brand_info.get(
                            "detected_brands",
                            [],
                        )
                    )
                    +
                    CrossLayerFactGuard._list(
                        brand_info.get(
                            "third_party_brands",
                            [],
                        )
                    )
                )
            }

            acronym_like = (
                candidate.isupper()
                and
                3 <= len(candidate) <= 12
            )

            if (
                len(candidate) >= 3
                and candidate.casefold()
                not in
                CrossLayerFactGuard._GENERIC_BRAND_TOKENS
                and (
                    acronym_like
                    or
                    candidate.casefold()
                    in
                    corroborated
                )
                and CrossLayerFactGuard._supported(
                    candidate,
                    source_text,
                )
            ):
                result.append(
                    candidate
                )

        return CrossLayerFactGuard._dedupe(
            result
        )

    @staticmethod
    def _upstream_brands(profile: dict, source_text: str) -> list[str]:
        compatibility = CrossLayerFactGuard._dict(profile.get("compatibility"))
        brand_info = CrossLayerFactGuard._dict(profile.get("brand_info"))
        knowledge = CrossLayerFactGuard._dict(profile.get("product_knowledge"))
        relationship = CrossLayerFactGuard._dict(knowledge.get("relationship"))

        candidates = []
        candidates += CrossLayerFactGuard._list(compatibility.get("brands", []))
        candidates += CrossLayerFactGuard._list(brand_info.get("third_party_brands", []))
        # detected_brands are intentionally NOT accepted directly here.
        # They are lower-confidence observations and must pass explicit source
        # compatibility grammar in _explicit_for_brand_candidates().
        candidates += CrossLayerFactGuard._list(relationship.get("brands", []))
        candidates += CrossLayerFactGuard._brand_from_notes(profile, source_text)
        candidates += (
            CrossLayerFactGuard
            ._source_compatibility_brand_candidates(
                profile,
                source_text,
            )
        )
        candidates += (
            CrossLayerFactGuard
            ._explicit_for_brand_candidates(
                profile,
                source_text,
            )
        )

        return CrossLayerFactGuard._dedupe([
            x for x in candidates
            if CrossLayerFactGuard._supported(x, source_text)
        ])

    @staticmethod
    def _explicit_for_brand_candidates(
        profile: dict,
        source_text: str,
    ) -> list[str]:
        """
        Rescue already-detected brands when the original title explicitly
        presents them as a compatibility target ("for Brand ...").

        This is not a brand detector.  It only validates an upstream detected
        brand against explicit source grammar.
        """
        brand_info = CrossLayerFactGuard._dict(
            profile.get("brand_info")
        )

        detected = CrossLayerFactGuard._list(
            brand_info.get(
                "detected_brands",
                [],
            )
        )

        result = []

        for brand in detected:
            if (
                brand.casefold()
                in
                CrossLayerFactGuard._GENERIC_BRAND_TOKENS
            ):
                continue

            escaped = re.escape(brand)

            if re.search(
                rf"\bfor\s+{escaped}\b",
                source_text,
                flags=re.IGNORECASE,
            ):
                result.append(
                    brand
                )

        return CrossLayerFactGuard._dedupe(
            result
        )

    @staticmethod
    def _is_code_like_unknown(value: str) -> bool:
        value = CrossLayerFactGuard._clean(value)
        if not value or len(value) < 4 or len(value) > 40:
            return False

        code_pattern = (
            r"(?:[A-Za-z0-9]+(?:[._/+\-][A-Za-z0-9]+)+)"
            r"|(?:[A-Za-z]*\d[A-Za-z0-9._/+\-]*)"
        )
        if not re.fullmatch(code_pattern, value):
            return False

        if re.fullmatch(
            r"\d+(?:\.\d+)?(?:[x×]\d+(?:\.\d+)?){1,3}(?:mm|cm|m|in)?",
            value,
            flags=re.IGNORECASE,
        ):
            return False

        if re.fullmatch(
            r"\d+(?:\.\d+)?(?:v|w|kw|a|ma|hz|kg|g|lb|oz|mm|cm|m|in)",
            value,
            flags=re.IGNORECASE,
        ):
            return False

        return True

    @staticmethod
    def _buyer_identity_supports(profile: dict, value: str) -> bool:
        identity = CrossLayerFactGuard._dict(profile.get("product_identity"))
        buyer = CrossLayerFactGuard._clean(
            identity.get("buyer_search_identity", "")
        )
        return bool(buyer and value.casefold() in buyer.casefold())

    @staticmethod
    def _upstream_models(profile: dict, source_text: str) -> tuple[list[str], list[str]]:
        identifiers = CrossLayerFactGuard._dict(profile.get("identifiers"))
        fact_lock = CrossLayerFactGuard._dict(profile.get("fact_lock"))
        search = CrossLayerFactGuard._dict(profile.get("search_strategy"))
        knowledge = CrossLayerFactGuard._dict(profile.get("product_knowledge"))
        relationship = CrossLayerFactGuard._dict(knowledge.get("relationship"))
        priority = CrossLayerFactGuard._dict(
            relationship.get("model_priority")
        )

        primary = []
        secondary = []

        model_numbers = CrossLayerFactGuard._list(
            identifiers.get("model_numbers", [])
        )
        primary += model_numbers[:1]
        secondary += model_numbers[1:]

        for value in (
            search.get("primary_model", ""),
            priority.get("primary_model", ""),
        ):
            text = CrossLayerFactGuard._clean(value)
            if text:
                primary.append(text)

        secondary += CrossLayerFactGuard._list(
            identifiers.get("part_numbers", [])
        )
        secondary += CrossLayerFactGuard._list(
            fact_lock.get("compatible_models", [])
        )
        secondary += CrossLayerFactGuard._list(
            fact_lock.get("part_numbers", [])
        )
        secondary += CrossLayerFactGuard._list(
            priority.get("secondary_models", [])
        )
        secondary += CrossLayerFactGuard._list(
            relationship.get("models", [])
        )
        secondary += CrossLayerFactGuard._list(
            relationship.get("part_numbers", [])
        )

        for value in CrossLayerFactGuard._list(
            identifiers.get("unknown_codes", [])
        ):
            if (
                CrossLayerFactGuard._is_code_like_unknown(value)
                and CrossLayerFactGuard._buyer_identity_supports(profile, value)
                and CrossLayerFactGuard._supported(value, source_text)
            ):
                primary.append(value)

        primary = CrossLayerFactGuard._dedupe([
            x for x in primary
            if CrossLayerFactGuard._supported(x, source_text)
        ])
        secondary = CrossLayerFactGuard._dedupe([
            x for x in secondary
            if CrossLayerFactGuard._supported(x, source_text)
        ])

        return primary, secondary

    @staticmethod
    def reconcile(profile: dict, normalized_knowledge: dict) -> dict:
        normalized = (
            deepcopy(normalized_knowledge)
            if isinstance(normalized_knowledge, dict)
            else {}
        )
        if not isinstance(profile, dict):
            return normalized

        source_text = CrossLayerFactGuard._source_text(profile)

        compat = CrossLayerFactGuard._dict(
            normalized.get("compatibility")
        )
        models = CrossLayerFactGuard._dict(
            normalized.get("models")
        )

        old_brands = CrossLayerFactGuard._list(
            compat.get("brands", [])
        )
        old_primary = CrossLayerFactGuard._clean(
            models.get("primary", "")
        )
        old_secondary = CrossLayerFactGuard._list(
            models.get("secondary", [])
        )
        old_all = CrossLayerFactGuard._list(
            models.get("all", [])
        )

        # Rescue-only semantics:
        # do not enrich healthy normalized output.  Only restore a field when
        # Normalization has emptied a core fact that already exists upstream.
        brands = old_brands

        if not brands:
            brands = CrossLayerFactGuard._upstream_brands(
                profile,
                source_text,
            )

        primary_candidates, secondary_candidates = (
            CrossLayerFactGuard._upstream_models(profile, source_text)
        )

        primary = old_primary
        secondary = old_secondary
        all_models = old_all

        primary_was_rescued = False

        if not primary and primary_candidates:
            primary = primary_candidates[0]
            primary_was_rescued = True

        # Only when the primary model itself had to be rescued do we restore
        # source-locked secondary identifiers. This avoids expanding model
        # lists on already healthy rows.
        if primary_was_rescued:
            secondary = CrossLayerFactGuard._dedupe(
                [
                    x for x in secondary_candidates
                    if x.casefold() != primary.casefold()
                ]
            )

        # High-value fact rescue:
        # If Normalization erased the model view completely, but Fact Lock
        # already contains source-supported identifiers, preserve them in
        # models.all as optional search facts. Do NOT invent a primary model.
        #
        # This fixes cases such as Canon numeric compatibility lists and
        # Husqvarna numeric models while remaining rescue-only.
        if (
            not primary
            and not secondary
            and not all_models
            and secondary_candidates
        ):
            all_models = CrossLayerFactGuard._dedupe(
                secondary_candidates
            )

        phrase = CrossLayerFactGuard._clean(
            compat.get("phrase", "")
        )
        if brands and not phrase:
            phrase = "Compatible with " + ", ".join(brands)

        normalized["compatibility"] = {
            **compat,
            "phrase": phrase,
            "brands": brands,
        }
        normalized["models"] = {
            **models,
            "all": all_models,
            "primary": primary,
            "secondary": secondary,
        }

        changes = []
        if brands != old_brands:
            changes.append({
                "field": "compatibility.brands",
                "before": old_brands,
                "after": brands,
            })
        if primary != old_primary:
            changes.append({
                "field": "models.primary",
                "before": old_primary,
                "after": primary,
            })
        if secondary != old_secondary:
            changes.append({
                "field": "models.secondary",
                "before": old_secondary,
                "after": secondary,
            })

        if all_models != old_all:
            changes.append({
                "field": "models.all",
                "before": old_all,
                "after": all_models,
            })

        normalized["cross_layer_fact_guard"] = {
            "version": CrossLayerFactGuard.VERSION,
            "changed": bool(changes),
            "changes": changes,
        }
        return normalized

from __future__ import annotations

import re
from typing import Any


class SourceCompatibilityFactProtector:
    """
    Recover compatibility brands only when the original source gives explicit
    compatibility grammar. This module is intentionally conservative: it is a
    source-fact conservation layer, not a general brand detector.
    """

    VERSION = "v1.0-explicit-source-compatibility-brand-protection"

    _GENERIC_TOKENS = {
        "for", "with", "use", "used", "compatible", "replacement",
        "accessory", "accessories", "part", "parts", "spare", "wholesale",
        "cnc", "router", "machine", "machining", "center", "woodworking",
        "edgebanding", "edgebander", "edge", "banding", "printer", "projector",
        "scooter", "mower", "chainsaw", "sewing", "washing", "refrigerator",
        "freezer", "dishwasher", "vacuum", "cleaner", "sensor", "motor",
        "switch", "wheel", "roller", "cup", "block", "pad", "board",
        "computer", "models", "model", "series", "kit", "set", "black",
        "white", "red", "blue", "green", "upper", "lower", "front", "rear",
        "bridge", "neck", "hsh", "sss", "hss", "tv", "pc",
        "chinese", "china", "japanese", "japan", "american", "european",
        "german", "french", "italian", "spanish", "british", "korean",
        "fully", "automatic", "edgebanders", "lockstitch", "most",
    }

    # A source-only candidate is accepted only if the immediate clause also
    # looks like an application/device context. This avoids promoting arbitrary
    # model/series words after "for" into brands.
    _APPLICATION_MARKERS = {
        "cnc", "router", "machine", "machining", "center", "woodworking",
        "edgebanding", "edgebander", "edge", "banding", "printer", "projector",
        "scooter", "mower", "chainsaw", "sewing", "washing", "refrigerator",
        "freezer", "dishwasher", "vacuum", "cleaner", "juicer", "blender",
        "robot", "3d", "laser", "lathe", "drill", "compressor", "appliance",
    }

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _list(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = SourceCompatibilityFactProtector._clean(item)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    @staticmethod
    def _source_title(profile: dict) -> str:
        ledger = profile.get("source_fact_ledger", {}) if isinstance(profile, dict) else {}
        ledger = ledger if isinstance(ledger, dict) else {}
        snapshot = ledger.get("source_snapshot", {})
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        return SourceCompatibilityFactProtector._clean(snapshot.get("title", ""))

    @staticmethod
    def _known_brand_candidates(profile: dict) -> list[str]:
        compatibility = profile.get("compatibility", {}) if isinstance(profile, dict) else {}
        compatibility = compatibility if isinstance(compatibility, dict) else {}
        brand_info = profile.get("brand_info", {}) if isinstance(profile, dict) else {}
        brand_info = brand_info if isinstance(brand_info, dict) else {}
        knowledge = profile.get("product_knowledge", {}) if isinstance(profile, dict) else {}
        knowledge = knowledge if isinstance(knowledge, dict) else {}
        relationship = knowledge.get("relationship", {})
        relationship = relationship if isinstance(relationship, dict) else {}

        values: list[str] = []
        values += SourceCompatibilityFactProtector._list(compatibility.get("brands", []))
        values += SourceCompatibilityFactProtector._list(brand_info.get("third_party_brands", []))
        values += SourceCompatibilityFactProtector._list(brand_info.get("detected_brands", []))
        values += SourceCompatibilityFactProtector._list(relationship.get("brands", []))
        return SourceCompatibilityFactProtector._list(values)

    @staticmethod
    def _is_identifier_shape(value: str) -> bool:
        value = SourceCompatibilityFactProtector._clean(value)
        if not value:
            return True
        if any(ch.isdigit() for ch in value):
            return True
        if re.fullmatch(r"[A-Z0-9]+(?:[._/+*\-][A-Z0-9]+)+", value):
            return True
        return False

    @staticmethod
    def _explicitly_linked(title: str, brand: str) -> bool:
        if not title or not brand:
            return False
        escaped = re.escape(brand)
        return bool(re.search(
            rf"\b(?:compatible\s+with|for\s+use\s+with|for|fits?)\s+{escaped}\b",
            title,
            flags=re.IGNORECASE,
        ))

    @staticmethod
    def _source_only_candidates(title: str) -> list[dict]:
        result: list[dict] = []
        if not title:
            return result

        pattern = re.compile(
            r"\b(?:compatible\s+with|for\s+use\s+with|for|fits?)\s+"
            r"([A-Za-z][A-Za-z0-9._/+&'*-]{1,39})",
            flags=re.IGNORECASE,
        )

        for match in pattern.finditer(title):
            candidate = SourceCompatibilityFactProtector._clean(match.group(1)).strip(" ,.;:")
            key = candidate.casefold()
            if (
                not candidate
                or key in SourceCompatibilityFactProtector._GENERIC_TOKENS
                or SourceCompatibilityFactProtector._is_identifier_shape(candidate)
            ):
                continue

            # Examine only the local clause following the candidate. A
            # source-only rescue needs a nearby machine/application marker.
            tail = title[match.end():]
            tail = re.split(r"\bfor\b|[;,]", tail, maxsplit=1, flags=re.IGNORECASE)[0]
            local_tokens = [
                token.casefold()
                for token in re.findall(r"[A-Za-z0-9]+", tail)[:4]
            ]
            has_application_marker = any(
                token in SourceCompatibilityFactProtector._APPLICATION_MARKERS
                for token in local_tokens
            )
            if not has_application_marker:
                continue

            if not re.fullmatch(r"[A-Za-z][A-Za-z&']{1,39}", candidate):
                continue

            if key not in {item["brand"].casefold() for item in result}:
                result.append({
                    "brand": candidate,
                    "evidence_type": "explicit_source_for_clause_with_application_context",
                    "source_text": match.group(0) + (" " + " ".join(local_tokens) if local_tokens else ""),
                })

        return result

    @staticmethod
    def extract(profile: dict) -> dict:
        title = SourceCompatibilityFactProtector._source_title(profile)
        evidence: list[dict] = []
        brands: list[str] = []

        # First preserve brands already observed upstream, but only when the
        # original title explicitly connects them via compatibility grammar.
        for brand in SourceCompatibilityFactProtector._known_brand_candidates(profile):
            if (
                brand.casefold() in SourceCompatibilityFactProtector._GENERIC_TOKENS
                or SourceCompatibilityFactProtector._is_identifier_shape(brand)
                or not SourceCompatibilityFactProtector._explicitly_linked(title, brand)
            ):
                continue
            if brand.casefold() not in {x.casefold() for x in brands}:
                brands.append(brand)
                evidence.append({
                    "brand": brand,
                    "evidence_type": "explicit_source_grammar_plus_upstream_brand_observation",
                    "source_text": title,
                })

        # Then allow a narrowly-scoped source-only rescue. This is the path
        # that protects cases such as "for Nanxing CNC ..." even when the AI
        # brand fields are empty.
        for item in SourceCompatibilityFactProtector._source_only_candidates(title):
            brand = item["brand"]
            if brand.casefold() not in {x.casefold() for x in brands}:
                brands.append(brand)
                evidence.append(item)

        return {
            "version": SourceCompatibilityFactProtector.VERSION,
            "protected_brands": brands,
            "evidence": evidence,
        }

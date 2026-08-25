from __future__ import annotations
import re
from typing import Any


class TitleFinalValidator:
    """Stable Title Pipeline V1.0: final deterministic release gate."""

    VERSION = "stable-v1.4-source-insufficient-aware"
    BLOCKED = {
        "best seller", "#1", "premium", "original", "genuine",
        "official", "authentic", "oem", "wholesale",
    }

    QUALIFIERS = {
        "english": ["compatible with"],
        "spanish": ["compatible con"],
        "french": ["compatible avec"],
        "german": ["kompatibel mit"],
        "italian": ["compatibile con"],
        "portuguese": ["compatível com", "compativel com"],
        "dutch": ["compatibel met"],
        "swedish": ["kompatibel med"],
        "japanese": ["対応", "互換"],
    }

    @staticmethod
    def _clean(v: Any) -> str:
        return re.sub(r"\s+", " ", str(v or "")).strip()

    @staticmethod
    def _qualifiers(language: str) -> list[str]:
        folded = TitleFinalValidator._clean(language).casefold()
        for key, values in TitleFinalValidator.QUALIFIERS.items():
            if key in folded:
                return values
        return ["compatible with"]

    @staticmethod
    def validate(composed: dict, resolved: dict, target_language="English") -> dict:
        title = TitleFinalValidator._clean(composed.get("title"))
        fold = title.casefold()
        approved = resolved.get("approved_facts", [])
        approved = approved if isinstance(approved, list) else []

        errors = []

        composition_status = TitleFinalValidator._clean(
            composed.get("status")
        ).upper()

        # 75 is the hard marketplace budget.
        if len(title) > 75:
            errors.append("TITLE_ABOVE_75")

        # 61 is a target band, not permission to invent filler.  A title below
        # 61 is valid only when the deterministic composer has proven that no
        # additional complete, approved source fact can fit safely.
        if (
            len(title) < 61
            and
            composition_status
            !=
            "SOURCE_FACTS_INSUFFICIENT"
        ):
            errors.append("TITLE_BELOW_61_WITH_UNUSED_FACT_CAPACITY")

        used_facts = composed.get(
            "used_facts",
            [],
        )

        if not isinstance(
            used_facts,
            list,
        ):
            used_facts = []

        used_by_id = {
            TitleFinalValidator._clean(
                item.get(
                    "fact_id"
                )
            ):
                TitleFinalValidator._clean(
                    item.get(
                        "selected_text"
                    )
                )
            for item in used_facts
            if (
                isinstance(
                    item,
                    dict,
                )
                and
                TitleFinalValidator._clean(
                    item.get(
                        "fact_id"
                    )
                )
            )
        }

        required = [
            fact
            for fact in approved
            if (
                isinstance(
                    fact,
                    dict,
                )
                and
                fact.get(
                    "required"
                )
            )
        ]

        for fact in required:

            fact_id = (
                TitleFinalValidator
                ._clean(
                    fact.get(
                        "fact_id"
                    )
                )
            )

            original_text = (
                TitleFinalValidator
                ._clean(
                    fact.get(
                        "text"
                    )
                )
            )

            selected_text = (
                used_by_id.get(
                    fact_id,
                    original_text,
                )
                or
                original_text
            )

            typ = (
                TitleFinalValidator
                ._clean(
                    fact.get(
                        "type"
                    )
                )
                .upper()
            )

            if typ == "QUANTITY":

                if (
                    selected_text
                    and
                    not fold.startswith(
                        selected_text.casefold()
                    )
                ):
                    errors.append(
                        "QUANTITY_RULE_FAILED"
                    )

            elif typ == "COMPATIBILITY_BRAND":

                # Brand itself must still be present even though the selected
                # rendered expression is "Compatible with Brand".
                if (
                    original_text
                    and
                    original_text.casefold()
                    not in
                    fold
                ):
                    errors.append(
                        "COMPATIBILITY_BRAND_MISSING"
                    )

                if not any(
                    qualifier.casefold()
                    in
                    fold
                    for qualifier
                    in
                    TitleFinalValidator
                    ._qualifiers(
                        target_language
                    )
                ):
                    errors.append(
                        "COMPATIBILITY_QUALIFIER_MISSING"
                    )

            elif (
                selected_text
                and
                selected_text.casefold()
                not in
                fold
            ):
                errors.append(
                    f"REQUIRED_{typ}_MISSING"
                )

        # Any identifier/spec/brand-like token used as an approved fact must
        # have come through the resolver; rejected facts may never appear.
        rejected = resolved.get("rejected_facts", [])
        rejected = rejected if isinstance(rejected, list) else []

        approved_texts = {
            TitleFinalValidator._clean(x.get("text")).casefold()
            for x in approved
            if isinstance(x, dict)
            and TitleFinalValidator._clean(x.get("text"))
        }

        forbidden_hits = []
        for fact in rejected:
            if not isinstance(fact, dict):
                continue
            typ = TitleFinalValidator._clean(fact.get("type")).upper()
            if typ not in {
                "MODEL", "PART_NUMBER", "COMPATIBILITY_MODEL",
                "COMPATIBILITY_BRAND", "SPECIFICATION",
            }:
                continue
            text = TitleFinalValidator._clean(
                fact.get(
                    "text"
                )
            )

            if not text:
                continue

            covered_by_approved_fact = any(
                (
                    text.casefold()
                    in
                    approved_text
                    and
                    approved_text
                    in
                    fold
                )
                for approved_text
                in
                approved_texts
            )

            if (
                text.casefold()
                not in
                approved_texts
                and
                text.casefold()
                in
                fold
                and
                not covered_by_approved_fact
            ):
                forbidden_hits.append(
                    text
                )

        if forbidden_hits:
            errors.append("UNAPPROVED_FACT_IN_TITLE")

        if any(word in fold for word in TitleFinalValidator.BLOCKED):
            errors.append("BLOCKED_MARKETING_TERM")

        # Discrete numeric models must not be converted to a range.
        numeric_models = {
            TitleFinalValidator._clean(x.get("text"))
            for x in approved
            if isinstance(x, dict)
            and x.get("type") == "COMPATIBILITY_MODEL"
            and re.fullmatch(r"\d{2,}", TitleFinalValidator._clean(x.get("text")))
        }
        for m in re.finditer(r"\b(\d{2,})\s*[-–—]\s*(\d{2,})\b", title):
            if m.group(1) in numeric_models and m.group(2) in numeric_models:
                errors.append("MODEL_RANGE_COMPRESSION_FORBIDDEN")
                break

        errors = list(dict.fromkeys(errors))
        return {
            "version": TitleFinalValidator.VERSION,
            "title": title,
            "character_count": len(title),
            "status": "PASS" if not errors else "FAIL",
            "errors": errors,
            "forbidden_fact_hits": forbidden_hits,
        }

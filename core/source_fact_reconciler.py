from __future__ import annotations

import re
from typing import Any


class SourceFactReconciler:
    """
    Source Fact Reconciliation V1.0

    Compares preserved source evidence against AI-understood product data.

    Important:
    - does NOT invent classifications
    - does NOT force every source term into the title
    - identifies facts that survived, were filtered, or remain unresolved
    - prevents "source had it, pipeline silently lost it"
    """

    SCHEMA_VERSION = "1.0-source-fact-reconciliation"

    @staticmethod
    def _clean(value: Any) -> str:
        if value is None:
            return ""

        return re.sub(
            r"\s+",
            " ",
            str(value),
        ).strip()

    @staticmethod
    def _flatten_strings(value: Any) -> list[str]:
        result: list[str] = []

        if isinstance(value, str):
            if SourceFactReconciler._clean(value):
                result.append(
                    SourceFactReconciler._clean(value)
                )

        elif isinstance(value, dict):
            for child in value.values():
                result.extend(
                    SourceFactReconciler._flatten_strings(
                        child
                    )
                )

        elif isinstance(value, (list, tuple, set)):
            for child in value:
                result.extend(
                    SourceFactReconciler._flatten_strings(
                        child
                    )
                )

        return result

    @staticmethod
    def _contains(
        haystack: str,
        needle: str,
    ) -> bool:
        needle = SourceFactReconciler._clean(needle)

        if not needle:
            return False

        return needle.casefold() in haystack.casefold()

    @staticmethod
    def _meaningful_words(text: str) -> list[str]:
        stop = {
            "for",
            "with",
            "and",
            "or",
            "the",
            "a",
            "an",
            "to",
            "of",
            "in",
            "on",
            "from",
            "parts",
            "part",
            "spare",
            "accessories",
            "accessory",
        }

        words = re.findall(
            r"[A-Za-z0-9]+(?:[-/+.][A-Za-z0-9]+)*",
            SourceFactReconciler._clean(text),
        )

        return [
            word
            for word in words
            if (
                len(word) >= 3
                and
                word.casefold() not in stop
            )
        ]

    @staticmethod
    def _phrase_coverage(
        phrase: str,
        semantic_text: str,
    ) -> float:
        words = SourceFactReconciler._meaningful_words(
            phrase
        )

        if not words:
            return 1.0

        covered = sum(
            1
            for word in words
            if word.casefold()
            in semantic_text.casefold()
        )

        return covered / len(words)

    @staticmethod
    def reconcile(
        profile: dict[str, Any],
        ledger: dict[str, Any],
    ) -> dict[str, Any]:

        if not isinstance(profile, dict):
            profile = {}

        if not isinstance(ledger, dict):
            ledger = {}

        high_confidence = ledger.get(
            "high_confidence",
            {},
        )

        if not isinstance(high_confidence, dict):
            high_confidence = {}

        source_evidence = ledger.get(
            "source_evidence",
            {},
        )

        if not isinstance(source_evidence, dict):
            source_evidence = {}

        # Fields considered "AI-understood semantic output".
        semantic_fields = {
            "product_identity":
                profile.get(
                    "product_identity",
                    {},
                ),
            "identifiers":
                profile.get(
                    "identifiers",
                    {},
                ),
            "basic_info":
                profile.get(
                    "basic_info",
                    {},
                ),
            "title_information":
                profile.get(
                    "title_information",
                    {},
                ),
            "brand_info":
                profile.get(
                    "brand_info",
                    {},
                ),
            "compatibility":
                profile.get(
                    "compatibility",
                    {},
                ),
            "specifications":
                profile.get(
                    "specifications",
                    {},
                ),
            "attributes":
                profile.get(
                    "attributes",
                    {},
                ),
            "search_strategy":
                profile.get(
                    "search_strategy",
                    {},
                ),
        }

        semantic_text = " ".join(
            SourceFactReconciler._flatten_strings(
                semantic_fields
            )
        )

        seller_brand = (
            SourceFactReconciler._clean(
                (
                    profile.get(
                        "brand_info",
                        {},
                    )
                    if isinstance(
                        profile.get(
                            "brand_info",
                            {},
                        ),
                        dict,
                    )
                    else
                    {}
                ).get(
                    "seller_brand",
                    "",
                )
            )
        )

        identifier_audit = []

        unresolved_identifiers = []

        for identifier in high_confidence.get(
            "identifier_candidates",
            [],
        ):
            identifier = SourceFactReconciler._clean(
                identifier
            )

            if not identifier:
                continue

            represented = (
                SourceFactReconciler._contains(
                    semantic_text,
                    identifier,
                )
            )

            disposition = (
                "represented"
                if represented
                else
                "unresolved"
            )

            if (
                seller_brand
                and
                identifier.casefold()
                ==
                seller_brand.casefold()
            ):
                disposition = "filtered_seller_brand"

            identifier_audit.append(
                {
                    "text":
                        identifier,
                    "disposition":
                        disposition,
                }
            )

            if disposition == "unresolved":
                unresolved_identifiers.append(
                    identifier
                )

        phrase_audit = []

        unresolved_phrases = []

        for phrase in source_evidence.get(
            "title_segments",
            [],
        ):
            phrase = SourceFactReconciler._clean(
                phrase
            )

            if not phrase:
                continue

            coverage = (
                SourceFactReconciler._phrase_coverage(
                    phrase,
                    semantic_text,
                )
            )

            if coverage >= 0.70:
                disposition = "represented"
            elif coverage >= 0.35:
                disposition = "partially_represented"
            else:
                disposition = "unresolved"

            phrase_audit.append(
                {
                    "text":
                        phrase,
                    "semantic_coverage":
                        round(
                            coverage,
                            3,
                        ),
                    "disposition":
                        disposition,
                }
            )

            if disposition in {
                "unresolved",
                "partially_represented",
            }:
                unresolved_phrases.append(
                    phrase
                )

        unresolved_high_value = []

        for item in unresolved_identifiers:
            if item not in unresolved_high_value:
                unresolved_high_value.append(
                    item
                )

        for item in unresolved_phrases:
            if item not in unresolved_high_value:
                unresolved_high_value.append(
                    item
                )

        return {
            "schema_version":
                SourceFactReconciler.SCHEMA_VERSION,

            "identifier_audit":
                identifier_audit,

            "phrase_audit":
                phrase_audit,

            "unresolved_identifiers":
                unresolved_identifiers,

            "unresolved_source_phrases":
                unresolved_phrases,

            "unresolved_high_value":
                unresolved_high_value,

            "coverage_status":
                (
                    "review_needed"
                    if unresolved_high_value
                    else
                    "complete"
                ),

            "silent_drop_detected":
                bool(
                    unresolved_high_value
                ),
        }

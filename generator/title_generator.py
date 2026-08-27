from __future__ import annotations

import re

from core.title_fact_gate import TitleFactGate


class TitleGenerator:
    """
    V6.0 Final Title Validator

    Architecture:
    - AI Title Strategy already composes the complete final_title.
    - This module NEVER semantically rewrites/crops candidate phrases.
    - It validates hard requirements and exposes exact failure reasons.
    """

    VERSION = "V7.0-fail-closed-source-trace-validator"

    BLOCKED_WORDS = [
        "best seller",
        "#1",
        "premium",
        "original",
        "genuine",
        "official",
        "authentic",
        "oem",
    ]

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""

        return re.sub(
            r"\s+",
            " ",
            str(value),
        ).strip()

    @staticmethod
    def _list(value) -> list[str]:
        if not isinstance(value, list):
            return []

        result = []
        seen = set()

        for item in value:
            item = TitleGenerator._clean(item)

            if not item:
                continue

            key = item.casefold()

            if key in seen:
                continue

            seen.add(key)
            result.append(item)

        return result

    @staticmethod
    def _compact_quantity(value) -> str:
        text = TitleGenerator._clean(value)

        if not text:
            return ""

        match = re.search(
            r"\b(\d{1,4})\s*(?:pcs?|pieces?|piece|sets?)\b",
            text,
            flags=re.IGNORECASE,
        )

        if not match:
            return ""

        try:
            count = int(match.group(1))
        except Exception:
            return ""

        if count <= 1:
            return ""

        return f"{count}pcs"

    @staticmethod
    def check_blocked_words(title: str) -> list[str]:
        title_fold = TitleGenerator._clean(title).casefold()

        return [
            word
            for word in TitleGenerator.BLOCKED_WORDS
            if word.casefold() in title_fold
        ]

    @staticmethod
    def _compatibility_qualifiers(language: str) -> list[str]:
        language = TitleGenerator._clean(language).casefold()

        mapping = {
            "english": ["compatible with"],
            "spanish": ["compatible con"],
            "español": ["compatible con"],
            "french": ["compatible avec"],
            "français": ["compatible avec"],
            "german": ["kompatibel mit"],
            "deutsch": ["kompatibel mit"],
            "italian": ["compatibile con"],
            "portuguese": ["compatível com", "compativel com"],
            "dutch": ["compatibel met"],
            "swedish": ["kompatibel med"],
            "japanese": ["対応", "互換"],
        }

        for key, values in mapping.items():
            if key in language:
                return values

        return sorted(
            {
                item
                for values in mapping.values()
                for item in values
            }
        )

    @staticmethod
    def _identity_variants(
        strategy: dict,
        strategy_input: dict,
    ) -> list[str]:

        locked = (
            strategy_input.get("locked", {})
            if isinstance(strategy_input, dict)
            else {}
        )

        identity = (
            locked.get("identity", {})
            if isinstance(locked.get("identity", {}), dict)
            else {}
        )

        variants = [
            TitleGenerator._clean(
                identity.get("text", "")
            )
        ]

        for candidate in strategy.get(
            "title_candidates",
            [],
        ):
            if not isinstance(candidate, dict):
                continue

            if TitleGenerator._clean(
                candidate.get("type", "")
            ).upper() != "IDENTITY":
                continue

            variants.extend(
                [
                    TitleGenerator._clean(
                        candidate.get("text", "")
                    ),
                    TitleGenerator._clean(
                        candidate.get("short_text", "")
                    ),
                ]
            )

        result = []

        for value in variants:
            if (
                value
                and
                value not in result
            ):
                result.append(value)

        return result

    @staticmethod
    def _all_verified_models(
        profile: dict,
        strategy_input: dict,
    ) -> list[str]:

        result = []

        def add(values):
            if isinstance(values, list):
                source_values = values
            elif values:
                source_values = [values]
            else:
                source_values = []

            for value in source_values:
                value = TitleGenerator._clean(value)

                if (
                    value
                    and
                    value not in result
                ):
                    result.append(value)

        locked = strategy_input.get(
            "locked",
            {},
        )

        if isinstance(locked, dict):
            models = locked.get(
                "models",
                {},
            )

            if isinstance(models, dict):
                add(models.get("all", []))
                add(models.get("primary", ""))
                add(models.get("secondary", []))

        compatibility_facts = strategy_input.get(
            "compatibility_facts",
            {},
        )

        if isinstance(compatibility_facts, dict):
            add(
                compatibility_facts.get(
                    "models",
                    [],
                )
            )
            add(
                compatibility_facts.get(
                    "part_numbers",
                    [],
                )
            )
            add(
                compatibility_facts.get(
                    "important_compatibility",
                    [],
                )
            )

        compatibility = profile.get(
            "compatibility",
            {},
        )

        if isinstance(compatibility, dict):
            add(
                compatibility.get(
                    "models",
                    [],
                )
            )
            add(
                compatibility.get(
                    "part_numbers",
                    [],
                )
            )

        return result

    @staticmethod
    def generate(
        profile: dict,
    ) -> dict:

        if not isinstance(profile, dict):
            raise ValueError(
                "TitleGenerator profile must be a dictionary"
            )

        strategy = profile.get(
            "title_strategy",
            {},
        )

        strategy_input = profile.get(
            "title_strategy_input",
            {},
        )

        if not isinstance(strategy, dict):
            strategy = {}

        if not isinstance(strategy_input, dict):
            strategy_input = {}

        title = TitleGenerator._clean(
            strategy.get(
                "final_title",
                "",
            )
        )

        if not title:
            raise ValueError(
                "V6 final_title missing from title_strategy"
            )

        title_fold = title.casefold()
        errors = []

        fact_gate = TitleFactGate.build(
            profile
        )

        approved_facts = (
            fact_gate.get(
                "approved_facts",
                [],
            )
            if isinstance(
                fact_gate,
                dict,
            )
            else
            []
        )

        rejected_facts = (
            fact_gate.get(
                "rejected_facts",
                [],
            )
            if isinstance(
                fact_gate,
                dict,
            )
            else
            []
        )

        # -------------------------------------------------
        # Length
        # -------------------------------------------------
        min_length_ok = len(title) >= 61
        max_length_ok = len(title) <= 75

        if not min_length_ok:
            errors.append(
                "title_below_61_characters"
            )

        if not max_length_ok:
            errors.append(
                "title_above_75_characters"
            )

        # -------------------------------------------------
        # Identity
        # -------------------------------------------------
        identity_variants = (
            TitleGenerator
            ._identity_variants(
                strategy,
                strategy_input,
            )
        )

        identity_present = any(
            value.casefold() in title_fold
            for value in identity_variants
        )

        if not identity_present:
            errors.append(
                "identity_missing"
            )

        # -------------------------------------------------
        # Quantity
        # -------------------------------------------------
        candidate_facts = strategy_input.get(
            "candidate_facts",
            {},
        )

        if not isinstance(candidate_facts, dict):
            candidate_facts = {}

        quantity = (
            TitleGenerator
            ._compact_quantity(
                candidate_facts.get(
                    "important_quantity",
                    "",
                )
            )
        )

        if not quantity:
            source_evidence = strategy_input.get(
                "source_evidence",
                {},
            )

            if isinstance(source_evidence, dict):
                source_quantities = (
                    source_evidence.get(
                        "quantities",
                        [],
                    )
                    if isinstance(
                        source_evidence.get(
                            "quantities",
                            [],
                        ),
                        list,
                    )
                    else
                    []
                )

                if source_quantities:
                    quantity = (
                        TitleGenerator
                        ._compact_quantity(
                            source_quantities[0]
                        )
                    )

        quantity_present = (
            True
            if not quantity
            else
            title_fold.startswith(
                quantity.casefold()
            )
        )

        if not quantity_present:
            errors.append(
                "multi_unit_quantity_missing_or_not_prefixed"
            )

        # -------------------------------------------------
        # Compatibility
        # -------------------------------------------------
        compatibility_facts = strategy_input.get(
            "compatibility_facts",
            {},
        )

        if not isinstance(
            compatibility_facts,
            dict,
        ):
            compatibility_facts = {}

        brands = TitleGenerator._list(
            compatibility_facts.get(
                "brands",
                [],
            )
        )

        if not brands:
            brands = TitleGenerator._list(
                compatibility_facts.get(
                    "third_party_brands",
                    [],
                )
            )

        compatibility_required = bool(
            brands
        )

        compatibility_brand_present = (
            True
            if not brands
            else
            any(
                brand.casefold()
                in
                title_fold
                for brand in brands
            )
        )

        qualifiers = (
            TitleGenerator
            ._compatibility_qualifiers(
                strategy_input.get(
                    "target_language",
                    "English",
                )
            )
        )

        compatibility_qualifier_present = (
            True
            if not brands
            else
            any(
                qualifier.casefold()
                in
                title_fold
                for qualifier in qualifiers
            )
        )

        if (
            brands
            and
            not compatibility_brand_present
        ):
            errors.append(
                "compatibility_brand_missing"
            )

        if (
            brands
            and
            not compatibility_qualifier_present
        ):
            errors.append(
                "compatibility_qualifier_missing"
            )

        # -------------------------------------------------
        # Primary model protection
        # -------------------------------------------------
        locked = strategy_input.get(
            "locked",
            {},
        )

        if not isinstance(locked, dict):
            locked = {}

        models_block = locked.get(
            "models",
            {},
        )

        if not isinstance(models_block, dict):
            models_block = {}

        primary_model = TitleGenerator._clean(
            models_block.get(
                "primary",
                "",
            )
        )

        primary_model_present = (
            True
            if not primary_model
            else
            primary_model.casefold()
            in
            title_fold
        )

        if not primary_model_present:
            errors.append(
                "primary_model_missing"
            )

        all_models = (
            TitleGenerator
            ._all_verified_models(
                profile,
                strategy_input,
            )
        )

        compatibility_models = TitleGenerator._list(
            compatibility_facts.get(
                "models",
                [],
            )
        )

        compatibility_models += [
            value
            for value in TitleGenerator._list(
                compatibility_facts.get(
                    "important_compatibility",
                    [],
                )
            )
            if value not in compatibility_models
        ]

        model_only_compatibility_present = (
            True
            if (
                brands
                or
                not compatibility_models
            )
            else
            any(
                model.casefold()
                in
                title_fold
                for model
                in compatibility_models
            )
        )

        if not model_only_compatibility_present:
            errors.append(
                "compatibility_models_missing"
            )

        # If <61 while verified models remain unused, the title is a
        # composition failure, NOT an insufficiency.
        unused_models = [
            model
            for model in all_models
            if model.casefold()
            not in
            title_fold
        ]

        if (
            not min_length_ok
            and
            unused_models
        ):
            errors.append(
                "unused_models_before_min_length"
            )

        # -------------------------------------------------
        # Range-compression fact protection
        # -------------------------------------------------
        source_evidence = strategy_input.get(
            "source_evidence",
            {},
        )

        if not isinstance(source_evidence, dict):
            source_evidence = {}

        raw_title = TitleGenerator._clean(
            source_evidence.get(
                "raw_title",
                "",
            )
        )

        numeric_models = {
            model
            for model in all_models
            if re.fullmatch(
                r"\d{2,}",
                model,
            )
        }

        range_compression_detected = False

        for match in re.finditer(
            r"\b(\d{2,})\s*[-–—]\s*(\d{2,})\b",
            title,
        ):
            left = match.group(1)
            right = match.group(2)

            if (
                left in numeric_models
                and
                right in numeric_models
                and
                match.group(0).casefold()
                not in
                raw_title.casefold()
            ):
                range_compression_detected = True
                errors.append(
                    "forbidden_model_range_compression"
                )
                break

        # -------------------------------------------------
        # Seller brand
        # -------------------------------------------------
        seller_brand = TitleGenerator._clean(
            compatibility_facts.get(
                "seller_brand",
                "",
            )
        )

        seller_brand_leaked = bool(
            seller_brand
            and
            seller_brand.casefold()
            in
            title_fold
        )

        if seller_brand_leaked:
            errors.append(
                "seller_brand_leaked"
            )

        # -------------------------------------------------
        # Noise / blocked terms
        # -------------------------------------------------
        blocked_words = (
            TitleGenerator
            .check_blocked_words(
                title
            )
        )

        if blocked_words:
            errors.append(
                "blocked_marketing_word"
            )

        noise_patterns = [
            r"\b\d+\.(?:please|the|technical)\b",
            r"\bmainland china\b",
            r"\bmeasurement error\b",
            r"\bslightly different from the pictures\b",
        ]

        source_noise_detected = any(
            re.search(
                pattern,
                title,
                flags=re.IGNORECASE,
            )
            for pattern in noise_patterns
        )

        if source_noise_detected:
            errors.append(
                "source_noise_in_title"
            )

        # -------------------------------------------------
        # V7 Source Trace / Hallucination Gate
        # -------------------------------------------------

        rejected_identifier_hits = []

        for fact in rejected_facts:

            if not isinstance(
                fact,
                dict,
            ):
                continue

            fact_type = TitleGenerator._clean(
                fact.get(
                    "type",
                    "",
                )
            ).upper()

            if fact_type not in {
                "MODEL",
                "PART_NUMBER",
                "COMPATIBILITY_MODEL",
                "COMPATIBILITY_BRAND",
                "SPECIFICATION",
            }:
                continue

            fact_text = TitleGenerator._clean(
                fact.get(
                    "text",
                    "",
                )
            )

            if (
                fact_text
                and
                fact_text.casefold()
                in
                title_fold
            ):
                rejected_identifier_hits.append(
                    fact_text
                )

        if rejected_identifier_hits:
            errors.append(
                "unverified_fact_in_title"
            )

        # Primary model is only mandatory when it passed the source-trace gate.
        approved_model_texts = {
            TitleGenerator._clean(
                fact.get(
                    "text",
                    "",
                )
            ).casefold()
            for fact in approved_facts
            if (
                isinstance(
                    fact,
                    dict,
                )
                and
                TitleGenerator._clean(
                    fact.get(
                        "type",
                        "",
                    )
                ).upper()
                in {
                    "MODEL",
                    "PART_NUMBER",
                    "COMPATIBILITY_MODEL",
                }
            )
        }

        primary_model_source_verified = bool(
            primary_model
            and
            primary_model.casefold()
            in
            approved_model_texts
        )

        if (
            primary_model
            and
            not primary_model_source_verified
        ):
            # Remove the earlier "primary_model_missing" error because an
            # untraceable AI-inferred model must NOT be forced into the title.
            errors = [
                error
                for error in errors
                if error
                !=
                "primary_model_missing"
            ]

        # -------------------------------------------------
        # Deduplicate errors
        # -------------------------------------------------
        clean_errors = []

        for error in errors:
            if error not in clean_errors:
                clean_errors.append(
                    error
                )

        accepted_candidates = []
        rejected_candidates = []
        selected_models = []

        for index, candidate in enumerate(
            strategy.get(
                "title_candidates",
                [],
            )
        ):
            if not isinstance(candidate, dict):
                continue

            full_text = TitleGenerator._clean(
                candidate.get(
                    "text",
                    "",
                )
            )

            short_text = TitleGenerator._clean(
                candidate.get(
                    "short_text",
                    "",
                )
            )

            present_text = ""

            if (
                full_text
                and
                full_text.casefold()
                in
                title_fold
            ):
                present_text = full_text

            elif (
                short_text
                and
                short_text.casefold()
                in
                title_fold
            ):
                present_text = short_text

            record = {
                "index":
                    index,
                "text":
                    full_text,
                "short_text":
                    short_text,
                "type":
                    TitleGenerator._clean(
                        candidate.get(
                            "type",
                            "",
                        )
                    ).upper(),
                "priority":
                    TitleGenerator._clean(
                        candidate.get(
                            "priority",
                            "",
                        )
                    ).upper(),
            }

            if present_text:
                record[
                    "selected_text"
                ] = present_text

                record[
                    "selected_source"
                ] = (
                    "text"
                    if present_text == full_text
                    else
                    "short_text"
                )

                accepted_candidates.append(
                    record
                )

                if record["type"] in {
                    "MODEL",
                    "PART_NUMBER",
                    "COMPATIBILITY_MODEL",
                }:
                    selected_models.append(
                        present_text
                    )

            else:
                record[
                    "reason"
                ] = "not_selected_by_ai_composer"

                rejected_candidates.append(
                    record
                )

        removed_models = [
            model
            for model in all_models
            if model.casefold()
            not in
            title_fold
        ]

        composition_status = TitleGenerator._clean(
            strategy.get(
                "composition_status",
                "",
            )
        ).upper()

        final_status = (
            "resolved"
            if not clean_errors
            else
            (
                "insufficient_verified_facts"
                if (
                    clean_errors
                    ==
                    [
                        "title_below_61_characters"
                    ]
                    and
                    composition_status
                    ==
                    "INSUFFICIENT_VERIFIED_FACTS"
                )
                else
                "constraint_unresolved"
            )
        )

        return {
            "title":
                title,

            "selected_models":
                selected_models,

            "removed_models":
                removed_models,

            "character_count":
                len(title),

            "validation": {
                "length_ok":
                    (
                        min_length_ok
                        and
                        max_length_ok
                    ),
                "min_length_ok":
                    min_length_ok,
                "max_length_ok":
                    max_length_ok,
                "identity_present":
                    identity_present,
                "quantity_required":
                    bool(quantity),
                "quantity_present":
                    quantity_present,
                "compatibility_required":
                    compatibility_required,
                "compatibility_brand_present":
                    compatibility_brand_present,
                "compatibility_qualifier_present":
                    compatibility_qualifier_present,
                "compatibility_models_present":
                    model_only_compatibility_present,
                "primary_model":
                    primary_model,
                "primary_model_present":
                    primary_model_present,
                "primary_model_source_verified":
                    primary_model_source_verified,
                "unverified_fact_hits":
                    rejected_identifier_hits,
                "range_compression_detected":
                    range_compression_detected,
                "seller_brand_leaked":
                    seller_brand_leaked,
                "source_noise_detected":
                    source_noise_detected,
                "hard_constraints_ok":
                    len(
                        clean_errors
                    )
                    ==
                    0,
                "hard_constraint_errors":
                    clean_errors,
                "compliance_ok":
                    (
                        not blocked_words
                        and
                        not seller_brand_leaked
                        and
                        not source_noise_detected
                    ),
                "insufficient_verified_facts":
                    final_status
                    ==
                    "insufficient_verified_facts",
                "unused_verified_models":
                    unused_models,
            },

            "blocked_words":
                blocked_words,

            "brand_check":
                (
                    "passed"
                    if (
                        compatibility_brand_present
                        and
                        compatibility_qualifier_present
                    )
                    else
                    "failed"
                ),

            "generator_version":
                TitleGenerator.VERSION,

            "budget_parts":
                [
                    title
                ],

            "accepted_candidates":
                accepted_candidates,

            "rejected_candidates":
                rejected_candidates,

            "budget_used":
                len(title),

            "budget_remaining":
                max(
                    0,
                    75 - len(title),
                ),

            "solver": {
                "status":
                    final_status,
                "composition_status":
                    composition_status,
                "compatibility_mode":
                    TitleGenerator._clean(
                        strategy.get(
                            "compatibility_mode",
                            "NONE",
                        )
                    ).upper(),
                "used_facts":
                    strategy.get(
                        "used_facts",
                        [],
                    )
                    if isinstance(
                        strategy.get(
                            "used_facts",
                            [],
                        ),
                        list,
                    )
                    else
                    [],
                "unused_high_value_facts":
                    strategy.get(
                        "unused_high_value_facts",
                        [],
                    )
                    if isinstance(
                        strategy.get(
                            "unused_high_value_facts",
                            [],
                        ),
                        list,
                    )
                    else
                    [],
                "strategy_validation":
                    strategy.get(
                        "final_title_validation",
                        {},
                    )
                    if isinstance(
                        strategy.get(
                            "final_title_validation",
                            {},
                        ),
                        dict,
                    )
                    else
                    {},
            },
        }

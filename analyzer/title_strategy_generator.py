from __future__ import annotations

import json

from openai import OpenAI

from core.title_fact_gate import TitleFactGate

from services.ai_runtime import DEFAULT_TIMEOUT_SECONDS, execute_with_retry

from .title_strategy_prompt import (
    TITLE_STRATEGY_SYSTEM_PROMPT,
)


class TitleStrategyError(Exception):
    pass


class TitleStrategyGenerator:

    @staticmethod
    def generate(
        profile: dict,
        api_key: str,
        model="gpt-4.1-mini",
    ):
        """
        V6.0 Title Planner + Language-Aware Composer.

        Normal path:
        - one AI call decides strategy AND final_title
        - deterministic validation checks hard rules
        - only failed titles receive one targeted repair call

        This replaces the old architecture where Generator mechanically
        assembled/cropped candidate phrases after Strategy.
        """

        client = OpenAI(
            api_key=api_key,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            max_retries=0,
        )

        strategy_input = profile.get(
            "title_strategy_input",
            {},
        )

        if not isinstance(strategy_input, dict):
            raise TitleStrategyError(
                "title_strategy_input must be a dictionary"
            )

        if not strategy_input:
            raise TitleStrategyError(
                "title_strategy_input is missing"
            )

        locked = strategy_input.get(
            "locked",
            {},
        )

        if not isinstance(locked, dict):
            raise TitleStrategyError(
                "title_strategy_input.locked must be a dictionary"
            )

        locked_identity = locked.get(
            "identity",
            {},
        )

        if not isinstance(locked_identity, dict):
            raise TitleStrategyError(
                "title_strategy_input.locked.identity must be a dictionary"
            )

        locked_identity_text = str(
            locked_identity.get(
                "text",
                "",
            )
            or
            ""
        ).strip()

        if not locked_identity_text:
            raise TitleStrategyError(
                "title_strategy_input.locked.identity.text is missing"
            )

        fact_gate = TitleFactGate.build(
            profile
        )

        strategy_payload = dict(
            strategy_input
        )

        strategy_payload[
            "approved_title_fact_pool"
        ] = fact_gate

        strategy_input = dict(
            strategy_input
        )

        strategy_input[
            "approved_title_fact_pool"
        ] = fact_gate

        strategy_payload[
            "title_constraints"
        ] = {
            **(
                strategy_input.get(
                    "title_constraints",
                    {},
                )
                if isinstance(
                    strategy_input.get(
                        "title_constraints",
                        {},
                    ),
                    dict,
                )
                else
                {}
            ),
            "marketplace":
                "Amazon",
            "min_title_length":
                61,
            "max_title_length":
                75,
            "objective":
                (
                    "use approved_title_fact_pool for brands/models/specifications; "
                    "maximize verified search and purchase value while preserving mandatory facts"
                ),
        }

        def _request_once():
            return client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role":
                            "system",
                        "content":
                            TITLE_STRATEGY_SYSTEM_PROMPT,
                    },
                    {
                        "role":
                            "user",
                        "content":
                            json.dumps(
                                strategy_payload,
                                ensure_ascii=False,
                                indent=2,
                            ),
                    },
                ],
                response_format={
                    "type":
                        "json_object"
                },
            )

        response = execute_with_retry(
            _request_once,
            stage="title_strategy_v6",
        )

        try:
            result = json.loads(
                response.choices[0]
                .message
                .content
            )

            result = (
                TitleStrategyGenerator
                .normalize_strategy_result(
                    result
                )
            )

            validation = (
                TitleStrategyGenerator
                .validate_final_title(
                    final_title=result.get(
                        "final_title",
                        "",
                    ),
                    strategy_input=strategy_input,
                    composition_status=result.get(
                        "composition_status",
                        "",
                    ),
                )
            )

            result[
                "final_title_validation"
            ] = validation

            # Only failed titles receive a second AI call.
            if not validation.get(
                "hard_constraints_ok",
                False,
            ):
                repaired = (
                    TitleStrategyGenerator
                    .repair_final_title(
                        result=result,
                        strategy_input=strategy_input,
                        validation=validation,
                        client=client,
                        model=model,
                    )
                )

                if repaired:
                    result[
                        "final_title"
                    ] = repaired.get(
                        "final_title",
                        result.get(
                            "final_title",
                            "",
                        ),
                    )

                    if repaired.get(
                        "composition_status",
                        "",
                    ):
                        result[
                            "composition_status"
                        ] = repaired[
                            "composition_status"
                        ]

                    result[
                        "final_title_repair_reason"
                    ] = repaired.get(
                        "reason",
                        "",
                    )

                    result[
                        "final_title_validation"
                    ] = (
                        TitleStrategyGenerator
                        .validate_final_title(
                            final_title=result.get(
                                "final_title",
                                "",
                            ),
                            strategy_input=strategy_input,
                            composition_status=result.get(
                                "composition_status",
                                "",
                            ),
                        )
                    )

                    result[
                        "final_title_validation"
                    ][
                        "repair_attempted"
                    ] = True


                    # V7 fail-closed: failed repair cannot become a valid final title.
                    if not result[
                        "final_title_validation"
                    ].get(
                        "hard_constraints_ok",
                        False,
                    ):
                        result[
                            "composition_status"
                        ] = "VALIDATION_FAILED"

            return result

        except Exception as exc:
            raise TitleStrategyError(
                f"Title strategy parse failed: {exc}"
            )


    @staticmethod
    def _clean_text(value) -> str:
        if value is None:
            return ""

        return __import__("re").sub(
            r"\s+",
            " ",
            str(value),
        ).strip()


    @staticmethod
    def _list_text(value) -> list[str]:
        if not isinstance(
            value,
            list,
        ):
            return []

        result = []
        seen = set()

        for item in value:
            text = (
                TitleStrategyGenerator
                ._clean_text(
                    item
                )
            )

            if not text:
                continue

            key = text.casefold()

            if key in seen:
                continue

            seen.add(
                key
            )
            result.append(
                text
            )

        return result


    @staticmethod
    def _compact_quantity(value) -> str:
        text = (
            TitleStrategyGenerator
            ._clean_text(
                value
            )
        )

        if not text:
            return ""

        match = __import__("re").search(
            r"\b(\d{1,4})\s*(?:pcs?|pieces?|piece|sets?)\b",
            text,
            flags=__import__("re").IGNORECASE,
        )

        if not match:
            return ""

        try:
            count = int(
                match.group(1)
            )
        except Exception:
            return ""

        if count <= 1:
            return ""

        return f"{count}pcs"


    @staticmethod
    def _compatibility_qualifiers(
        target_language: str,
    ) -> list[str]:
        language = (
            TitleStrategyGenerator
            ._clean_text(
                target_language
            )
            .casefold()
        )

        mapping = {
            "english": [
                "compatible with",
            ],
            "spanish": [
                "compatible con",
            ],
            "español": [
                "compatible con",
            ],
            "french": [
                "compatible avec",
            ],
            "français": [
                "compatible avec",
            ],
            "german": [
                "kompatibel mit",
            ],
            "deutsch": [
                "kompatibel mit",
            ],
            "italian": [
                "compatibile con",
            ],
            "portuguese": [
                "compatível com",
                "compativel com",
            ],
            "dutch": [
                "compatibel met",
            ],
            "swedish": [
                "kompatibel med",
            ],
            "japanese": [
                "対応",
                "互換",
            ],
        }

        for key, values in mapping.items():
            if key in language:
                return values

        # Fallback accepts the common English phrase plus localized forms.
        return sorted(
            {
                value
                for values in mapping.values()
                for value in values
            }
        )


    @staticmethod
    def validate_final_title(
        final_title: str,
        strategy_input: dict,
        composition_status: str = "",
    ) -> dict:
        """
        Deterministic final-title audit.

        It validates facts and structure; it does NOT rewrite semantics.
        """

        title = (
            TitleStrategyGenerator
            ._clean_text(
                final_title
            )
        )

        title_fold = title.casefold()

        errors = []

        if not title:
            errors.append(
                "final_title_missing"
            )

        if len(title) > 75:
            errors.append(
                "title_above_75_characters"
            )

        if len(title) < 61:
            errors.append(
                "title_below_61_characters"
            )

        locked = (
            strategy_input.get(
                "locked",
                {},
            )
            if isinstance(
                strategy_input,
                dict,
            )
            else
            {}
        )

        if not isinstance(
            locked,
            dict,
        ):
            locked = {}

        identity = (
            TitleStrategyGenerator
            ._clean_text(
                (
                    locked.get(
                        "identity",
                        {},
                    )
                    if isinstance(
                        locked.get(
                            "identity",
                            {},
                        ),
                        dict,
                    )
                    else
                    {}
                ).get(
                    "text",
                    "",
                )
            )
        )

        if (
            identity
            and
            identity.casefold()
            not in
            title_fold
        ):
            # Do not attempt fuzzy semantic validation here.
            # AI may use a safe short identity, but it must be represented
            # through a candidate short_text.
            candidate_shorts = []

            for candidate in (
                strategy_input.get(
                    "candidate_facts",
                    {},
                ).get(
                    "source_title_segments",
                    [],
                )
                if isinstance(
                    strategy_input.get(
                        "candidate_facts",
                        {},
                    ),
                    dict,
                )
                else
                []
            ):
                _ = candidate

            errors.append(
                "identity_missing"
            )

        # Quantity rule.
        candidate_facts = (
            strategy_input.get(
                "candidate_facts",
                {},
            )
            if isinstance(
                strategy_input.get(
                    "candidate_facts",
                    {},
                ),
                dict,
            )
            else
            {}
        )

        quantity = (
            TitleStrategyGenerator
            ._compact_quantity(
                candidate_facts.get(
                    "important_quantity",
                    "",
                )
            )
        )

        if not quantity:
            source_evidence = (
                strategy_input.get(
                    "source_evidence",
                    {},
                )
                if isinstance(
                    strategy_input.get(
                        "source_evidence",
                        {},
                    ),
                    dict,
                )
                else
                {}
            )

            quantities = (
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

            if quantities:
                quantity = (
                    TitleStrategyGenerator
                    ._compact_quantity(
                        quantities[0]
                    )
                )

        if (
            quantity
            and
            not title_fold.startswith(
                quantity.casefold()
            )
        ):
            errors.append(
                "multi_unit_quantity_missing_or_not_prefixed"
            )

        # Compatibility facts.
        compatibility_facts = (
            strategy_input.get(
                "compatibility_facts",
                {},
            )
            if isinstance(
                strategy_input.get(
                    "compatibility_facts",
                    {},
                ),
                dict,
            )
            else
            {}
        )

        brands = (
            TitleStrategyGenerator
            ._list_text(
                compatibility_facts.get(
                    "brands",
                    [],
                )
            )
        )

        if not brands:
            brands = (
                TitleStrategyGenerator
                ._list_text(
                    compatibility_facts.get(
                        "third_party_brands",
                        [],
                    )
                )
            )

        compatibility_models = (
            TitleStrategyGenerator
            ._list_text(
                compatibility_facts.get(
                    "models",
                    [],
                )
            )
        )

        compatibility_models += [
            model
            for model in (
                TitleStrategyGenerator
                ._list_text(
                    compatibility_facts.get(
                        "important_compatibility",
                        [],
                    )
                )
            )
            if model not in compatibility_models
        ]

        seller_brand = (
            TitleStrategyGenerator
            ._clean_text(
                compatibility_facts.get(
                    "seller_brand",
                    "",
                )
            )
        )

        if (
            seller_brand
            and
            seller_brand.casefold()
            in
            title_fold
        ):
            errors.append(
                "seller_brand_leaked"
            )

        if brands:
            if not any(
                brand.casefold()
                in
                title_fold
                for brand in brands
            ):
                errors.append(
                    "compatibility_brand_missing"
                )

            qualifiers = (
                TitleStrategyGenerator
                ._compatibility_qualifiers(
                    strategy_input.get(
                        "target_language",
                        "English",
                    )
                )
            )

            if not any(
                qualifier.casefold()
                in
                title_fold
                for qualifier in qualifiers
            ):
                errors.append(
                    "compatibility_qualifier_missing"
                )

        # Locked primary model must not lose to low-value filler.
        models_block = (
            locked.get(
                "models",
                {},
            )
            if isinstance(
                locked.get(
                    "models",
                    {},
                ),
                dict,
            )
            else
            {}
        )

        primary_model = (
            TitleStrategyGenerator
            ._clean_text(
                models_block.get(
                    "primary",
                    "",
                )
            )
        )

        if (
            primary_model
            and
            primary_model.casefold()
            not in
            title_fold
        ):
            errors.append(
                "primary_model_missing"
            )

        # If there is no brand but there ARE verified compatibility models,
        # at least one of them must appear.
        if (
            not brands
            and
            compatibility_models
            and
            not any(
                model.casefold()
                in
                title_fold
                for model in compatibility_models
            )
        ):
            errors.append(
                "compatibility_models_missing"
            )

        # A <61 title is not allowed to declare insufficiency while verified
        # compatibility models remain unused.
        if len(title) < 61:
            unused_compatibility_models = [
                model
                for model in compatibility_models
                if model.casefold()
                not in
                title_fold
            ]

            if unused_compatibility_models:
                errors.append(
                    "unused_models_before_min_length"
                )

        # Discrete model range compression guard.
        raw_title = (
            TitleStrategyGenerator
            ._clean_text(
                (
                    strategy_input.get(
                        "source_evidence",
                        {},
                    )
                    if isinstance(
                        strategy_input.get(
                            "source_evidence",
                            {},
                        ),
                        dict,
                    )
                    else
                    {}
                ).get(
                    "raw_title",
                    "",
                )
            )
        )

        numeric_models = {
            model
            for model in compatibility_models
            if __import__("re").fullmatch(
                r"\d{2,}",
                model,
            )
        }

        for match in __import__("re").finditer(
            r"\b(\d{2,})\s*[-–—]\s*(\d{2,})\b",
            title,
        ):
            left = match.group(1)
            right = match.group(2)
            range_text = match.group(0)

            if (
                left in numeric_models
                and
                right in numeric_models
                and
                range_text.casefold()
                not in
                raw_title.casefold()
            ):
                errors.append(
                    "forbidden_model_range_compression"
                )
                break

        # Obvious source-noise fragments must never appear.
        noise_patterns = [
            r"\b\d+\.(?:please|the|technical)\b",
            r"\b\d+-\d+\s*cm\s+(?:error|difference)\b",
            r"\bmainland china\b",
        ]

        for pattern in noise_patterns:
            if __import__("re").search(
                pattern,
                title,
                flags=__import__("re").IGNORECASE,
            ):
                errors.append(
                    "source_noise_in_title"
                )
                break

        # De-duplicate error codes.
        cleaned_errors = []

        for error in errors:
            if error not in cleaned_errors:
                cleaned_errors.append(
                    error
                )

        composition_status = (
            TitleStrategyGenerator
            ._clean_text(
                composition_status
            )
            .upper()
        )

        return {
            "title":
                title,
            "character_count":
                len(title),
            "hard_constraints_ok":
                len(
                    cleaned_errors
                )
                ==
                0,
            "errors":
                cleaned_errors,
            "identity":
                identity,
            "primary_model":
                primary_model,
            "compatibility_brands":
                brands,
            "compatibility_models":
                compatibility_models,
            "composition_status":
                composition_status,
            "repair_attempted":
                False,
        }


    @staticmethod
    def repair_final_title(
        result: dict,
        strategy_input: dict,
        validation: dict,
        client,
        model: str,
    ) -> dict:
        """
        One targeted repair call for failed composed titles.

        The repair model receives exact error codes and the same verified facts.
        It must rewrite the whole title naturally rather than crop phrases.
        """

        repair_payload = {
            "current_title":
                result.get(
                    "final_title",
                    "",
                ),
            "errors":
                validation.get(
                    "errors",
                    [],
                ),
            "strategy_input":
                strategy_input,
            "selected_strategy": {
                "core_product":
                    result.get(
                        "core_product",
                        "",
                    ),
                "model_priority":
                    result.get(
                        "model_priority",
                        [],
                    ),
                "compatibility_priority":
                    result.get(
                        "compatibility_priority",
                        [],
                    ),
                "title_candidates":
                    result.get(
                        "title_candidates",
                        [],
                    ),
                "unused_high_value_facts":
                    result.get(
                        "unused_high_value_facts",
                        [],
                    ),
            },
        }

        repair_prompt = """
You are repairing ONE Amazon title that failed deterministic V6 validation.

Rewrite the WHOLE title naturally. Do not mechanically append or crop fragments.

HARD RULES:
- 61 to 75 characters whenever verified facts support it
- preserve the locked product identity
- quantity >1 uses compact prefix such as 5pcs; quantity 1 is omitted
- if compatible brand exists, include the brand with the correct local-language
  compatibility qualifier
- include the verified primary model when present
- when no brand exists but verified compatibility models exist, use the
  highest-value models naturally (English commonly uses "for")
- before using low-value material/color/features, use remaining verified
  models when they add search value
- never convert discrete models into ranges
- never invent a model, brand, dimension, material, feature, or compatibility
- never include seller brand
- never use source boilerplate/noise
- never create semantic fragments by cutting a phrase
- brands, models, part numbers and specifications must come from
  approved_title_fact_pool.approved_facts
- never use approved_title_fact_pool.rejected_facts
- follow target_language grammar and marketplace search habits
- count the literal final_title before returning; never claim READY outside 61-75

If verified facts truly cannot support 61 characters, return the best truthful
title and composition_status="INSUFFICIENT_VERIFIED_FACTS".

Return JSON only:
{
  "final_title": "",
  "composition_status": "READY|INSUFFICIENT_VERIFIED_FACTS|CORE_CONFLICT",
  "reason": ""
}
"""

        def _repair_once():
            return client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role":
                            "system",
                        "content":
                            repair_prompt,
                    },
                    {
                        "role":
                            "user",
                        "content":
                            json.dumps(
                                repair_payload,
                                ensure_ascii=False,
                                indent=2,
                            ),
                    },
                ],
                response_format={
                    "type":
                        "json_object"
                },
            )

        try:
            response = execute_with_retry(
                _repair_once,
                stage="title_strategy_v6_final_repair",
            )

            repaired = json.loads(
                response.choices[0]
                .message
                .content
            )

            if not isinstance(
                repaired,
                dict,
            ):
                return {}

            final_title = (
                TitleStrategyGenerator
                ._clean_text(
                    repaired.get(
                        "final_title",
                        "",
                    )
                )
            )

            if not final_title:
                return {}

            return {
                "final_title":
                    final_title,
                "composition_status":
                    (
                        TitleStrategyGenerator
                        ._clean_text(
                            repaired.get(
                                "composition_status",
                                "",
                            )
                        )
                    ),
                "reason":
                    (
                        TitleStrategyGenerator
                        ._clean_text(
                            repaired.get(
                                "reason",
                                "",
                            )
                        )
                    ),
            }

        except Exception:
            return {}


    @staticmethod
    def _candidate_shortest_text(
        candidate: dict,
    ) -> str:

        if not isinstance(
            candidate,
            dict,
        ):
            return ""

        full_text = str(
            candidate.get(
                "text",
                "",
            )
            or
            ""
        ).strip()

        short_text = str(
            candidate.get(
                "short_text",
                "",
            )
            or
            ""
        ).strip()

        if (
            short_text
            and
            len(short_text)
            <
            len(full_text)
        ):
            return short_text

        return full_text


    @staticmethod
    def _current_protected_candidates(
        result: dict,
    ) -> list:

        candidates = result.get(
            "title_candidates",
            [],
        )

        if not isinstance(
            candidates,
            list,
        ):
            return []

        protected = []

        for candidate in candidates:

            if not isinstance(
                candidate,
                dict,
            ):
                continue

            if not bool(
                candidate.get(
                    "required",
                    False,
                )
            ):
                continue

            candidate_type = str(
                candidate.get(
                    "type",
                    "",
                )
                or
                ""
            ).upper()

            # Single-unit quantity is not a protected title prefix.
            if candidate_type == "QUANTITY":

                quantity_text = str(
                    candidate.get(
                        "text",
                        "",
                    )
                    or
                    ""
                ).strip()

                match = __import__(
                    "re"
                ).match(
                    r"^\\s*(\\d+)\\b",
                    quantity_text,
                )

                if match:

                    try:
                        if int(
                            match.group(1)
                        ) <= 1:
                            continue
                    except Exception:
                        pass

            protected.append(
                candidate
            )

        return protected


    @staticmethod
    def _protected_bundle_length(
        result: dict,
    ) -> int:

        parts = [
            TitleStrategyGenerator
            ._candidate_shortest_text(
                candidate
            )
            for candidate
            in TitleStrategyGenerator
            ._current_protected_candidates(
                result
            )
        ]

        parts = [
            part
            for part in parts
            if part
        ]

        return len(
            " ".join(
                parts
            )
        )


    @staticmethod
    def _identity_short_is_structurally_safe(
        full_text: str,
        short_text: str,
    ) -> bool:
        """
        Conservative deterministic guard.

        The repair model may REMOVE words from the locked identity,
        but it may not invent a different product expression here.

        This is intentionally strict:
        - short must actually be shorter
        - at least 2 tokens for multi-word identities
        - every significant short token must already exist in full identity
        - final head token should be preserved
        """

        full_text = str(
            full_text
            or
            ""
        ).strip()

        short_text = str(
            short_text
            or
            ""
        ).strip()

        if not full_text or not short_text:
            return False

        if len(short_text) >= len(full_text):
            return False

        token_pattern = __import__(
            "re"
        ).compile(
            r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*"
        )

        full_tokens = [
            token.casefold()
            for token in token_pattern.findall(
                full_text
            )
        ]

        short_tokens = [
            token.casefold()
            for token in token_pattern.findall(
                short_text
            )
        ]

        if not short_tokens:
            return False

        if (
            len(full_tokens) > 1
            and
            len(short_tokens) < 2
        ):
            return False

        full_token_set = set(
            full_tokens
        )

        if any(
            token
            not in full_token_set
            for token in short_tokens
        ):
            return False

        # Preserve the final product-head token when possible.
        if (
            full_tokens
            and
            short_tokens
            and
            full_tokens[-1]
            !=
            short_tokens[-1]
        ):
            return False

        return True


    @staticmethod
    def _compatibility_short_is_structurally_safe(
        full_text: str,
        short_text: str,
    ) -> bool:
        """
        Compatibility repair may shorten a long compatible-brand list,
        but it may never invent a brand and must keep the explicit
        'Compatible with' qualifier.
        """

        full_text = str(
            full_text
            or
            ""
        ).strip()

        short_text = str(
            short_text
            or
            ""
        ).strip()

        if not full_text or not short_text:
            return False

        if len(short_text) >= len(full_text):
            return False

        required_prefix = "compatible with "

        if not short_text.casefold().startswith(
            required_prefix
        ):
            return False

        short_body = short_text[
            len(
                "Compatible with "
            ):
        ].strip()

        if not short_body:
            return False

        # Split common multi-brand separators and verify every retained
        # brand/model fragment came from the original compatibility phrase.
        fragments = [
            fragment.strip()
            for fragment in __import__(
                "re"
            ).split(
                r"[,;/|]+",
                short_body,
            )
            if fragment.strip()
        ]

        full_fold = full_text.casefold()

        if not fragments:
            return False

        return all(
            fragment.casefold()
            in full_fold
            for fragment in fragments
        )


    @staticmethod
    def _classify_core_overflow(
        result: dict,
    ) -> str:

        protected = (
            TitleStrategyGenerator
            ._current_protected_candidates(
                result
            )
        )

        if not protected:
            return "none"

        identity = next(
            (
                candidate
                for candidate in protected
                if str(
                    candidate.get(
                        "type",
                        "",
                    )
                    or
                    ""
                ).upper()
                ==
                "IDENTITY"
            ),
            None,
        )

        compatibility = next(
            (
                candidate
                for candidate in protected
                if str(
                    candidate.get(
                        "type",
                        "",
                    )
                    or
                    ""
                ).upper()
                ==
                "COMPATIBILITY"
            ),
            None,
        )

        models = [
            candidate
            for candidate in protected
            if str(
                candidate.get(
                    "type",
                    "",
                )
                or
                ""
            ).upper()
            in {
                "MODEL",
                "PART_NUMBER",
            }
        ]

        total = (
            TitleStrategyGenerator
            ._protected_bundle_length(
                result
            )
        )

        if total <= 75:
            return "none"

        identity_text = (
            TitleStrategyGenerator
            ._candidate_shortest_text(
                identity
            )
            if identity
            else
            ""
        )

        compatibility_text = (
            TitleStrategyGenerator
            ._candidate_shortest_text(
                compatibility
            )
            if compatibility
            else
            ""
        )

        identity_compatibility_length = len(
            " ".join(
                part
                for part in [
                    identity_text,
                    compatibility_text,
                ]
                if part
            )
        )

        if (
            compatibility_text
            and
            len(compatibility_text)
            >= 45
        ):
            return "compatibility_overflow"

        if (
            identity_text
            and
            len(identity_text)
            >= 30
        ):
            return "identity_overflow"

        if len(models) > 1:
            return "model_overflow"

        if identity_compatibility_length > 75:
            return "identity_compatibility_overflow"

        return "mixed_core_overflow"


    @staticmethod
    def repair_core_overflow_if_needed(
        result: dict,
        client,
        model: str,
    ) -> dict:
        """
        V3.4 Joint Core Budget + Two-Pass Targeted Repair.

        Root-cause fix for V3.3:
        V3.3 calculated an independent maximum for IDENTITY while keeping
        COMPATIBILITY full, and vice versa. In mixed long-core cases this
        could produce impossible limits (for example identity_max=1 or
        compatibility_max=2), even though shortening BOTH fields together
        could safely fit the protected core.

        V3.4 therefore budgets the flexible core jointly:
            IDENTITY + COMPATIBILITY <= shared flexible budget

        Quantity / model / part number remain immutable.
        """

        if not isinstance(result, dict):
            return result

        required_budget = result.get("required_budget", {})
        if not isinstance(required_budget, dict):
            required_budget = {}
            result["required_budget"] = required_budget

        def _protected():
            return (
                TitleStrategyGenerator
                ._current_protected_candidates(result)
            )

        def _bundle_length():
            return (
                TitleStrategyGenerator
                ._protected_bundle_length(result)
            )

        def _candidate_of_type(type_name: str):
            for candidate in result.get("title_candidates", []):
                if (
                    isinstance(candidate, dict)
                    and bool(candidate.get("required", False))
                    and str(candidate.get("type", "") or "").upper()
                    == type_name
                ):
                    return candidate
            return None

        def _fixed_candidates():
            return [
                candidate
                for candidate in _protected()
                if str(candidate.get("type", "") or "").upper()
                not in {"IDENTITY", "COMPATIBILITY"}
            ]

        def _effective(candidate):
            if not isinstance(candidate, dict):
                return ""
            return (
                TitleStrategyGenerator
                ._candidate_shortest_text(candidate)
            )

        def _shared_flexible_budget():
            """
            Maximum total characters available to:
                IDENTITY + COMPATIBILITY
            including the one space between them when both are present.
            """
            fixed_parts = [
                _effective(candidate)
                for candidate in _fixed_candidates()
                if _effective(candidate)
            ]

            fixed_length = len(" ".join(fixed_parts))

            identity_exists = bool(_effective(identity))
            compatibility_exists = bool(_effective(compatibility))
            flexible_count = int(identity_exists) + int(compatibility_exists)

            # Total separators in the final protected bundle:
            # N parts -> N-1 spaces.
            total_part_count = len(fixed_parts) + flexible_count
            total_spaces = max(0, total_part_count - 1)

            return max(
                0,
                75 - fixed_length - total_spaces,
            )

        def _flexible_length(
            identity_text: str,
            compatibility_text: str,
        ) -> int:
            return len(
                " ".join(
                    part
                    for part in [
                        str(identity_text or "").strip(),
                        str(compatibility_text or "").strip(),
                    ]
                    if part
                )
            )

        def _refresh_diagnostics():
            current = _bundle_length()

            required_budget["protected_bundle_length"] = current
            required_budget["resolved"] = current <= 75
            required_budget["overflow_type"] = (
                TitleStrategyGenerator
                ._classify_core_overflow(result)
            )
            required_budget["characters_over_budget"] = max(
                0,
                current - 75,
            )

            return current

        current_length = _refresh_diagnostics()

        required_budget.setdefault("repair_attempted", False)
        required_budget.setdefault("repair_applied", False)
        required_budget.setdefault("repair_type", "")
        required_budget.setdefault("repair_reason", "")
        required_budget["repair_passes"] = 0
        required_budget["repair_history"] = []

        if current_length <= 75:
            return result

        identity = _candidate_of_type("IDENTITY")
        compatibility = _candidate_of_type("COMPATIBILITY")

        original_identity_short = (
            str(identity.get("short_text", "") or "").strip()
            if identity
            else ""
        )
        original_compatibility_short = (
            str(compatibility.get("short_text", "") or "").strip()
            if compatibility
            else ""
        )

        any_safe_change = False
        applied_identity = False
        applied_compatibility = False
        last_reason = ""

        for repair_pass in (1, 2):

            before_length = _refresh_diagnostics()

            if before_length <= 75:
                break

            identity_current = _effective(identity)
            compatibility_current = _effective(compatibility)

            flexible_budget = _shared_flexible_budget()
            current_flexible_length = _flexible_length(
                identity_current,
                compatibility_current,
            )

            required_reduction = max(
                0,
                current_flexible_length - flexible_budget,
            )

            repair_payload = {
                "max_title_length": 75,
                "repair_pass": repair_pass,
                "overflow_type": required_budget.get(
                    "overflow_type",
                    "",
                ),
                "current_protected_bundle_length": before_length,
                "characters_that_must_be_removed": max(
                    0,
                    before_length - 75,
                ),
                "joint_flexible_core": {
                    "maximum_combined_characters":
                        flexible_budget,
                    "current_combined_characters":
                        current_flexible_length,
                    "minimum_characters_to_remove":
                        required_reduction,
                    "rule":
                        (
                            "identity_short_text plus compatibility_short_text "
                            "including their separating space must fit this "
                            "combined budget"
                        ),
                },
                "identity": {
                    "full_text": (
                        str(identity.get("text", "") or "").strip()
                        if identity else ""
                    ),
                    "current_effective_text": identity_current,
                },
                "compatibility": {
                    "full_text": (
                        str(compatibility.get("text", "") or "").strip()
                        if compatibility else ""
                    ),
                    "current_effective_text": compatibility_current,
                },
                "immutable_core": [
                    {
                        "type": str(candidate.get("type", "") or ""),
                        "text": _effective(candidate),
                    }
                    for candidate in _fixed_candidates()
                ],
            }

            repair_prompt = """
You repair ONLY an Amazon title protected-core overflow.

Do not write a complete title.
Do not reinterpret the product.
Do not invent facts.

V3.4 JOINT BUDGET RULE:
IDENTITY and COMPATIBILITY are the only flexible protected fields.

The payload gives:
joint_flexible_core.maximum_combined_characters

The combined output:
    identity_short_text + one separating space + compatibility_short_text
MUST be <= that exact combined character budget.

This is a JOINT limit. You may shorten one field, both fields, or keep one
unchanged, whichever preserves the most semantic/search value.

PRIORITY OF INFORMATION:
1. Keep the product physically identifiable.
2. Keep explicit "Compatible with" wording if compatibility exists.
3. Keep the highest-value compatible brand(s).
4. Quantity, model numbers and part numbers in immutable_core cannot change.

IDENTITY:
- Preserve the same physical product type.
- Remove redundant generic context, duplicate nouns, optional modifiers,
  or non-essential application wording.
- Do not collapse to a vague category.
- Prefer words already present in the original identity.
- For kit/assembly/set products, preserve the fact that it is a kit,
  assembly or set when that changes what is sold.

COMPATIBILITY:
- Keep the exact prefix "Compatible with".
- A long multi-brand list may be reduced to the highest-value one or two
  brands when necessary.
- Every retained brand must already occur in the original compatibility.
- Never invent a brand.

Return JSON only:
{
  "safe": true,
  "identity_short_text": "",
  "compatibility_short_text": "",
  "repair_type": "identity|compatibility|identity_and_compatibility|none",
  "reason": ""
}

If the joint numeric budget cannot be met safely, return safe=false.
"""

            def _repair_once():
                return client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": repair_prompt,
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                repair_payload,
                                ensure_ascii=False,
                                indent=2,
                            ),
                        },
                    ],
                    response_format={"type": "json_object"},
                )

            required_budget["repair_attempted"] = True
            required_budget["repair_passes"] = repair_pass

            try:
                repair_response = execute_with_retry(
                    _repair_once,
                    stage=(
                        "title_strategy_core_joint_repair_"
                        f"pass_{repair_pass}"
                    ),
                )

                repair_result = json.loads(
                    repair_response.choices[0].message.content
                )

            except Exception as exc:
                last_reason = (
                    f"repair_pass_{repair_pass}_call_failed: {exc}"
                )
                required_budget["repair_history"].append(
                    {
                        "pass": repair_pass,
                        "before_length": before_length,
                        "applied": False,
                        "reason": last_reason,
                    }
                )
                continue

            if not isinstance(repair_result, dict):
                last_reason = (
                    f"repair_pass_{repair_pass}_result_not_dict"
                )
                required_budget["repair_history"].append(
                    {
                        "pass": repair_pass,
                        "before_length": before_length,
                        "applied": False,
                        "reason": last_reason,
                    }
                )
                continue

            if not bool(repair_result.get("safe", False)):
                last_reason = str(
                    repair_result.get(
                        "reason",
                        f"repair_pass_{repair_pass}_no_safe_repair",
                    )
                    or
                    f"repair_pass_{repair_pass}_no_safe_repair"
                )
                required_budget["repair_history"].append(
                    {
                        "pass": repair_pass,
                        "before_length": before_length,
                        "joint_budget": flexible_budget,
                        "applied": False,
                        "reason": last_reason,
                    }
                )
                continue

            proposed_identity = str(
                repair_result.get(
                    "identity_short_text",
                    "",
                )
                or
                ""
            ).strip()

            proposed_compatibility = str(
                repair_result.get(
                    "compatibility_short_text",
                    "",
                )
                or
                ""
            ).strip()

            # Blank means "keep the current effective text".
            candidate_identity = (
                proposed_identity
                or identity_current
            )
            candidate_compatibility = (
                proposed_compatibility
                or compatibility_current
            )

            identity_safe = True
            compatibility_safe = True

            if (
                identity
                and
                proposed_identity
                and
                proposed_identity != identity_current
            ):
                identity_safe = (
                    TitleStrategyGenerator
                    ._identity_short_is_structurally_safe(
                        str(identity.get("text", "") or ""),
                        proposed_identity,
                    )
                )

            if (
                compatibility
                and
                proposed_compatibility
                and
                proposed_compatibility != compatibility_current
            ):
                compatibility_safe = (
                    TitleStrategyGenerator
                    ._compatibility_short_is_structurally_safe(
                        str(compatibility.get("text", "") or ""),
                        proposed_compatibility,
                    )
                )

            joint_length = _flexible_length(
                candidate_identity,
                candidate_compatibility,
            )

            joint_budget_ok = (
                joint_length <= flexible_budget
            )

            pass_identity_applied = False
            pass_compatibility_applied = False

            # Apply only when the WHOLE proposed flexible core is safe
            # and satisfies the shared character budget.
            if (
                identity_safe
                and compatibility_safe
                and joint_budget_ok
            ):
                if (
                    identity
                    and
                    proposed_identity
                    and
                    proposed_identity != identity_current
                ):
                    identity["short_text"] = proposed_identity
                    pass_identity_applied = True
                    applied_identity = True
                    any_safe_change = True

                if (
                    compatibility
                    and
                    proposed_compatibility
                    and
                    proposed_compatibility != compatibility_current
                ):
                    compatibility["short_text"] = proposed_compatibility
                    pass_compatibility_applied = True
                    applied_compatibility = True
                    any_safe_change = True

            after_length = _refresh_diagnostics()

            if not joint_budget_ok:
                last_reason = (
                    "proposal_exceeded_joint_flexible_budget"
                )
            elif not identity_safe:
                last_reason = (
                    "proposal_failed_identity_safety_guard"
                )
            elif not compatibility_safe:
                last_reason = (
                    "proposal_failed_compatibility_safety_guard"
                )
            else:
                last_reason = str(
                    repair_result.get("reason", "")
                    or
                    "joint_core_repair_applied"
                )

            required_budget["repair_history"].append(
                {
                    "pass": repair_pass,
                    "before_length": before_length,
                    "after_length": after_length,
                    "characters_removed": max(
                        0,
                        before_length - after_length,
                    ),
                    "joint_flexible_budget":
                        flexible_budget,
                    "proposed_joint_length":
                        joint_length,
                    "identity_safe":
                        identity_safe,
                    "compatibility_safe":
                        compatibility_safe,
                    "joint_budget_ok":
                        joint_budget_ok,
                    "identity_applied":
                        pass_identity_applied,
                    "compatibility_applied":
                        pass_compatibility_applied,
                    "applied": (
                        pass_identity_applied
                        or
                        pass_compatibility_applied
                    ),
                    "reason": last_reason,
                }
            )

            if after_length <= 75:
                break

        final_length = _refresh_diagnostics()

        if final_length <= 75:
            required_budget["repair_applied"] = any_safe_change

            if applied_identity and applied_compatibility:
                required_budget["repair_type"] = (
                    "identity_and_compatibility"
                )
            elif applied_identity:
                required_budget["repair_type"] = "identity"
            elif applied_compatibility:
                required_budget["repair_type"] = "compatibility"
            else:
                required_budget["repair_type"] = ""

            required_budget["repair_reason"] = (
                last_reason
                or
                "protected_core_resolved"
            )

            return result

        # Failed joint repair must never leave partial semantic edits behind.
        if identity:
            identity["short_text"] = original_identity_short

        if compatibility:
            compatibility["short_text"] = original_compatibility_short

        _refresh_diagnostics()

        required_budget["repair_applied"] = False
        required_budget["repair_type"] = ""
        required_budget["repair_reason"] = (
            last_reason
            or
            "two_pass_joint_repair_did_not_resolve_75_char_budget"
        )

        return result


    @staticmethod
    def normalize_strategy_result(
        result: dict,
    ) -> dict:
        """
        对 Title Strategy AI 输出做结构标准化。

        注意：

        这里只做 Schema 保护。

        不重新判断：
        - 什么信息重要
        - 什么是型号
        - 什么是规格
        - 什么应该进入标题

        这些判断必须由 AI Strategy 完成。
        """

        if not isinstance(
            result,
            dict,
        ):
            raise TitleStrategyError(
                "Title strategy result must be a dictionary"
            )

        # =============================================
        # Legacy Fields
        # =============================================

        legacy_list_fields = [
            "must_include",
            "optional_include",
            "exclude",
            "model_priority",
            "compatibility_priority",
            "title_structure",
            "priority_order",
        ]

        for field in legacy_list_fields:
            value = result.get(
                field,
                []
            )

            if not isinstance(
                value,
                list,
            ):
                value = []

            cleaned = []

            for item in value:
                text = str(
                    item
                ).strip()

                if not text:
                    continue

                if text not in cleaned:
                    cleaned.append(
                        text
                    )

            result[
                field
            ] = cleaned

        legacy_text_fields = [
            "core_product",
            "buyer_search_intent",
            "title_length_strategy",
            "reasoning",
        ]

        for field in legacy_text_fields:
            value = result.get(
                field,
                ""
            )

            if value is None:
                value = ""

            result[
                field
            ] = str(
                value
            ).strip()

        # =============================================
        # Title Candidates
        # =============================================

        candidates = result.get(
            "title_candidates",
            []
        )

        if not isinstance(
            candidates,
            list,
        ):
            candidates = []

        allowed_types = {
            "IDENTITY",
            "SECONDARY_IDENTITY",
            "MODEL",
            "PART_NUMBER",
            "COMPATIBILITY_MODEL",
            "COMPATIBILITY",
            "FEATURE",
            "SPECIFICATION",
            "QUANTITY",
            "MATERIAL",
            "USAGE",
            "SEARCH_TERM",
            "OTHER",
        }

        allowed_priorities = {
            "S",
            "A",
            "B",
            "C",
            "D",
        }

        normalized_candidates = []
        seen = set()

        for candidate in candidates:

            if not isinstance(
                candidate,
                dict,
            ):
                continue

            text = str(
                candidate.get(
                    "text",
                    ""
                )
            ).strip()

            if not text:
                continue

            short_text = str(
                candidate.get(
                    "short_text",
                    ""
                )
                or
                ""
            ).strip()

            candidate_type = str(
                candidate.get(
                    "type",
                    "OTHER"
                )
            ).strip().upper()

            if (
                candidate_type
                not in allowed_types
            ):
                candidate_type = "OTHER"

            priority = str(
                candidate.get(
                    "priority",
                    "C"
                )
            ).strip().upper()

            if (
                priority
                not in allowed_priorities
            ):
                priority = "C"

            raw_scores = candidate.get(
                "scores",
                {}
            )

            if not isinstance(
                raw_scores,
                dict,
            ):
                raw_scores = {}

            def normalize_score(
                value,
            ) -> int:
                """
                Score Schema保护。

                这里只负责：
                - 转数字
                - 限制0~100

                不重新判断产品价值。
                """

                try:
                    score_value = float(
                        value
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    score_value = 0.0

                score_value = max(
                    0.0,
                    min(
                        100.0,
                        score_value,
                    ),
                )

                return int(
                    round(
                        score_value
                    )
                )

            scores = {
                "search_value":
                    normalize_score(
                        raw_scores.get(
                            "search_value",
                            0,
                        )
                    ),

                "purchase_impact":
                    normalize_score(
                        raw_scores.get(
                            "purchase_impact",
                            0,
                        )
                    ),

                "identity_value":
                    normalize_score(
                        raw_scores.get(
                            "identity_value",
                            0,
                        )
                    ),

                "differentiation_value":
                    normalize_score(
                        raw_scores.get(
                            "differentiation_value",
                            0,
                        )
                    ),

                "character_efficiency":
                    normalize_score(
                        raw_scores.get(
                            "character_efficiency",
                            0,
                        )
                    ),
            }

            final_score = round(
                (
                    scores[
                        "search_value"
                    ]
                    * 0.30
                )
                +
                (
                    scores[
                        "purchase_impact"
                    ]
                    * 0.25
                )
                +
                (
                    scores[
                        "identity_value"
                    ]
                    * 0.20
                )
                +
                (
                    scores[
                        "differentiation_value"
                    ]
                    * 0.15
                )
                +
                (
                    scores[
                        "character_efficiency"
                    ]
                    * 0.10
                ),
                1,
            )

            raw_incremental = candidate.get(
                "incremental_value",
                None,
            )

            has_incremental = isinstance(
                raw_incremental,
                dict,
            )

            if not has_incremental:
                raw_incremental = {}

            incremental_value = {
                "new_information":
                    normalize_score(
                        raw_incremental.get(
                            "new_information",
                            0,
                        )
                    ),

                "redundancy_penalty":
                    normalize_score(
                        raw_incremental.get(
                            "redundancy_penalty",
                            0,
                        )
                    ),

                "selection_value":
                    normalize_score(
                        raw_incremental.get(
                            "selection_value",
                            0,
                        )
                    ),
            }

            if has_incremental:

                incremental_modifier = (
                    incremental_value[
                        "new_information"
                    ]
                    * 0.50
                    +
                    incremental_value[
                        "selection_value"
                    ]
                    * 0.30
                    +
                    (
                        100
                        -
                        incremental_value[
                            "redundancy_penalty"
                        ]
                    )
                    * 0.20
                )

                adjusted_score = round(
                    final_score
                    *
                    (
                        0.50
                        +
                        (
                            incremental_modifier
                            / 200.0
                        )
                    ),
                    1,
                )

            else:
                incremental_modifier = 100.0
                adjusted_score = final_score

            required = candidate.get(
                "required",
                False
            )

            if not isinstance(
                required,
                bool,
            ):
                required = False

            reason = str(
                candidate.get(
                    "reason",
                    ""
                )
                or
                ""
            ).strip()

            duplicate_key = (
                text.casefold()
            )

            if duplicate_key in seen:
                continue

            seen.add(
                duplicate_key
            )

            normalized_candidates.append(
                {
                    "text":
                        text,

                    "short_text":
                        short_text,

                    "type":
                        candidate_type,

                    "priority":
                        priority,

                    "scores":
                        scores,

                    "final_score":
                        final_score,

                    "incremental_value":
                        incremental_value,

                    "incremental_modifier":
                        round(
                            incremental_modifier,
                            1,
                        ),

                    "adjusted_score":
                        adjusted_score,

                    "required":
                        required,

                    "reason":
                        reason,
                }
            )
        # =============================================
        # V3.0 Required Budget Arbitration
        #
        # Root cause:
        # The AI may mark too many FEATURE / SPECIFICATION / MATERIAL /
        # COLOR candidates as required. If every "required" flag is treated
        # as untouchable, the protected bundle itself can exceed 75 chars.
        #
        # Rule:
        # 1. Never demote IDENTITY.
        # 2. Never demote COMPATIBILITY.
        # 3. Never demote multi-unit QUANTITY.
        # 4. Protect up to the two strongest required MODEL/PART_NUMBER
        #    candidates.
        # 5. Other AI-required candidates remain required only while the
        #    shortest safe protected bundle fits within 75 characters.
        # 6. When the bundle is too long, demote the lowest marginal-value
        #    non-core required candidate to optional and repeat.
        #
        # This is NOT semantic rewriting. It only resolves conflicts between
        # AI "required" flags under the fixed 75-character title budget.
        # =============================================

        def candidate_shortest_text(
            candidate,
        ):

            full_text = str(
                candidate.get(
                    "text",
                    ""
                )
                or
                ""
            ).strip()

            short_text = str(
                candidate.get(
                    "short_text",
                    ""
                )
                or
                ""
            ).strip()

            if (
                short_text
                and
                len(short_text)
                <
                len(full_text)
            ):
                return short_text

            return full_text


        def bundle_length(
            bundle,
        ):

            parts = [
                candidate_shortest_text(
                    candidate
                )
                for candidate in bundle
            ]

            parts = [
                part
                for part in parts
                if part
            ]

            return len(
                " ".join(
                    parts
                )
            )


        def parse_quantity_count(
            candidate,
        ):
            """
            Extract a leading package count from a QUANTITY candidate.

            Examples:
            "10 PCS" -> 10
            "5 pcs" -> 5
            "1 Piece" -> 1

            Returns None when no trustworthy leading integer exists.
            """

            if not isinstance(
                candidate,
                dict,
            ):
                return None

            if str(
                candidate.get(
                    "type",
                    "",
                )
                or
                ""
            ).upper() != "QUANTITY":
                return None

            quantity_text = str(
                candidate.get(
                    "text",
                    "",
                )
                or
                ""
            ).strip()

            match = __import__(
                "re"
            ).match(
                r"^\\s*(\\d+)\\b",
                quantity_text,
            )

            if not match:
                return None

            try:
                return int(
                    match.group(1)
                )
            except Exception:
                return None


        # Preserve the AI decision for diagnostics.
        for candidate in normalized_candidates:

            candidate[
                "required_by_ai"
            ] = bool(
                candidate.get(
                    "required",
                    False
                )
            )

            candidate[
                "required_budget_demoted"
            ] = False


        # Single-unit quantity does not belong in the title prefix and
        # therefore must not consume protected-core budget.
        for candidate in normalized_candidates:

            if str(
                candidate.get(
                    "type",
                    "",
                )
                or
                ""
            ).upper() != "QUANTITY":
                continue

            quantity_count = parse_quantity_count(
                candidate
            )

            if quantity_count == 1:

                candidate[
                    "required"
                ] = False

                candidate[
                    "required_budget_demoted"
                ] = True

                candidate[
                    "required_budget_reason"
                ] = (
                    "single_unit_quantity_not_title_prefix"
                )


        fixed_required = []
        model_required = []
        flexible_required = []

        for candidate in normalized_candidates:

            if not candidate.get(
                "required",
                False
            ):
                continue

            candidate_type = str(
                candidate.get(
                    "type",
                    ""
                )
                or
                ""
            ).upper()

            if candidate_type in {
                "IDENTITY",
                "COMPATIBILITY",
                "QUANTITY",
            }:
                fixed_required.append(
                    candidate
                )
                continue

            if candidate_type in {
                "MODEL",
                "PART_NUMBER",
            }:
                model_required.append(
                    candidate
                )
                continue

            flexible_required.append(
                candidate
            )


        def required_value_key(
            candidate,
        ):

            try:
                adjusted_score = float(
                    candidate.get(
                        "adjusted_score",
                        0
                    )
                    or
                    0
                )
            except (
                TypeError,
                ValueError,
            ):
                adjusted_score = 0.0

            incremental = candidate.get(
                "incremental_value",
                {}
            )

            if not isinstance(
                incremental,
                dict,
            ):
                incremental = {}

            try:
                selection_value = float(
                    incremental.get(
                        "selection_value",
                        0
                    )
                    or
                    0
                )
            except (
                TypeError,
                ValueError,
            ):
                selection_value = 0.0

            try:
                new_information = float(
                    incremental.get(
                        "new_information",
                        0
                    )
                    or
                    0
                )
            except (
                TypeError,
                ValueError,
            ):
                new_information = 0.0

            length_cost = max(
                1,
                len(
                    candidate_shortest_text(
                        candidate
                    )
                )
            )

            # Higher = more valuable per character.
            efficiency = (
                adjusted_score
                * 0.50
                +
                selection_value
                * 0.30
                +
                new_information
                * 0.20
            ) / length_cost

            return (
                efficiency,
                adjusted_score,
                selection_value,
                new_information,
            )


        # Protect at most the two strongest AI-required models/parts.
        model_required.sort(
            key=required_value_key,
            reverse=True,
        )

        protected_model_required = (
            model_required[:2]
        )

        for candidate in model_required[2:]:

            candidate[
                "required"
            ] = False

            candidate[
                "required_budget_demoted"
            ] = True

            candidate[
                "required_budget_reason"
            ] = (
                "more_than_two_primary_model_or_part_candidates"
            )


        active_flexible_required = list(
            flexible_required
        )

        protected_bundle = (
            fixed_required
            +
            protected_model_required
            +
            active_flexible_required
        )


        # If the protected bundle is still too long, a second required
        # MODEL/PART_NUMBER must not displace identity + compatibility +
        # the strongest primary identifier.
        #
        # This implements the established title rule:
        # use one or two high-value models; the second model is conditional
        # on remaining title space.
        while (
            bundle_length(
                protected_bundle
            )
            >
            75
            and
            len(
                protected_model_required
            )
            >
            1
        ):

            candidate_to_demote = (
                protected_model_required.pop()
            )

            candidate_to_demote[
                "required"
            ] = False

            candidate_to_demote[
                "required_budget_demoted"
            ] = True

            candidate_to_demote[
                "required_budget_reason"
            ] = (
                "secondary_required_model_exceeds_core_budget"
            )

            protected_bundle = (
                fixed_required
                +
                protected_model_required
                +
                active_flexible_required
            )


        # Demote lowest marginal-value flexible required candidates until the
        # shortest safe bundle fits. IDENTITY / COMPATIBILITY / QUANTITY and
        # the strongest 1-2 model/part candidates remain protected.
        while (
            bundle_length(
                protected_bundle
            )
            >
            75
            and
            active_flexible_required
        ):

            candidate_to_demote = min(
                active_flexible_required,
                key=required_value_key,
            )

            candidate_to_demote[
                "required"
            ] = False

            candidate_to_demote[
                "required_budget_demoted"
            ] = True

            candidate_to_demote[
                "required_budget_reason"
            ] = (
                "protected_required_bundle_exceeds_75"
            )

            active_flexible_required.remove(
                candidate_to_demote
            )

            protected_bundle = (
                fixed_required
                +
                protected_model_required
                +
                active_flexible_required
            )


        required_budget_summary = {
            "protected_bundle_length":
                bundle_length(
                    protected_bundle
                ),
            "ai_required_count":
                sum(
                    1
                    for candidate
                    in normalized_candidates
                    if candidate.get(
                        "required_by_ai",
                        False
                    )
                ),
            "final_required_count":
                sum(
                    1
                    for candidate
                    in normalized_candidates
                    if candidate.get(
                        "required",
                        False
                    )
                ),
            "demoted_count":
                sum(
                    1
                    for candidate
                    in normalized_candidates
                    if candidate.get(
                        "required_budget_demoted",
                        False
                    )
                ),
            "resolved":
                bundle_length(
                    protected_bundle
                )
                <=
                75,

            "repair_attempted":
                False,

            "repair_applied":
                False,

            "repair_type":
                "",

            "repair_reason":
                "",
        }


        # =============================================
        # Candidate Final Ordering
        #
        # Title Strategy AI 负责：
        # - 语义判断
        # - priority
        # - required
        # - incremental value
        #
        # Normalizer 负责：
        # - final_score
        # - adjusted_score
        # - 将这些确定性结果转化为最终候选顺序
        #
        # TitleGenerator 不再重新排序。
        # =============================================

        priority_rank = {
            "S": 0,
            "A": 1,
            "B": 2,
            "C": 3,
            "D": 4,
        }


        def candidate_sort_key(
            item,
        ):

            candidate_type = str(
                item.get(
                    "type",
                    "OTHER",
                )
                or
                "OTHER"
            ).upper()


            required = item.get(
                "required",
                False,
            )


            if not isinstance(
                required,
                bool,
            ):
                required = False


            try:

                adjusted_score = float(
                    item.get(
                        "adjusted_score",
                        0,
                    )
                    or
                    0
                )

            except (
                TypeError,
                ValueError,
            ):

                adjusted_score = 0.0


            priority = str(
                item.get(
                    "priority",
                    "D",
                )
                or
                "D"
            ).upper()


            # =========================================
            # Group 0
            #
            # Primary required identity
            #
            # 必须永远位于标题最前面。
            # =========================================

            if (
                candidate_type
                ==
                "IDENTITY"
                and
                required
            ):

                group = 0


            # =========================================
            # Group 1
            #
            # Compatibility brand is protected ahead
            # of optional secondary identity/context.
            # =========================================

            elif candidate_type == "COMPATIBILITY":

                group = 1


            # =========================================
            # Group 2
            #
            # AI-selected primary model / part number.
            # Strategy marks only the strongest 1-2 as
            # required when their selection value is high.
            # =========================================

            elif (
                candidate_type
                in {
                    "MODEL",
                    "PART_NUMBER",
                }
                and
                required
            ):

                group = 2


            # =========================================
            # Group 3
            #
            # Other high-value supporting information.
            #
            # SECONDARY_IDENTITY is deliberately NOT
            # protected. It competes here by adjusted
            # incremental value and character efficiency.
            # This prevents a long/redundant secondary
            # identity from displacing compatibility or
            # primary models.
            # =========================================

            elif candidate_type not in {
                "MODEL",
                "PART_NUMBER",
                "SEARCH_TERM",
            }:

                group = 3


            # =========================================
            # Group 4
            #
            # Completion models / part numbers.
            # Used after stronger supporting facts.
            # =========================================

            elif candidate_type in {
                "MODEL",
                "PART_NUMBER",
            }:

                group = 4


            # =========================================
            # Group 5
            #
            # Remaining search/context completion.
            # =========================================

            else:

                group = 5


            return (
                group,

                # adjusted_score 是组内核心排序依据
                -adjusted_score,

                # priority 只作为辅助排序
                priority_rank.get(
                    priority,
                    99,
                ),
            )


        normalized_candidates.sort(
            key=candidate_sort_key
        )
        result[
            "title_candidates"
        ] = normalized_candidates

        # =============================================
        # Schema Version
        # =============================================

        result[
            "required_budget"
        ] = required_budget_summary

        result[
            "schema_version"
        ] = "6.0-final-title-planner-composer"

        result[
            "final_title"
        ] = str(
            result.get(
                "final_title",
                "",
            )
            or
            ""
        ).strip()

        result[
            "composition_status"
        ] = str(
            result.get(
                "composition_status",
                "",
            )
            or
            ""
        ).strip().upper()

        allowed_composition_status = {
            "READY",
            "INSUFFICIENT_VERIFIED_FACTS",
            "CORE_CONFLICT",
            "VALIDATION_FAILED",
        }

        if (
            result[
                "composition_status"
            ]
            not in
            allowed_composition_status
        ):
            result[
                "composition_status"
            ] = "READY"

        result[
            "compatibility_mode"
        ] = str(
            result.get(
                "compatibility_mode",
                "NONE",
            )
            or
            "NONE"
        ).strip().upper()

        for list_field in (
            "used_facts",
            "unused_high_value_facts",
        ):
            value = result.get(
                list_field,
                [],
            )

            if not isinstance(
                value,
                list,
            ):
                value = []

            cleaned = []

            for item in value:
                item_text = str(
                    item
                    or
                    ""
                ).strip()

                if (
                    item_text
                    and
                    item_text not in cleaned
                ):
                    cleaned.append(
                        item_text
                    )

            result[
                list_field
            ] = cleaned

        return result

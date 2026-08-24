from __future__ import annotations


class StrategyInputBuilderError(Exception):
    pass


class StrategyInputBuilder:
    """
    Title Strategy Input Builder V1.0

    职责：
    - 为 Title Strategy 构造唯一、明确、无歧义的输入
    - 优先使用 normalized_knowledge
    - 保留已确认的事实、特征、规格和搜索意图
    - 不重新理解产品
    - 不重新决定 Identity
    - 不生成标题
    - 不做 Candidate 排序
    """

    SCHEMA_VERSION = "3.0-final-title-composer-input"

    @staticmethod
    def _dict(
        value,
    ) -> dict:

        if isinstance(
            value,
            dict,
        ):
            return value

        return {}

    @staticmethod
    def _list(
        value,
    ) -> list:

        if isinstance(
            value,
            list,
        ):
            return value

        return []

    @staticmethod
    def _text(
        value,
    ) -> str:

        if value is None:
            return ""

        return " ".join(
            str(value)
            .strip()
            .split()
        )

    @staticmethod
    def build(
        profile: dict,
    ) -> dict:

        if not isinstance(
            profile,
            dict,
        ):

            raise StrategyInputBuilderError(
                "Profile must be a dictionary"
            )

        normalized = (
            StrategyInputBuilder._dict(
                profile.get(
                    "normalized_knowledge",
                    {},
                )
            )
        )

        product_knowledge = (
            StrategyInputBuilder._dict(
                profile.get(
                    "product_knowledge",
                    {},
                )
            )
        )

        # =============================================
        # Normalized / locked inputs
        # =============================================

        normalized_identity = (
            StrategyInputBuilder._dict(
                normalized.get(
                    "identity",
                    {},
                )
            )
        )

        normalized_compatibility = (
            StrategyInputBuilder._dict(
                normalized.get(
                    "compatibility",
                    {},
                )
            )
        )

        normalized_models = (
            StrategyInputBuilder._dict(
                normalized.get(
                    "models",
                    {},
                )
            )
        )

        profile_compatibility = (
            StrategyInputBuilder._dict(
                profile.get(
                    "compatibility",
                    {},
                )
            )
        )

        profile_title_information = (
            StrategyInputBuilder._dict(
                profile.get(
                    "title_information",
                    {},
                )
            )
        )

        profile_brand_info = (
            StrategyInputBuilder._dict(
                profile.get(
                    "brand_info",
                    {},
                )
            )
        )

        # =============================================
        # Product Knowledge supporting information
        # =============================================

        title_information = (
            StrategyInputBuilder._dict(
                product_knowledge.get(
                    "title_information",
                    {},
                )
            )
        )

        feature_classification = (
            StrategyInputBuilder._dict(
                product_knowledge.get(
                    "feature_classification",
                    {},
                )
            )
        )

        facts = (
            StrategyInputBuilder._dict(
                product_knowledge.get(
                    "facts",
                    {},
                )
            )
        )

        purpose = (
            StrategyInputBuilder._dict(
                product_knowledge.get(
                    "purpose",
                    {},
                )
            )
        )

        seo = (
            StrategyInputBuilder._dict(
                product_knowledge.get(
                    "seo",
                    {},
                )
            )
        )

        compliance = (
            StrategyInputBuilder._dict(
                product_knowledge.get(
                    "compliance",
                    {},
                )
            )
        )

        source_fact_ledger = (
            StrategyInputBuilder._dict(
                profile.get(
                    "source_fact_ledger",
                    {},
                )
            )
        )

        source_fact_audit = (
            StrategyInputBuilder._dict(
                profile.get(
                    "source_fact_audit",
                    {},
                )
            )
        )

        source_snapshot = (
            StrategyInputBuilder._dict(
                source_fact_ledger.get(
                    "source_snapshot",
                    {},
                )
            )
        )

        source_high_confidence = (
            StrategyInputBuilder._dict(
                source_fact_ledger.get(
                    "high_confidence",
                    {},
                )
            )
        )

        source_evidence = (
            StrategyInputBuilder._dict(
                source_fact_ledger.get(
                    "source_evidence",
                    {},
                )
            )
        )

        # =============================================
        # Build clean strategy input
        # =============================================

        strategy_input = {
            "schema_version":
                StrategyInputBuilder.SCHEMA_VERSION,

            # -----------------------------------------
            # Hard title constraints
            #
            # These are execution requirements, not preferences.
            # Strategy must create enough VERIFIED candidate coverage to
            # support the 61–75 character target whenever source facts allow.
            # -----------------------------------------

            "title_constraints": {
                "min_length_exclusive": 60,
                "min_length": 61,
                "max_length": 75,
                "identity_required": True,
                "compatibility_required_when_present": True,
                "primary_model_required_when_present": True,
                "no_invented_filler": True,
                "no_model_range_compression": True,
                "language_aware_word_order": True,
            },

            "target_language": (
                StrategyInputBuilder._text(
                    profile.get(
                        "target_language",
                        profile.get(
                            "language",
                            "English",
                        ),
                    )
                )
                or
                "English"
            ),

            # -----------------------------------------
            # Locked decisions
            # Title Strategy不得重新决定
            # -----------------------------------------

            "locked": {

                "identity": {
                    "text":
                        StrategyInputBuilder._text(
                            normalized_identity.get(
                                "text",
                                "",
                            )
                        ),

                    "source":
                        StrategyInputBuilder._text(
                            normalized_identity.get(
                                "source",
                                "",
                            )
                        ),

                    "confidence":
                        normalized_identity.get(
                            "confidence",
                            0,
                        ),
                },

                "compatibility": {
                    "phrase":
                        StrategyInputBuilder._text(
                            normalized_compatibility.get(
                                "phrase",
                                "",
                            )
                        ),

                    "brands":
                        StrategyInputBuilder._list(
                            normalized_compatibility.get(
                                "brands",
                                [],
                            )
                        ),
                },

                "models": {
                    "all":
                        StrategyInputBuilder._list(
                            normalized_models.get(
                                "all",
                                [],
                            )
                        ),

                    "primary":
                        StrategyInputBuilder._text(
                            normalized_models.get(
                                "primary",
                                "",
                            )
                        ),

                    "secondary":
                        StrategyInputBuilder._list(
                            normalized_models.get(
                                "secondary",
                                [],
                            )
                        ),
                },
            },

            # -----------------------------------------
            # Compatibility facts
            #
            # Brand compatibility and model compatibility are separate.
            # Pure numeric compatible models are preserved here.
            # -----------------------------------------

            "compatibility_facts": {
                "brands":
                    StrategyInputBuilder._list(
                        profile_compatibility.get("brands", [])
                    ),
                "models":
                    StrategyInputBuilder._list(
                        profile_compatibility.get("models", [])
                    ),
                "part_numbers":
                    StrategyInputBuilder._list(
                        profile_compatibility.get("part_numbers", [])
                    ),
                "notes":
                    StrategyInputBuilder._list(
                        profile_compatibility.get("compatibility_notes", [])
                    ),
                "important_compatibility":
                    StrategyInputBuilder._list(
                        profile_title_information.get("important_compatibility", [])
                    ),
                "third_party_brands":
                    StrategyInputBuilder._list(
                        profile_brand_info.get("third_party_brands", [])
                    ),
                "seller_brand":
                    StrategyInputBuilder._text(
                        profile_brand_info.get("seller_brand", "")
                    ),
            },

            # -----------------------------------------
            # Candidate facts
            # Strategy可以评估优先级，
            # 但不能修改事实。
            # -----------------------------------------

            "candidate_facts": {

                "priority_attributes":
                    StrategyInputBuilder._list(
                        title_information.get(
                            "priority_attributes",
                            [],
                        )
                    ),

                "important_specifications":
                    StrategyInputBuilder._list(
                        title_information.get(
                            "important_specifications",
                            [],
                        )
                    ),

                "important_quantity":
                    StrategyInputBuilder._text(
                        title_information.get(
                            "important_quantity",
                            "",
                        )
                    ),

                "important_context":
                    StrategyInputBuilder._list(
                        title_information.get(
                            "important_context",
                            [],
                        )
                    ),

                "design_features":
                    StrategyInputBuilder._list(
                        feature_classification.get(
                            "design_features",
                            [],
                        )
                    ),

                "functional_features":
                    StrategyInputBuilder._list(
                        feature_classification.get(
                            "functional_features",
                            [],
                        )
                    ),

                "usage_scenarios":
                    StrategyInputBuilder._list(
                        feature_classification.get(
                            "usage_scenarios",
                            [],
                        )
                    ),

                "specifications":
                    StrategyInputBuilder._list(
                        feature_classification.get(
                            "specifications",
                            [],
                        )
                    ),

                # V1.1 Candidate Pool Expansion
                #
                # These are candidate SOURCES only. Strategy must still verify
                # that they are fact-supported and add incremental value.
                "search_primary_keywords":
                    StrategyInputBuilder._list(
                        seo.get(
                            "primary_keywords",
                            [],
                        )
                    ),

                "search_secondary_keywords":
                    StrategyInputBuilder._list(
                        seo.get(
                            "secondary_keywords",
                            [],
                        )
                    ),

                "locked_all_models":
                    StrategyInputBuilder._list(
                        normalized_models.get(
                            "all",
                            [],
                        )
                    ),

                "locked_secondary_models":
                    StrategyInputBuilder._list(
                        normalized_models.get(
                            "secondary",
                            [],
                        )
                    ),

                # Source Fact Preservation V2.0
                #
                # These facts come directly from the original collected data.
                # They are NOT automatically selected for the title.
                # Strategy must classify value/meaning, but may not silently
                # ignore them when they are high-value and source-supported.
                "source_identifier_candidates":
                    StrategyInputBuilder._list(
                        source_high_confidence.get(
                            "identifier_candidates",
                            [],
                        )
                    ),

                "source_specifications":
                    StrategyInputBuilder._list(
                        source_high_confidence.get(
                            "specifications",
                            [],
                        )
                    ),

                "source_title_segments":
                    StrategyInputBuilder._list(
                        source_evidence.get(
                            "title_segments",
                            [],
                        )
                    ),

                "source_for_phrases":
                    StrategyInputBuilder._list(
                        source_evidence.get(
                            "for_phrases",
                            [],
                        )
                    ),

                "unresolved_source_facts":
                    StrategyInputBuilder._list(
                        source_fact_audit.get(
                            "unresolved_high_value",
                            [],
                        )
                    ),

                "compatibility_models":
                    StrategyInputBuilder._list(
                        profile_compatibility.get("models", [])
                    ),

                "compatibility_part_numbers":
                    StrategyInputBuilder._list(
                        profile_compatibility.get("part_numbers", [])
                    ),

                "important_compatibility":
                    StrategyInputBuilder._list(
                        profile_title_information.get("important_compatibility", [])
                    ),
            },

            # -----------------------------------------
            # Confirmed facts
            # -----------------------------------------

            "confirmed_facts": {
                "quantity":
                    facts.get(
                        "quantity",
                        "",
                    ),

                "material":
                    facts.get(
                        "material",
                        [],
                    ),

                "color":
                    facts.get(
                        "color",
                        "",
                    ),

                "dimensions":
                    facts.get(
                        "dimensions",
                        "",
                    ),

                "voltage":
                    facts.get(
                        "voltage",
                        "",
                    ),

                "power":
                    facts.get(
                        "power",
                        "",
                    ),

                "weight":
                    facts.get(
                        "weight",
                        "",
                    ),

                "part_numbers":
                    facts.get(
                        "part_numbers",
                        [],
                    ),

                "package_contents":
                    facts.get(
                        "package_contents",
                        [],
                    ),
            },

            # -----------------------------------------
            # Purpose / search intent
            # -----------------------------------------

            "purpose": {
                "primary_function":
                    StrategyInputBuilder._text(
                        purpose.get(
                            "primary_function",
                            "",
                        )
                    ),

                "primary_use":
                    StrategyInputBuilder._text(
                        purpose.get(
                            "primary_use",
                            "",
                        )
                    ),
            },

            "search": {
                "intent":
                    StrategyInputBuilder._text(
                        seo.get(
                            "search_intent",
                            "",
                        )
                    ),

                "primary_keywords":
                    StrategyInputBuilder._list(
                        seo.get(
                            "primary_keywords",
                            [],
                        )
                    ),

                "secondary_keywords":
                    StrategyInputBuilder._list(
                        seo.get(
                            "secondary_keywords",
                            [],
                        )
                    ),
            },

            # -----------------------------------------
            # Source Evidence / Coverage Audit
            #
            # This exists specifically to prevent PIPELINE_FACT_LOSS.
            # It is evidence, not pre-approved title copy.
            # -----------------------------------------

            "source_evidence": {
                "raw_title":
                    StrategyInputBuilder._text(
                        source_snapshot.get(
                            "title",
                            "",
                        )
                    ),

                "source_title_segments":
                    StrategyInputBuilder._list(
                        source_evidence.get(
                            "title_segments",
                            [],
                        )
                    ),

                "source_for_phrases":
                    StrategyInputBuilder._list(
                        source_evidence.get(
                            "for_phrases",
                            [],
                        )
                    ),

                "identifier_candidates":
                    StrategyInputBuilder._list(
                        source_high_confidence.get(
                            "identifier_candidates",
                            [],
                        )
                    ),

                "identifier_candidates_from_title":
                    StrategyInputBuilder._list(
                        source_high_confidence.get(
                            "identifier_candidates_from_title",
                            [],
                        )
                    ),

                "specifications":
                    StrategyInputBuilder._list(
                        source_high_confidence.get(
                            "specifications",
                            [],
                        )
                    ),

                "quantities":
                    StrategyInputBuilder._list(
                        source_high_confidence.get(
                            "quantities",
                            [],
                        )
                    ),

                "unresolved_identifiers":
                    StrategyInputBuilder._list(
                        source_fact_audit.get(
                            "unresolved_identifiers",
                            [],
                        )
                    ),

                "unresolved_source_phrases":
                    StrategyInputBuilder._list(
                        source_fact_audit.get(
                            "unresolved_source_phrases",
                            [],
                        )
                    ),

                "unresolved_high_value":
                    StrategyInputBuilder._list(
                        source_fact_audit.get(
                            "unresolved_high_value",
                            [],
                        )
                    ),

                "coverage_status":
                    StrategyInputBuilder._text(
                        source_fact_audit.get(
                            "coverage_status",
                            "",
                        )
                    ),

                "silent_drop_detected":
                    bool(
                        source_fact_audit.get(
                            "silent_drop_detected",
                            False,
                        )
                    ),
            },

            # -----------------------------------------
            # Compliance
            # -----------------------------------------

            "compliance": {
                "brand_relationship":
                    StrategyInputBuilder._text(
                        compliance.get(
                            "brand_relationship",
                            "",
                        )
                    ),

                "brand_usage_rule":
                    StrategyInputBuilder._text(
                        compliance.get(
                            "brand_usage_rule",
                            "",
                        )
                    ),

                "blocked_claims":
                    StrategyInputBuilder._list(
                        compliance.get(
                            "blocked_claims",
                            [],
                        )
                    ),

                "seller_brand":
                    StrategyInputBuilder._text(
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
                    ),
            },
        }

        # Identity 是 Strategy 必需数据。
        if not strategy_input[
            "locked"
        ][
            "identity"
        ][
            "text"
        ]:

            raise StrategyInputBuilderError(
                "Normalized identity is missing"
            )

        return strategy_input

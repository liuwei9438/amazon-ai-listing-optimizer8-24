from __future__ import annotations


class KnowledgeNormalizerError(Exception):
    pass


class KnowledgeNormalizer:
    """
    Knowledge Normalizer V1.0

    职责：
    - 不修改 Raw Knowledge
    - 不重新理解产品
    - 不重新调用 AI
    - 不决定哪个 Identity 更好
    - 只把已经确认的数据映射到统一结构

    当前 V1.0 只统一：
    1. Identity
    2. Compatibility
    3. Models
    """

    SCHEMA_VERSION = "1.0"


    @staticmethod
    def _clean_text(
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
    def _clean_list(
        value,
    ) -> list[str]:

        if not isinstance(
            value,
            list,
        ):
            return []

        result = []

        seen = set()

        for item in value:

            text = (
                KnowledgeNormalizer
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
    def normalize(
        profile: dict,
    ) -> dict:
        """
        根据已经存在的：

        profile["product_knowledge"]
        profile["identity_decision"]

        建立统一读取视图。

        注意：
        这里只做映射，
        不重新决策。
        """

        if not isinstance(
            profile,
            dict,
        ):

            raise KnowledgeNormalizerError(
                "Profile must be a dictionary"
            )


        # =============================================
        # Product Knowledge
        # =============================================

        product_knowledge = profile.get(
            "product_knowledge",
            {},
        )

        if not isinstance(
            product_knowledge,
            dict,
        ):

            product_knowledge = {}


        knowledge_identity = product_knowledge.get(
            "identity",
            {},
        )

        if not isinstance(
            knowledge_identity,
            dict,
        ):

            knowledge_identity = {}


        relationship = product_knowledge.get(
            "relationship",
            {},
        )

        if not isinstance(
            relationship,
            dict,
        ):

            relationship = {}


        # =============================================
        # Identity Decision
        # =============================================

        identity_decision = profile.get(
            "identity_decision",
            {},
        )

        if not isinstance(
            identity_decision,
            dict,
        ):

            identity_decision = {}


        canonical_identity = (
            identity_decision.get(
                "canonical_identity",
                {},
            )
        )

        if not isinstance(
            canonical_identity,
            dict,
        ):

            canonical_identity = {}


        canonical_text = (
            KnowledgeNormalizer
            ._clean_text(
                canonical_identity.get(
                    "text",
                    "",
                )
            )
        )


        # =============================================
        # Identity fallback
        #
        # 这里只是防止 Identity Decision 异常。
        #
        # 不进行新的语义判断。
        # =============================================

        if not canonical_text:

            canonical_text = (
                KnowledgeNormalizer
                ._clean_text(
                    knowledge_identity.get(
                        "object_name",
                        "",
                    )
                )
            )


        identity_source = (
            KnowledgeNormalizer
            ._clean_text(
                canonical_identity.get(
                    "decision_source",
                    "",
                )
            )
        )


        identity_confidence = (
            canonical_identity.get(
                "confidence",
                0,
            )
        )


        # =============================================
        # Compatibility
        # =============================================

        compatibility_phrase = (
            KnowledgeNormalizer
            ._clean_text(
                relationship.get(
                    "compatibility_phrase",
                    "",
                )
            )
        )


        brands = (
            KnowledgeNormalizer
            ._clean_list(
                relationship.get(
                    "brands",
                    [],
                )
            )
        )


        # =============================================
        # Models
        # =============================================

        models = (
            KnowledgeNormalizer
            ._clean_list(
                relationship.get(
                    "models",
                    [],
                )
            )
        )


        model_priority = relationship.get(
            "model_priority",
            {},
        )

        if not isinstance(
            model_priority,
            dict,
        ):

            model_priority = {}


        primary_model = (
            KnowledgeNormalizer
            ._clean_text(
                model_priority.get(
                    "primary_model",
                    "",
                )
            )
        )


        secondary_models = (
            KnowledgeNormalizer
            ._clean_list(
                model_priority.get(
                    "secondary_models",
                    [],
                )
            )
        )


        # 如果现有 Knowledge 没有单独的 primary_model，
        # 不在这里猜。
        #
        # models 保持完整事实即可。


        # =============================================
        # Final normalized view
        # =============================================

        return {
            "schema_version":
                KnowledgeNormalizer.SCHEMA_VERSION,

            "identity":
            {
                "text":
                    canonical_text,

                "source":
                    identity_source,

                "confidence":
                    identity_confidence,
            },

            "compatibility":
            {
                "phrase":
                    compatibility_phrase,

                "brands":
                    brands,
            },

            "models":
            {
                "all":
                    models,

                "primary":
                    primary_model,

                "secondary":
                    secondary_models,
            },
        }

from __future__ import annotations

import re


class HighlightGenerator:

    """
    Amazon AI Listing Optimizer

    Highlight Generator V3.0 Stable

    数据来源:
    Product Knowledge

    职责:
    - 提取商品核心亮点
    - 不重新理解产品
    - 不猜测产品属性
    - 不根据关键词判断功能
    """



    BLOCKED_WORDS = [

        "best",
        "best seller",
        "#1",

        "premium",
        "original",
        "genuine",
        "official",
        "authentic",

        "discount",
        "promotion",

        "perfect",
        "amazing",

        "top quality",
        "high quality",

    ]


    @staticmethod
    def generate(
        profile: dict
    ) -> dict:
    
    
        knowledge = profile.get(
            "product_knowledge",
            {}
        )
    
    
        if not isinstance(
            knowledge,
            dict
        ):
            knowledge = {}
    
    
        identity = knowledge.get(
            "identity",
            {}
        )
    
    
        feature_classification = knowledge.get(
            "feature_classification",
            {}
        )
    
    
        highlights = []
    
    
        # =================================================
        # 1. Product Identity
        # 产品主体
        # =================================================
    
        if isinstance(
            identity,
            dict
        ):
    
            product_name = (
                identity.get(
                    "product_name"
                )
                or
                identity.get(
                    "object_name"
                )
                or
                ""
            )
    
    
            if product_name:
    
                highlights.append(
                    {
                        "type":
                        "product",
    
                        "text":
                        product_name,
                    }
                )
    
    
    
        # =================================================
        # 2. Feature Collection
        # 商品特点
        # =================================================
    
        highlight_focus = []
    
    
        # -----------------------------
        # Identity Features
        # -----------------------------
    
        if isinstance(
            identity,
            dict
        ):
    
            highlight_focus.extend(
                identity.get(
                    "design_features",
                    []
                )
            )
    
    
            highlight_focus.extend(
                identity.get(
                    "functional_features",
                    []
                )
            )
    
    
    
        # -----------------------------
        # Fact Features
        # -----------------------------
    
        if isinstance(
            feature_classification,
            dict
        ):
    
            highlight_focus.extend(
                feature_classification.get(
                    "materials",
                    []
                )
            )
    
    
            highlight_focus.extend(
                feature_classification.get(
                    "specifications",
                    []
                )
            )
    
    
    
        # -----------------------------
        # Fallback old logic
        # -----------------------------
    
        if not highlight_focus:
    
            strategy = knowledge.get(
                "generation_strategy",
                {}
            )
    
    
            if isinstance(
                strategy,
                dict
            ):
    
                highlight_focus.extend(
                    strategy.get(
                        "highlight_focus",
                        []
                    )
                )
    
    
    
        for feature in highlight_focus:
    
            text = HighlightGenerator.clean_text(
                feature
            )
    
    
            if text:
    
                highlights.append(
                    {
                        "type":
                        "feature",
    
                        "text":
                        text,
                    }
                )
    
    
    
        # =================================================
        # 3. Compatibility
        # =================================================
    
        compatibility = knowledge.get(
            "relationship",
            {}
        )
    
    
        if isinstance(
            compatibility,
            dict
        ):
    
            brands = compatibility.get(
                "brands",
                []
            )
    
    
            if brands:
    
                highlights.append(
                    {
                        "type":
                        "compatibility",
    
                        "text":
                        HighlightGenerator.build_compatibility(
                            brands
                        )
                    }
                )
    
    
    
        # =================================================
        # 4. Clean
        # =================================================
    
        highlights = (
            HighlightGenerator.clean_highlights(
                highlights
            )
        )
    
    
        blocked = (
            HighlightGenerator.check_blocked_words(
                str(highlights)
            )
        )
    
    
        return {
    
            "highlights":
                highlights,
    
    
            "validation":
            {
                "compliance_ok":
                    len(blocked) == 0
            },
    
    
            "blocked_words":
                blocked,
    
        }

    # =========================
    # 兼容表达
    # =========================

    @staticmethod
    def build_compatibility(
        brands
    ):


        return (
            "Compatible with "
            +
            ", ".join(
                [
                    str(x)
                    for x in brands[:3]
                ]
            )
            +
            " Models"
        )



    # =========================
    # 去重清理
    # =========================

    @staticmethod
    def clean_highlights(
        highlights
    ):


        result = []

        seen = set()


        for item in highlights:


            text = HighlightGenerator.clean_text(
                item.get(
                    "text",
                    ""
                )
            )


            if not text:

                continue



            key = (
                item.get(
                    "type",
                    ""
                )
                +
                "_"
                +
                text.lower()
            )


            if key not in seen:

                result.append(
                    {
                        "type":
                        item.get(
                            "type",
                            ""
                        ),

                        "text":
                        text,
                    }
                )


                seen.add(
                    key
                )



        return result



    # =========================
    # 文本清理
    # =========================

    @staticmethod
    def clean_text(
        text
    ):


        return re.sub(
            r"\s+",
            " ",
            str(text)
        ).strip()



    # =========================
    # 禁用词检查
    # =========================

    @staticmethod
    def check_blocked_words(
        text
    ):


        found = []


        for word in HighlightGenerator.BLOCKED_WORDS:


            if re.search(
                r"\b"
                +
                re.escape(word)
                +
                r"\b",
                text,
                flags=re.I,
            ):

                found.append(
                    word
                )


        return found

from __future__ import annotations

import re


class DescriptionGenerator:

    """
    Amazon AI Listing Optimizer

    Description Generator V2.4.1 Stable

    功能:
    - Amazon 风格详情描述生成
    - 保留事实信息
    - 提取产品用途
    - 提取兼容信息
    - 融合 Highlight
    - 自动过滤违规营销词
    """

    BLOCKED_WORDS = [

        "best",
        "best seller",
        "#1",
        "number one",

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

    # 不是品牌的"品牌"：AI 偶尔把 "3D Printer" 拆开，
    # 把 Print / 3D 当成兼容品牌输出（"Compatible with Print"）。
    JUNK_BRAND_WORDS = {
        "print",
        "prints",
        "3d",
        "printer",
        "printing",
    }


    @staticmethod
    def generate(
        profile: dict,
        highlights
    ) -> dict:


        basic = profile.get(
            "basic_info",
            {}
        )


        compatibility = profile.get(
            "compatibility",
            {}
        )


        paragraphs = []



        # =========================
        # 产品定位
        # =========================

        product_intro = (
            DescriptionGenerator.build_product_intro(
                profile
            )
        )


        if product_intro:

            paragraphs.append(
                product_intro
            )



        # =========================
        # Highlight卖点
        # =========================

        highlight_items = (
            DescriptionGenerator.extract_highlights(
                highlights
            )
        )


        for item in highlight_items:

            if item:

                paragraphs.append(
                    item
                )



        # =========================
        # 兼容信息
        # =========================

        compatibility_text = (
            DescriptionGenerator.build_compatibility(
                compatibility
            )
        )


        if compatibility_text:

            has_compatible = any(
                "compatible with" in item.lower()
                for item in highlight_items
            )


            if not has_compatible:

                paragraphs.append(
                    compatibility_text
                )



        # =========================
        # 购买提示
        # =========================

        paragraphs.append(
            "Please check your original part number and model before purchase to ensure compatibility."
        )



        result = []


        for text in paragraphs:


            cleaned = DescriptionGenerator.clean(
                text
            )


            if (
                cleaned
                and
                cleaned not in result
            ):

                result.append(
                    cleaned
                )



        description = "\n\n".join(
            result
        )


        blocked_words = (
            DescriptionGenerator.check_blocked_words(
                description
            )
        )


        return {

            "description": description,

            "validation": {

                "compliance_ok":
                    len(blocked_words) == 0

            },

            "blocked_words": blocked_words

        }



    # =========================
    # 产品定位生成
    # =========================
    @staticmethod
    def build_product_intro(
        profile: dict
    ):

        basic = profile.get(
            "basic_info",
            {}
        )


        product_type = str(
            basic.get(
                "product_type",
                ""
            )
        )


        main_function = str(
            basic.get(
                "main_function",
                ""
            )
        )


        # V2.6.1：简介开头不再拼接功能短语——AI 返回的功能形态不定
        # （第三人称动词 / 动名词 / 名词短语都有），固定句式会拼出
        # "designed for drives filament" 这类语法错误。
        # 改成安全的中性句，功能信息由要点2的 "Function: ..." 承载。
        type_text = (
            str(
                product_type
            )
            .strip()
            .rstrip(
                "."
            )
        )

        if not type_text:

            return ""

        # a/an 冠词随首字母元音切换，避免 "a encoder motor" 这类小错。
        article = (
            "an"
            if type_text[:1].lower()
            in "aeiou"
            else "a"
        )

        return (
            f"This is {article} "
            f"{type_text.lower()}."
        )
    
    # =========================
    # Highlight 提取
    # =========================

    @staticmethod
    def extract_highlights(
        highlights
    ):
    
        result = []
    
    
        if isinstance(
            highlights,
            dict
        ):
    
            data = highlights.get(
                "highlights",
                []
            )
    
    
            if isinstance(
                data,
                list
            ):
    
                for item in data:
    
                    if isinstance(
                        item,
                        dict
                    ):
    
                        text = item.get(
                            "text",
                            ""
                        )
    
                    else:
    
                        text = item
    
    
                    if text:
    
                        result.append(
                            str(text)
                        )
    
    
    
        elif isinstance(
            highlights,
            list
        ):
    
            for item in highlights:
    
                if isinstance(
                    item,
                    dict
                ):
    
                    text = item.get(
                        "text",
                        ""
                    )
    
                else:
    
                    text = item
    
    
                if text:
    
                    result.append(
                        str(text)
                    )
    
    
        return DescriptionGenerator.remove_duplicate(
            result
        )



    # =========================
    # 兼容信息生成
    # =========================

    @staticmethod
    def build_compatibility(
        compatibility
    ):


        if not isinstance(
            compatibility,
            dict
        ):

            return ""



        brands = [
            str(brand).strip()
            for brand in compatibility.get(
                "brands",
                []
            )
            if str(brand)
            .strip()
            .lower()
            not in DescriptionGenerator.JUNK_BRAND_WORDS
        ]


        models = compatibility.get(
            "models",
            []
        )



        if not brands:

            return ""



        brand_text = ", ".join(
            [
                str(x)
                for x in brands[:3]
            ]
        )



        if models:


            if len(models) > 4:

                model_text = (
                    " ".join(
                        [
                            str(x)
                            for x in models[:3]
                        ]
                    )
                    +
                    " and more models"
                )


            else:

                model_text = " ".join(
                    [
                        str(x)
                        for x in models
                    ]
                )



            return (
                f"Compatible with {brand_text} "
                f"{model_text}."
            )



        return (
            f"Compatible with {brand_text} models."
        )



    # =========================
    # 去重复
    # =========================

    @staticmethod
    def remove_duplicate(
        items
    ):

        result = []

        seen = set()


        for item in items:

            key = str(item).lower().strip()


            if key not in seen:

                result.append(
                    item
                )

                seen.add(
                    key
                )


        return result



    # =========================
    # 文本清理
    # =========================

    @staticmethod
    def clean(
        text
    ):

        text = re.sub(
            r"\s+",
            " ",
            str(text)
        )


        return text.strip()



    # =========================
    # 禁用词检查
    # =========================

    @staticmethod
    def check_blocked_words(
        text
    ):

        found = []


        for word in DescriptionGenerator.BLOCKED_WORDS:


            if re.search(
                r"\b" + re.escape(word) + r"\b",
                text,
                flags=re.I,
            ):

                found.append(
                    word
                )


        return found
        

from __future__ import annotations

import re


class ShortTitleGenerator:

    """
    Amazon AI Listing Optimizer

    Short Title Generator V2.6 Stable

    数据来源:
    Product Knowledge

    职责:
    - 生成产品快速识别标题
    - 不生成营销卖点
    - 不重新理解产品
    - 不读取原始标题
    """


    MAX_LENGTH = 80



    COMPATIBLE_PHRASES = {

        "English":
            "Compatible with",

        "Spanish":
            "Compatible con",

        "German":
            "Kompatibel mit",

        "French":
            "Compatible avec",

        "Italian":
            "Compatibile con",

    }



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


        compatibility = knowledge.get(
            "relationship",
            {}
        )


        strategy = knowledge.get(
            "generation_strategy",
            {}
        )



        parts = []



        # =========================
        # 产品核心
        # =========================

        product_name = (

            identity.get(
                "object_name"
            )
        
            or
        
            identity.get(
                "product_name"
            )
        
            or
        
            identity.get(
                "product_type"
            )
        
            or ""
        
        )



        if product_name:

            parts.append(
                product_name
            )



        # =========================
        # 规格型特点
        # =========================

        short_focus = strategy.get(
            "short_title_focus",
            []
        )


        if isinstance(
            short_focus,
            list
        ):

            for item in short_focus:


                if (
                    item
                    and
                    item.lower()
                    not in product_name.lower()
                ):
                
                    parts.append(
                        item
                    )
                
                    break


        # =========================
        # 兼容品牌
        # =========================

        brands = compatibility.get(
            "brands",
            []
        )


        if brands:

            phrase = (
                ShortTitleGenerator.get_phrase(
                    profile
                )
            )
        
        
            parts.append(
                phrase
                +
                " "
                +
                str(brands[0])
            )



        title = " ".join(
            parts
        )



        return {

            "short_title":

                ShortTitleGenerator.clean(
                    title
                )

        }



    # =========================
    # 多语言接口
    # =========================

    @staticmethod
    def get_phrase(
        profile
    ):

        language = profile.get(
            "language",
            "English"
        )


        return (
            ShortTitleGenerator.COMPATIBLE_PHRASES.get(
                language,
                "Compatible with"
            )
        )



    # =========================
    # 规格判断
    # =========================

    @staticmethod
    def is_specification(
        text
    ):


        value = str(
            text
        ).lower()



        allowed = [

            "9d",
            "6-in-1",
            "ipx7",
            "led",
            "wireless",
            "cordless",
            "portable",
            "mini",

        ]



        return any(

            x in value

            for x in allowed

        )



    # =========================
    # 清理
    # =========================

    @staticmethod
    def clean(
        text
    ):


        return re.sub(
            r"\s+",
            " ",
            str(text)
        ).strip()[:ShortTitleGenerator.MAX_LENGTH]

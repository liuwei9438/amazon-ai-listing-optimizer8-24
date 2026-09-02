from __future__ import annotations

import re
from typing import Any


class BulletGenerator:
    """
    Amazon AI Listing Optimizer

    Bullet Generator V5

    原则:
    - 只使用 Product Knowledge 已确认信息
    - 不生成营销承诺
    - 不添加未经确认优势
    - 不改变型号、材质、规格
    - 五点负责解释商品事实
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
        "oem",
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
        highlights: Any = None,
    ) -> dict:

        knowledge = profile.get(
            "product_knowledge",
            {}
        )


        if not isinstance(
            knowledge,
            dict,
        ):
            knowledge = {}


        identity = knowledge.get(
            "identity",
            {},
        )


        purpose = knowledge.get(
            "purpose",
            {},
        )


        relationship = knowledge.get(
            "relationship",
            {},
        )


        facts = knowledge.get(
            "facts",
            {},
        )


        feature_classification = knowledge.get(
            "feature_classification",
            {},
        )


        bullets = []


        # =========================
        # Bullet 1
        # 产品定位
        # =========================

        product_text = (
            BulletGenerator.build_product_identity(
                identity,
                relationship,
            )
        )


        if product_text:

            bullets.append(
                product_text
            )



        # =========================
        # Bullet 2
        # 核心功能
        # =========================

        function_text = (
            BulletGenerator.build_function(
                purpose
            )
        )


        if function_text:

            bullets.append(
                function_text
            )



        # =========================
        # Bullet 3
        # 兼容信息
        # =========================

        compatibility_text = (
            BulletGenerator.build_compatibility(
                relationship
            )
        )


        if compatibility_text:

            bullets.append(
                compatibility_text
            )



        # =========================
        # Bullet 4
        # 规格参数
        # =========================

        specification_text = (
            BulletGenerator.build_specifications(
                facts
            )
        )


        if specification_text:

            bullets.append(
                specification_text
            )



        # =========================
        # Bullet 5
        # 产品特点
        # =========================

        feature_text = (
            BulletGenerator.build_features(
                feature_classification,
                identity,
            )
        )


        if feature_text:

            bullets.append(
                feature_text
            )


        # =========================
        # 补足 5 条（V2.6）
        #
        # 导出时要点槽位只写新生成内容，条数不足会保留原表旧值，
        # 而原表旧要点常是历史遗留的乱数据。这里用亮点文案 +
        # 核对提示把 5 个槽位填满，保证导出内容全部是新生成的。
        # =========================

        if len(bullets) < 5:

            for item in BulletGenerator.extract_highlight_texts(
                highlights
            ):

                if len(bullets) >= 5:

                    break

                if not item:

                    continue

                item_key = item.casefold()

                # 跳过两种低质量补位：
                # 1. 纯规格串（"13 x 9 x 8 cm"、"0.150 kg"）
                #    单独当一条要点没有可读性；
                # 2. 与已有要点重复/被包含（产品名换个说法再出现一次）。
                if BulletGenerator.is_spec_only(item):

                    continue

                if any(
                    item_key in existing
                    or existing in item_key
                    for existing in (
                        str(bullet).casefold()
                        for bullet in bullets
                    )
                ):

                    continue

                bullets.append(
                    item
                )


        for fallback in (
            "Please confirm the original part number and model "
            "before purchase to ensure compatibility.",

            "Package contents and specifications are listed above; "
            "please review them before ordering.",

            "Please check the product images and stated dimensions "
            "carefully to ensure this item matches your needs.",

            "If anything is unclear, verify the details against "
            "your equipment model before ordering.",
        ):

            if len(bullets) >= 5:

                break

            if fallback not in bullets:

                bullets.append(
                    fallback
                )


        bullets = [
            BulletGenerator.clean(item)
            for item in bullets
            if item
        ]


        bullets = (
            BulletGenerator.remove_duplicate(
                bullets
            )
        )


        bullets = bullets[:5]


        blocked_words = (
            BulletGenerator.check_blocked_words(
                str(bullets)
            )
        )


        return {

            "bullets":
                bullets,


            "validation":
            {

                "compliance_ok":
                    len(blocked_words) == 0,

            },


            "blocked_words":
                blocked_words,

        }



    @staticmethod
    def build_product_identity(
        identity: dict,
        relationship: dict,
    ) -> str:

        product_name = BulletGenerator.first_text(
            identity.get("product_name"),
            identity.get("object_name"),
        )


        if not product_name:

            return ""


        brands = relationship.get(
            "brands",
            [],
        )


        parent_product = BulletGenerator.first_text(
            identity.get("parent_product")
        )


        if brands and parent_product:

            return (
                f"Compatible replacement {product_name} "
                f"for {brands[0]} {parent_product} models."
            )


        return product_name



    @staticmethod
    def build_function(
        purpose: dict,
    ) -> str:

        function = BulletGenerator.first_text(
            purpose.get("primary_function")
        )


        if not function:

            return ""

        # V2.6.1：AI 返回的功能短语形态不定（第三人称动词 / 动名词 /
        # 名词短语都有），固定写 "Used to xxx" 会拼出
        # "Used to drives"、"Used to heating" 这类语法错误。
        # 改成 "Function: {首字母大写}。" 对任何形态都通顺。
        function_text = re.sub(
            r"^to\s+",
            "",
            function.strip().rstrip("."),
            flags=re.I,
        )

        if not function_text:

            return ""

        function_text = (
            function_text[0].upper()
            +
            function_text[1:]
        )

        return (
            f"Function: {function_text}."
        )

    @staticmethod
    def build_compatibility(
        relationship: dict,
    ) -> str:

        if not isinstance(
            relationship,
            dict,
        ):
            return ""


        brands = [
            str(brand).strip()
            for brand in relationship.get(
                "brands",
                [],
            )
            if str(brand)
            .strip()
            .lower()
            not in BulletGenerator.JUNK_BRAND_WORDS
        ]


        models = relationship.get(
            "models",
            [],
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

            model_text = ", ".join(
                [
                    str(x)
                    for x in models[:4]
                ]
            )


            return (
                f"Compatibility: Compatible with {brand_text} "
                f"{model_text} models. "
                "Please verify compatibility before purchase."
            )


        return (
            f"Compatibility: Compatible with {brand_text} models."
        )



    @staticmethod
    def build_specifications(
        facts: dict,
    ) -> str:

        if not isinstance(
            facts,
            dict,
        ):

            return ""


        values = []


        material = BulletGenerator.first_value(
            facts.get("material")
        )

        if material:

            values.append(
                f"Material: {material}"
            )


        dimensions = BulletGenerator.first_value(
            facts.get("dimensions")
        )

        if dimensions:

            values.append(
                f"Dimensions: {dimensions}"
            )


        weight = BulletGenerator.first_value(
            facts.get("weight")
        )

        if weight:

            values.append(
                f"Weight: {weight}"
            )


        voltage = BulletGenerator.first_value(
            facts.get("voltage")
        )

        if voltage:

            values.append(
                f"Voltage: {voltage}"
            )


        power = BulletGenerator.first_value(
            facts.get("power")
        )

        if power:

            values.append(
                f"Power: {power}"
            )


        if not values:

            return ""


        return (
            "Specifications: "
            +
            "; ".join(values)
            +
            "."
        )



    @staticmethod
    def build_features(
        feature_classification: dict,
        identity: dict,
    ) -> str:

        if not isinstance(
            feature_classification,
            dict,
        ):

            return ""


        features = []


        for key in (
            "design_features",
            "functional_features",
            "materials",
        ):

            items = feature_classification.get(
                key,
                [],
            )


            if isinstance(
                items,
                list,
            ):

                for item in items:

                    value = BulletGenerator.first_value(
                        item
                    )

                    if value:

                        features.append(
                            value
                        )


        if not features:

            # V2.6.1：不再用 "Features: {产品名}" 兜底——那只是把要点1
            # 换个前缀重复一遍。没有真实特点就返回空，
            # 由亮点补位 + 购买提示把 5 个槽位填满。

            return ""


        joined = (
            "; ".join(
                features[:3]
            )
        )

        if not joined:

            return ""

        return (
            "Features: "
            +
            joined
            +
            "."
        )



    @staticmethod
    def is_spec_only(
        text: str,
    ) -> bool:
        """判断是否为纯规格串（如 "13 x 9 x 8 cm"、"0.150 kg"）。

        规则：含数字，且去掉单位词后剩下的实义词不超过 1 个。
        """

        value = str(
            text
        ).strip()

        if not value or not re.search(
            r"\d",
            value,
        ):

            return False

        units = {
            "mm", "cm", "m", "kg", "g", "v", "w", "oz", "ml", "l",
            "mah", "x", "℃",
        }

        real_words = [
            word
            for word in re.findall(
                r"[A-Za-z]+",
                value,
            )
            if word.lower() not in units
        ]

        return len(real_words) <= 1


    @staticmethod
    def extract_highlight_texts(
        highlights,
    ) -> list:
        """从 highlight_result 里取出纯文本列表，用于补足要点条数。"""

        if isinstance(
            highlights,
            dict,
        ):

            data = highlights.get(
                "highlights",
                [],
            )

        elif isinstance(
            highlights,
            list,
        ):

            data = highlights

        else:

            data = []


        texts = []

        if isinstance(
            data,
            list,
        ):

            for item in data:

                if isinstance(
                    item,
                    dict,
                ):

                    text = str(
                        item.get(
                            "text"
                        )
                        or
                        ""
                    ).strip()

                else:

                    text = str(
                        item
                        or
                        ""
                    ).strip()


                if text:

                    texts.append(
                        text
                    )


        return texts


    @staticmethod
    def first_text(
        *values,
    ) -> str:

        for value in values:

            text = BulletGenerator.first_value(
                value
            )

            if text:

                return text


        return ""



    @staticmethod
    def first_value(
        value,
    ) -> str:

        if value is None:

            return ""


        if isinstance(
            value,
            list,
        ):

            if not value:

                return ""

            value = value[0]


        if isinstance(
            value,
            dict,
        ):

            value = value.get(
                "value",
                "",
            )


        text = str(
            value
        ).strip()


        if text.lower() in (
            "",
            "none",
            "null",
            "unknown",
            "n/a",
            "[]",
            "{}",
        ):

            return ""


        return text



    @staticmethod
    def clean(
        text: str,
    ) -> str:

        return re.sub(
            r"\s+",
            " ",
            str(text),
        ).strip()



    @staticmethod
    def remove_duplicate(
        items,
    ):

        result = []

        seen = set()


        for item in items:

            key = (
                str(item)
                .lower()
                .strip()
            )


            if key not in seen:

                result.append(
                    item
                )

                seen.add(
                    key
                )


        return result



    @staticmethod
    def check_blocked_words(
        text,
    ):

        found = []


        for word in BulletGenerator.BLOCKED_WORDS:

            if re.search(
                r"\b"
                +
                re.escape(word)
                +
                r"\b",
                str(text),
                flags=re.I,
            ):

                found.append(
                    word
                )


        return found

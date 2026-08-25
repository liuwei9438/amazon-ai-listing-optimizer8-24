from __future__ import annotations

from typing import Dict, Any


class TitlePlanner:

    """
    标题策略规划器 V2.4.4

    功能:

    1. 提取标题核心产品身份
    2. 提取标题搜索补充词
    3. 筛选高价值标题卖点
    4. 提取兼容信息
    5. 提取标题避免信息

    不生成最终标题。
    """

    @staticmethod
    def plan(
        product_knowledge: Dict[str, Any]
    ) -> Dict[str, Any]:

        identity = product_knowledge.get(
            "identity",
            {}
        )

        relationship = product_knowledge.get(
            "relationship",
            {}
        )


        return {

            "main_product":
                TitlePlanner.get_main_product(
                    product_knowledge
                ),


            "search_terms":
                TitlePlanner.get_search_terms(
                    product_knowledge
                ),


            "features":
                TitlePlanner.get_features(
                    product_knowledge
                ),
            
            "title_attributes":
                TitlePlanner.get_title_attributes(
                    product_knowledge
                ),

            "compatibility":
                TitlePlanner.get_compatibility(
                    relationship
                ),


            "avoid":
                TitlePlanner.get_avoid_terms(
                    identity
                ),

        }



    # =====================================================
    # 核心产品身份
    # =====================================================

    @staticmethod
    def get_main_product(
        product_knowledge
    ):
    
        identity = product_knowledge.get(
            "identity",
            {}
        )
    
    
        basic_info = product_knowledge.get(
            "basic_info",
            {}
        )
    
    
        candidates = [

            # 第一优先级：
            # 标题专用产品身份
            identity.get(
                "title_product_identity",
                ""
            ),
        
        
            # 第二优先级：
            # 产品本体名称
            identity.get(
                "object_name",
                ""
            ),
        
        
            # 第三优先级：
            # 产品类型
            basic_info.get(
                "product_type",
                ""
            ),
        
        
            # 第四优先级：
            # 买家搜索身份（备用）
            identity.get(
                "buyer_search_identity",
                ""
            ),
        
        
            # 最后备用
            identity.get(
                "product_name",
                ""
            ),
        
        ]
    
    
        for value in candidates:
    
    
            value = str(
                value
            ).strip()
    
    
            if not value:
    
                continue
    
    
            if TitlePlanner.is_low_value_identity(
                value
            ):
    
                continue
    
    
            return [
                TitlePlanner.normalize_main_product_identity(
                    value
                )
            ]
    
        return []



    # =====================================================
    # 判断低价值身份词
    # =====================================================

    @staticmethod
    def is_low_value_identity(
        value: str
    ):

        text = value.lower()


        if text in [
            "parts",
            "accessories",
            "replacement",
            "replacement parts",
        ]:
            return True


        return False
    @staticmethod
    def normalize_main_product_identity(
        text: str
    ):
    
        remove_patterns = [
            "compatible with",
            "compatible",
            "replacement",
            "replacement for",
            "for",
        ]
    
    
        result = text.lower()
    
    
        for pattern in remove_patterns:
    
            result = result.replace(
                pattern,
                ""
            )
    
    
        words = result.split()
    
    
        return " ".join(
            words[:5]
        ).title()
    # =====================================================
    # 搜索补充词
    # 仅作为标题辅助，不直接堆砌
    # =====================================================

    @staticmethod
    def get_search_terms(
        product_knowledge
    ):

        result = []


        seo = product_knowledge.get(
            "seo",
            {}
        )


        keywords = seo.get(
            "secondary_keywords",
            []
        )


        if isinstance(
            keywords,
            list
        ):

            for item in keywords:

                text = str(
                    item
                ).strip()


                if not text:
                    continue


                if TitlePlanner.is_low_value_keyword(
                    text
                ):
                    continue


                result.append(
                    text
                )


        return TitlePlanner.clean_list(
            result
        )[:8]



    # =====================================================
    # 标题高价值卖点
    # =====================================================

    @staticmethod
    def get_features(
        product_knowledge
    ):

        classification = (
            product_knowledge.get(
                "feature_classification",
                {}
            )
        )


        design_features = classification.get(
            "design_features",
            []
        )


        functional_features = classification.get(
            "functional_features",
            []
        )


        candidates = []


        if isinstance(
            design_features,
            list
        ):

            candidates.extend(
                design_features
            )


        if isinstance(
            functional_features,
            list
        ):

            candidates.extend(
                functional_features
            )



        filtered = []


        for item in candidates:


            text = str(
                item
            ).strip()


            if not text:
                continue



            if TitlePlanner.is_title_feature(
                text
            ):

                filtered.append(
                    text
                )



        return TitlePlanner.clean_list(
            filtered
        )[:5]

    # =====================================================
    # 标题高价值属性
    # =====================================================

    @staticmethod
    def get_title_attributes(
        product_knowledge
    ):

        title_information = (
            product_knowledge.get(
                "title_information",
                {}
            )
        )


        result = []


        # 1. 数量
        quantity = title_information.get(
            "important_quantity",
            ""
        )
        
        if quantity:
        
            result.append(
                quantity
            )
        
        
        # 2. 产品使用场景/搜索上下文
        specifications = title_information.get(
            "important_specifications",
            []
        )
        
        
        for item in specifications:
        
            text = str(item).lower()
        
        
            if any(
                word in text
                for word in [
                    "diameter",
                    "width",
                    "length",
                    "height",
                    "dimension",
                    "size"
                ]
            ):
                continue
        
        
            result.append(item)
        
        # 3. 高价值属性
        result.extend(
            title_information.get(
                "priority_attributes",
                []
            )
        )
        
    
    # =====================================================
    # 判断是否适合作为标题卖点
    # 通用逻辑，不写死产品
    # =====================================================

    @staticmethod
    def is_title_feature(
        text: str
    ):

        value = text.lower()



        # 太短的信息通常价值低

        if len(value) < 3:

            return False



        # 过于泛化的动作描述

        generic_actions = [

            "function",

            "use",

            "using",

            "operation",

            "works",

            "working",

        ]


        for word in generic_actions:

            if word in value:

                return False



        # 过于营销化描述

        marketing_patterns = [

            "high quality",

            "premium",

            "durable construction",

            "easy to install",

            "cost effective",

        ]


        for word in marketing_patterns:

            if word in value:

                return False



        return True




    @staticmethod
    def is_low_value_keyword(
        text: str
    ):

        value = text.lower()


        low_value_patterns = [

            "best",

            "premium",

            "quality",

            "replacement part",

            "repair solution",

        ]


        for word in low_value_patterns:

            if word in value:

                return True


        return False
    # =====================================================
    # 兼容信息
    # =====================================================

    @staticmethod
    def get_compatibility(
        relationship
    ):

        brands = relationship.get(
            "brands",
            []
        )


        if not isinstance(
            brands,
            list
        ):

            return []



        result = []


        for brand in brands:


            brand = str(
                brand
            ).strip()


            if not brand:
                continue


            result.append(
                "Compatible with "
                +
                brand
            )


            # 亚马逊标题一般只保留一个主要兼容品牌

            break



        return result




    # =====================================================
    # 标题避免词
    # =====================================================

    @staticmethod
    def get_avoid_terms(
        identity
    ):

        avoid = []


        category = identity.get(
            "category",
            ""
        )


        product_type = identity.get(
            "product_type",
            ""
        )


        candidates = [

            category,

            product_type,

        ]


        for item in candidates:


            if not item:
                continue


            text = str(
                item
            ).strip()


            if TitlePlanner.is_category_term(
                text
            ):

                avoid.append(
                    text
                )



        return TitlePlanner.clean_list(
            avoid
        )




    # =====================================================
    # 判断是否属于低价值分类词
    # 不针对具体类目
    # =====================================================

    @staticmethod
    def is_category_term(
        text: str
    ):

        value = text.lower()



        category_patterns = [

            "parts",

            "accessories",

            "component",

            "replacement",

            "supplies",

            "appliance",

            "personal care",

        ]


        for pattern in category_patterns:


            if pattern in value:

                return True



        return False
    # =====================================================
    # 通用列表清理
    # =====================================================

    @staticmethod
    def clean_list(
        values
    ):

        result = []

        seen = set()



        if not isinstance(
            values,
            list
        ):

            return result



        for value in values:


            text = str(
                value
            ).strip()


            if not text:

                continue



            key = text.lower()



            if key in seen:

                continue



            seen.add(
                key
            )


            result.append(
                text
            )



        return result

from __future__ import annotations

from typing import Dict, Any


class ProductCoreBuilder:
    """
    Product Core Object Builder

    作用：
    将 Product Profile 转换成统一产品事实层。

    注意：
    - 不调用 AI
    - 不生成营销文案
    - 不猜测信息
    - 只整理已有事实
    """



    @staticmethod
    def build(profile: Dict[str, Any]) -> Dict[str, Any]:


        basic_info = profile.get(
            "basic_info",
            {}
        )


        compatibility = profile.get(
            "compatibility",
            {}
        )


        facts = profile.get(
            "facts",
            {}
        )


        if not isinstance(
            basic_info,
            dict
        ):

            basic_info = {}


        if not isinstance(
            compatibility,
            dict
        ):

            compatibility = {}


        if not isinstance(
            facts,
            dict
        ):

            facts = {}



        # =====================
        # Identity
        # =====================

        identity = {

            "product_name":
            ProductCoreBuilder.clean(
                basic_info.get(
                    "product_name",
                    ""
                )
            ),


            "product_type":
            ProductCoreBuilder.clean(
                basic_info.get(
                    "product_type",
                    ""
                )
            ),


            "category":
            ProductCoreBuilder.clean(
                basic_info.get(
                    "category",
                    ""
                )
            )

        }



        # =====================
        # Function
        # =====================

        primary_function = (

            basic_info.get(
                "main_function",
                ""

            )

            or

            basic_info.get(
                "core_function",
                ""
            )

        )


        function = {

            "primary_function":
            ProductCoreBuilder.clean(
                primary_function
            ),


            "problem_solved":
            ProductCoreBuilder.clean(
                profile.get(
                    "problem_solved",
                    ""
                )
            )

        }



        # =====================
        # Compatibility
        # =====================

        brands = compatibility.get(
            "brands",
            []
        )


        models = (

            compatibility.get(
                "models",
                []
            )

            or

            compatibility.get(
                "compatible_models",
                []
            )

        )


        compatibility_core = {


            "relationship":
            ProductCoreBuilder.clean(
                compatibility.get(
                    "relationship",
                    "Compatible replacement"
                )
            ),


            "brands":
            ProductCoreBuilder.clean_list(
                brands
            ),


            "models":
            ProductCoreBuilder.clean_list(
                models
            )

        }



        # =====================
        # Specifications
        # =====================

        specifications = {

            "material":
            ProductCoreBuilder.extract_value(
                facts.get(
                    "material",
                    ""
                )
            ),


            "color":
            ProductCoreBuilder.extract_value(
                facts.get(
                    "color",
                    ""
                )
            ),


            "quantity":
            ProductCoreBuilder.extract_value(
                facts.get(
                    "quantity",
                    ""
                )
            ),


            "dimensions":
            ProductCoreBuilder.extract_value(
                facts.get(
                    "dimensions",
                    ""
                )
            )

        }



        # =====================
        # Features
        # =====================

        features = {

            "features":
            profile.get(
                "features",
                []
            )

        }



        return {


            "identity":
            identity,


            "function":
            function,


            "compatibility":
            compatibility_core,


            "specifications":
            specifications,


            "features":
            features,


            "source":
            {
                "generated_from":
                "Product Profile"
            }

        }



    @staticmethod
    def clean(value):

        if not value:

            return ""

        return str(value).strip()



    @staticmethod
    def clean_list(values):

        if not values:

            return []


        if isinstance(
            values,
            str
        ):

            return [
                values.strip()
            ]


        return [

            str(v).strip()

            for v in values

            if v

        ]



    @staticmethod
    def extract_value(value):

        if isinstance(
            value,
            dict
        ):

            return str(
                value.get(
                    "value",
                    ""
                )
            ).strip()


        if isinstance(
            value,
            list
        ):

            return ", ".join(
                [
                    str(x)
                    for x in value
                    if x
                ]
            )


        return str(value).strip()

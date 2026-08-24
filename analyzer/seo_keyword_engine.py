from __future__ import annotations

from typing import Any


class SEOKeywordEngine:


    BLOCKED_WORDS = [
        "best",
        "premium",
        "original",
        "genuine",
        "official",
        "authentic",
        "cheap",
        "sale",
        "discount",
        "oem",
        "#1",
    ]


    BRAND_BLACKLIST = [
        "lg",
        "dyson",
        "samsung",
        "apple",
        "bawldy",
        "tineco",
    ]


    @staticmethod
    def clean_keyword(keyword: str):

        if not keyword:
            return ""

        text = str(keyword).strip()

        lower = text.lower()


        for word in SEOKeywordEngine.BLOCKED_WORDS:

            if word in lower:
                return ""


        return text



    @staticmethod
    def unique(items):

        result = []

        for item in items:

            item = SEOKeywordEngine.clean_keyword(item)


            if item and item.lower() not in [
                x.lower() for x in result
            ]:

                result.append(item)


        return result



    @staticmethod
    def remove_brands(items):

        result = []


        for item in items:

            lower = item.lower()

            has_brand = False


            for brand in SEOKeywordEngine.BRAND_BLACKLIST:

                if brand in lower:

                    has_brand = True
                    break


            if not has_brand:

                result.append(item)


        return result



    @staticmethod
    def detect_intent(product_type):

        text = product_type.lower()


        if any(
            x in text
            for x in [
                "part",
                "button",
                "switch",
                "replacement",
                "component"
            ]
        ):

            return "replacement part"



        if any(
            x in text
            for x in [
                "filter",
                "blade",
                "cartridge",
                "consumable"
            ]
        ):

            return "replacement consumable"



        return "replacement accessory"




    @staticmethod
    def generate(profile: dict[str, Any]):


        basic = profile.get(
            "basic_info",
            {}
        )


        compatibility = profile.get(
            "compatibility",
            {}
        )


        seo_intent = profile.get(
            "seo_intent",
            {}
        )



        primary = seo_intent.get(
            "primary_search",
            []
        ) or []



        primary_keyword = (
            primary[0]
            if primary
            else ""
        )



        product_type = str(
            basic.get(
                "product_type",
                ""
            )
        )



        secondary = []


        feature_keywords = []



        if primary_keyword:


            secondary.extend(
                [
                    f"{primary_keyword} replacement",
                    f"{primary_keyword} replacement part",
                ]
            )


            text = primary_keyword.lower()



            # button 产品
            if "start button" in text:

                secondary.extend(
                    [
                        "start button replacement",
                        "washer start button",
                    ]
                )



            # filter 产品

            if "filter" in text:

                secondary.extend(
                    [
                        "replacement filter",
                        "filter replacement part",
                        "filter cartridge replacement",
                    ]
                )



            # blade 产品

            if "blade" in text or "head" in text:

                secondary.extend(
                    [
                        "replacement blade",
                        "replacement head",
                    ]
                )



        # -----------------------
        # 功能关键词提取
        # -----------------------

        attributes = profile.get(
            "attributes",
            {}
        )


        for key,value in attributes.items():

            if value:

                feature_keywords.append(
                    str(value)
                )



        # -----------------------
        # 型号关键词
        # -----------------------

        models = compatibility.get(
            "models",
            []
        ) or []


        model_keywords = []


        for model in models[:10]:

            model_keywords.append(
                f"{model} replacement"
            )



        # -----------------------
        # 后台搜索词
        # -----------------------

        backend = []


        backend.extend(
            secondary
        )


        backend.extend(
            [
                "replacement component",
                "appliance replacement part",
            ]
        )




        backend = SEOKeywordEngine.remove_brands(
            backend
        )



        return {


            "primary_keywords":
            SEOKeywordEngine.unique(
                primary
            ),



            "secondary_keywords":
            SEOKeywordEngine.unique(
                secondary
            ),



            "model_keywords":
            SEOKeywordEngine.unique(
                model_keywords
            ),



            "feature_keywords":
            SEOKeywordEngine.unique(
                feature_keywords
            ),



            "backend_search_terms":
            SEOKeywordEngine.unique(
                backend
            ),



            "search_intent":
            SEOKeywordEngine.detect_intent(
                product_type
            )

        }

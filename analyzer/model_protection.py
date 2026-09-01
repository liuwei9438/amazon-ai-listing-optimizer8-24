from __future__ import annotations

import re


class ModelProtection:


    """
    Protect product model numbers.

    Rules:
    - Keep original model format
    - Restore AI modified models
    - Prevent lowercase/spacing changes
    """


    @staticmethod
    def extract_models(profile: dict):

        models = []


        compatibility = profile.get(
            "compatibility",
            {}
        )


        source_models = compatibility.get(
            "models",
            []
        )


        for model in source_models:

            if model:

                models.append(
                    str(model).strip()
                )


        return models



    @staticmethod
    def protect(text: str, models: list):


        if not text:

            return text



        result = text



        for model in models:


            pattern = re.compile(

                re.escape(model),

                flags=re.I

            )


            result = pattern.sub(

                model,

                result

            )


        return result



    @staticmethod
    def protect_result(result, models):


        if isinstance(result, str):

            return ModelProtection.protect(
                result,
                models
            )


        if isinstance(result, dict):

            new_result = {}


            for key,value in result.items():

                new_result[key] = (
                    ModelProtection.protect_result(
                        value,
                        models
                    )
                )


            return new_result



        if isinstance(result,list):

            return [

                ModelProtection.protect_result(
                    item,
                    models
                )

                for item in result

            ]


        return result

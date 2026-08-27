from __future__ import annotations

import json
from typing import Any

from services import (
    OpenAIResponsesClient,
    AIClientError,
)


class IdentifierClassificationError(RuntimeError):
    pass



class IdentifierClassifier:

    """
    Identifier Classifier V1.1 Stable

    作用:
    根据完整商品上下文，
    判断候选标识属于:

    - model_number
    - part_number
    - dimension
    - specification
    - quantity
    - unknown

    注意:
    不直接修改 Product Profile。
    只提供分类结果。
    """



    CLASSIFICATION_TYPES = [

        "model_number",

        "part_number",

        "dimension",

        "specification",

        "quantity",

        "unknown",

    ]



    SYSTEM_PROMPT = """
    You are a product identifier classification expert for an Amazon listing system.
    
    Your task is to classify candidate values according to their actual product meaning.
    
    Do not classify based only on appearance.
    Always analyze the complete product context:
    
    - product name
    - product type
    - main function
    - brand information
    - compatibility information
    - title
    - description
    
    
    Classify each candidate into exactly one category:
    
    model_number:
    
    A value that identifies a specific product model, device model, or compatible machine model.
    
    A valid model_number usually:
    - distinguishes one product model from another
    - is used to identify a product/device family
    - appears in product model or compatibility context
    
    
    part_number:
    
    A manufacturer code or replacement part identifier.
    
    
    dimension:
    
    A measurement describing physical size.
    
    Examples:
    - length
    - width
    - height
    - diameter
    
    
    specification:
    
    A value describing product characteristics, functions, performance, or technical parameters.
    
    This includes:
    - product generation names
    - feature levels
    - protection ratings
    - capacity
    - power
    - voltage
    - runtime
    - speed
    - technical capabilities
    
    
    quantity:
    
    A count or package quantity.
    
    
    unknown:
    
    Use when the meaning cannot be safely determined.
    
    
    Decision rules:
    
    1. First ask:
    Does this value identify a specific product model?
    
    If yes:
    classify as model_number.
    
    
    2. If the value describes product capability, feature level, technical rating, or performance:
    
    Do NOT classify as model_number.
    
    Classify as specification.
    
    
    3. If the value appears in compatibility information:
    
    Consider whether it represents a compatible device model.
    
    Only classify as model_number when the context supports that interpretation.
    
    
    4. Do not classify these as models only because they contain numbers or letters.
    
    The meaning must come from product context.
    
    
    5. When uncertain:
    use unknown instead of guessing.
    
    
    Return JSON only.
    """

    RESPONSE_SCHEMA = {

        "type": "object",

        "additionalProperties": False,

        "properties": {

            "identifier_results": {

                "type": "array",

                "items": {

                    "type": "object",

                    "additionalProperties": False,

                    "properties": {

                        "value": {

                            "type": "string"

                        },


                        "type": {

                            "type": "string",

                            "enum": [
                                "model_number",
                                "part_number",
                                "dimension",
                                "specification",
                                "quantity",
                                "unknown",
                            ]

                        },


                        "confidence": {

                            "type": "number"

                        }

                    },

                    "required": [

                        "value",

                        "type",

                        "confidence"

                    ]

                }

            }

        },

        "required": [

            "identifier_results"

        ]

    }



    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1-mini"
    ):

        self.client = OpenAIResponsesClient(

            api_key=api_key,

            model=model,
            stage="identifier_classification",

        )



    def classify(
        self,
        product_context: dict[str, Any],
        candidates: list[str],
    ) -> dict[str, Any]:


        if not candidates:

            return {

                "identifier_results": []

            }



        payload = {

            "product_context":
                product_context,


            "candidates":
                candidates,

        }



        try:

            result = self.client.create_json(

                self.SYSTEM_PROMPT,

                json.dumps(

                    payload,

                    ensure_ascii=False,

                ),

                self.RESPONSE_SCHEMA,

            )


        except AIClientError as exc:

            raise IdentifierClassificationError(

                str(exc)

            ) from exc



        return self.normalize_result(
            result
        )



    @staticmethod
    def normalize_result(
        result: dict[str, Any]
    ) -> dict[str, Any]:


        if not isinstance(
            result,
            dict
        ):

            return {

                "identifier_results": []

            }



        items = result.get(

            "identifier_results",

            []

        )



        cleaned = []



        if not isinstance(
            items,
            list
        ):

            items = []



        for item in items:


            if not isinstance(
                item,
                dict
            ):

                continue



            value = str(

                item.get(

                    "value",

                    ""

                )

            ).strip()



            category = str(

                item.get(

                    "type",

                    "unknown"

                )

            ).lower()



            confidence = item.get(

                "confidence",

                0

            )



            if not value:

                continue



            if category not in IdentifierClassifier.CLASSIFICATION_TYPES:

                category = "unknown"



            cleaned.append(

                {

                    "value": value,

                    "type": category,

                    "confidence": confidence,

                }

            )



        return {

            "identifier_results": cleaned

        }
        

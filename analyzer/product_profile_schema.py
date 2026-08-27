from __future__ import annotations

from copy import deepcopy
from typing import Any


def _string() -> dict[str, Any]:
    return {
        "type": "string"
    }


def _string_list() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "string"
        }
    }


_SCHEMA: dict[str, Any] = {

    "type": "object",

    "additionalProperties": False,

    "properties": {

        "product_identity": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "name": _string(),
                
                "buyer_search_identity": _string(),

                "title_product_identity": _string(),

                "category": _string(),

                "parent_product": _string(),

                "context": _string_list(),

                "design_features": _string_list(),

                "functional_features": _string_list(),

                "usage_scenarios": _string_list(),

            },

            "required": [

                "name",

                "buyer_search_identity",

                "title_product_identity",

                "category",

                "parent_product",

                "context",

                "design_features",

                "functional_features",

                "usage_scenarios",

            ],
        },


        "identifiers": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "model_numbers": _string_list(),

                "part_numbers": _string_list(),

                "series_numbers": _string_list(),

                "unknown_codes": _string_list(),

            },

            "required": [

                "model_numbers",

                "part_numbers",

                "series_numbers",

                "unknown_codes",

            ],
        },


        "basic_info": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "product_name": _string(),

                "product_type": _string(),

                "category": _string(),

                "main_function": _string(),

                "quantity": _string(),

                "material": _string(),

                "color": _string(),

                "dimensions": _string(),

                "voltage": _string(),

                "power": _string(),

                "package_contents": _string_list(),

            },

            "required": [

                "product_name",

                "product_type",

                "category",

                "main_function",

                "quantity",

                "material",

                "color",

                "dimensions",

                "voltage",

                "power",

                "package_contents",

            ],
        },


        "title_information": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "priority_attributes": _string_list(),

                "important_specifications": _string_list(),

                "important_quantity": _string(),

                "important_context": _string_list(),

                "important_compatibility": _string_list(),

            },

            "required": [

                "priority_attributes",

                "important_specifications",

                "important_quantity",

                "important_context",

                "important_compatibility",

            ],

        },


        "brand_info": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "seller_brand": _string(),

                "third_party_brands": _string_list(),

                "detected_brands": _string_list(),

                "relationship": {

                    "type": "string",

                    "enum": [

                        "unbranded_compatible",

                        "own_brand",

                        "generic",

                        "high_risk_brand_usage",

                        "unknown",

                    ],
                },


                "risk_level": {

                    "type": "string",

                    "enum": [

                        "low",

                        "medium",

                        "high",

                    ],
                },


                "rewrite_strategy": {

                    "type": "string",

                    "enum": [

                        "compatible_with",

                        "own_brand",

                        "no_brand",

                    ],
                },

            },

            "required": [

                "seller_brand",

                "third_party_brands",

                "detected_brands",

                "relationship",

                "risk_level",

                "rewrite_strategy",

            ],
        },


        "compatibility": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "brands": _string_list(),

                "models": _string_list(),

                "part_numbers": _string_list(),

                "compatibility_notes": _string_list(),

            },

            "required": [

                "brands",

                "models",

                "part_numbers",

                "compatibility_notes",

            ],
        },
                "specifications": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "dimensions": _string_list(),

                "weight": _string_list(),

                "voltage": _string_list(),

                "power": _string_list(),

                "capacity": _string_list(),

            },

            "required": [

                "dimensions",

                "weight",

                "voltage",

                "power",

                "capacity",

            ],
        },


        "attributes": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "functions": _string_list(),

                "usage_scenarios": _string_list(),

                "factual_selling_points": _string_list(),

                "materials": _string_list(),

                "design_features": _string_list(),

                "functional_features": _string_list(),

                "specifications": _string_list(),

            },

            "required": [

                "functions",

                "usage_scenarios",

                "factual_selling_points",

                "materials",

                "design_features",

                "functional_features",

                "specifications",

            ],
        },


        "search_strategy": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "primary_model": _string(),

                "title_identifiers": _string_list(),

                "bullet_identifiers": _string_list(),

                "backend_identifiers": _string_list(),

            },

            "required": [

                "primary_model",

                "title_identifiers",

                "bullet_identifiers",

                "backend_identifiers",

            ],
        },


        "seo": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "main_keywords": _string_list(),

                "secondary_keywords": _string_list(),

                "search_intent": _string(),

            },

            "required": [

                "main_keywords",

                "secondary_keywords",

                "search_intent",

            ],
        },


        "compliance": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "risk_level": {

                    "type": "string",

                    "enum": [

                        "low",

                        "medium",

                        "high",

                    ],
                },


                "risk_reasons": _string_list(),

                "forbidden_or_risky_terms": _string_list(),

                "compatibility_wording_required": {

                    "type": "boolean"

                },

            },

            "required": [

                "risk_level",

                "risk_reasons",

                "forbidden_or_risky_terms",

                "compatibility_wording_required",

            ],
        },


        "fact_lock": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "quantity": _string(),

                "material": _string(),

                "color": _string(),

                "dimensions": _string(),

                "voltage": _string(),

                "power": _string(),

                "compatible_models": _string_list(),

                "part_numbers": _string_list(),

                "package_contents": _string_list(),

            },

            "required": [

                "quantity",

                "material",

                "color",

                "dimensions",

                "voltage",

                "power",

                "compatible_models",

                "part_numbers",

                "package_contents",

            ],
        },


        "source_identity": {

            "type": "object",

            "additionalProperties": False,

            "properties": {

                "sku": _string(),

                "parent_sku": _string(),

                "source_row_index": {

                    "type": "integer"

                },

            },

            "required": [

                "sku",

                "parent_sku",

                "source_row_index",

            ],
        },

    },
        "required": [

        "product_identity",
    
        "identifiers",
    
        "basic_info",
    
        "title_information",
    
        "brand_info",
    
        "compatibility",

        "specifications",

        "attributes",

        "search_strategy",

        "seo",

        "compliance",

        "fact_lock",

        "source_identity",

    ],

}


_EMPTY_PROFILE: dict[str, Any] = {


    "product_identity": {

        "name": "",

        "buyer_search_identity": "",

         "title_product_identity": "",

        "category": "",

        "parent_product": "",

        "context": [],

        "design_features": [],

        "functional_features": [],

        "usage_scenarios": [],

    },


    "identifiers": {

        "model_numbers": [],

        "part_numbers": [],

        "series_numbers": [],

        "unknown_codes": [],

    },


    "basic_info": {

        "product_name": "",

        "product_type": "",

        "category": "",

        "main_function": "",

        "quantity": "",

        "material": "",

        "color": "",

        "dimensions": "",

        "voltage": "",

        "power": "",

        "package_contents": [],

    },
    "title_information": {
    
        "priority_attributes": [],
    
        "important_specifications": [],
    
        "important_quantity": "",
    
        "important_context": [],
    
        "important_compatibility": [],
    
    },

    "brand_info": {

        "seller_brand": "",

        "third_party_brands": [],

        "detected_brands": [],

        "relationship": "unknown",

        "risk_level": "low",

        "rewrite_strategy": "no_brand",

    },


    "compatibility": {

        "brands": [],

        "models": [],

        "part_numbers": [],

        "compatibility_notes": [],

    },


    "specifications": {

        "dimensions": [],

        "weight": [],

        "voltage": [],

        "power": [],

        "capacity": [],

    },


    "attributes": {

        "functions": [],

        "usage_scenarios": [],

        "factual_selling_points": [],

        "materials": [],

        "design_features": [],

        "functional_features": [],

        "specifications": [],

    },


    "search_strategy": {

        "primary_model": "",

        "title_identifiers": [],

        "bullet_identifiers": [],

        "backend_identifiers": [],

    },


    "seo": {

        "main_keywords": [],

        "secondary_keywords": [],

        "search_intent": "",

    },


    "compliance": {

        "risk_level": "low",

        "risk_reasons": [],

        "forbidden_or_risky_terms": [],

        "compatibility_wording_required": False,

    },


    "fact_lock": {

        "quantity": "",

        "material": "",

        "color": "",

        "dimensions": "",

        "voltage": "",

        "power": "",

        "compatible_models": [],

        "part_numbers": [],

        "package_contents": [],

    },


    "source_identity": {

        "sku": "",

        "parent_sku": "",

        "source_row_index": 0,

    },

}



def _validate_strict_object_schema(
    node: Any,
    path: str = "$",
) -> None:

    if isinstance(node, dict):

        if node.get("type") == "object":

            if node.get("additionalProperties") is not False:

                raise ValueError(
                    f"{path}: every object must set additionalProperties to False"
                )


            properties = node.get("properties")

            required = node.get("required")


            if not isinstance(properties, dict):

                raise ValueError(
                    f"{path}: object properties must be a dictionary"
                )


            if (
                not isinstance(required, list)
                or set(required) != set(properties)
            ):

                raise ValueError(
                    f"{path}: required must contain every declared property"
                )


        for key, value in node.items():

            _validate_strict_object_schema(
                value,
                f"{path}.{key}"
            )


    elif isinstance(node, list):

        for index, value in enumerate(node):

            _validate_strict_object_schema(
                value,
                f"{path}[{index}]"
            )



_validate_strict_object_schema(_SCHEMA)



def json_schema() -> dict[str, Any]:

    """
    Return a fresh strict schema for OpenAI Structured Outputs.
    """

    return deepcopy(_SCHEMA)



def empty_profile() -> dict[str, Any]:

    """
    Return a fresh, complete Product Profile with safe default values.
    """

    return deepcopy(_EMPTY_PROFILE)

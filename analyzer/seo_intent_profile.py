from __future__ import annotations

from copy import deepcopy
from typing import Any


def _string() -> dict[str, Any]:
    return {"type": "string"}


def _string_list() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _weighted_keyword() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "keyword": _string(),
            "weight": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["keyword", "weight"],
    }


def _search_scenario() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scenario": _string(),
            "queries": _string_list(),
        },
        "required": ["scenario", "queries"],
    }


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "primary_search": _string_list(),
        "secondary_search": _string_list(),
        "purchase_intent": _string_list(),
        "search_entities": _string_list(),
        "search_scenarios": {
            "type": "array",
            "items": _search_scenario(),
        },
        "negative_keywords": _string_list(),
        "weighted_keywords": {
            "type": "array",
            "items": _weighted_keyword(),
        },
    },
    "required": [
        "primary_search",
        "secondary_search",
        "purchase_intent",
        "search_entities",
        "search_scenarios",
        "negative_keywords",
        "weighted_keywords",
    ],
}


_EMPTY_PROFILE: dict[str, Any] = {
    "primary_search": [],
    "secondary_search": [],
    "purchase_intent": [],
    "search_entities": [],
    "search_scenarios": [],
    "negative_keywords": [],
    "weighted_keywords": [],
}


def _validate_strict_object_schema(node: Any, path: str = "$") -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                raise ValueError(
                    f"{path}: every object must set additionalProperties to False"
                )

            properties = node.get("properties")
            required = node.get("required")

            if not isinstance(properties, dict):
                raise ValueError(f"{path}: object properties must be a dictionary")

            if not isinstance(required, list) or set(required) != set(properties):
                raise ValueError(
                    f"{path}: required must contain every declared property"
                )

        for key, value in node.items():
            _validate_strict_object_schema(value, f"{path}.{key}")

    elif isinstance(node, list):
        for index, value in enumerate(node):
            _validate_strict_object_schema(value, f"{path}[{index}]")


_validate_strict_object_schema(_SCHEMA)


def json_schema() -> dict[str, Any]:
    """Return a fresh strict schema for the SEO Intent Profile."""
    return deepcopy(_SCHEMA)


def empty_profile() -> dict[str, Any]:
    """Return a fresh empty SEO Intent Profile."""
    return deepcopy(_EMPTY_PROFILE)

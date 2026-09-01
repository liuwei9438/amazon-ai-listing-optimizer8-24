from __future__ import annotations

from typing import Any
import re


VALID_COLORS = {
    "black", "white", "silver", "gray", "grey",
    "red", "blue", "green", "yellow",
    "orange", "pink", "purple", "gold",
    "brown", "beige",
}


VALID_MATERIALS = {
    "abs": "ABS",
    "pp": "PP",
    "pvc": "PVC",
    "aluminum": "Aluminum",
    "aluminium": "Aluminum",
    "metal": "Metal",
    "rubber": "Rubber",
    "silicone": "Silicone",
    "glass": "Glass",
}



# =========================
# 基础字段
# =========================


def validate_color(
    attributes: dict[str, Any]
) -> dict[str, Any]:

    colors = attributes.get(
        "color",
        []
    )


    if not isinstance(
        colors,
        list
    ):

        colors = []


    cleaned = []


    for color in colors:

        if not isinstance(
            color,
            str
        ):

            continue


        value = color.strip().lower()


        if value in VALID_COLORS:

            cleaned.append(
                color.strip().capitalize()
            )


    attributes["color"] = list(
        dict.fromkeys(
            cleaned
        )
    )


    return attributes



def validate_material(
    attributes: dict[str, Any]
) -> dict[str, Any]:

    materials = attributes.get(
        "material",
        []
    )


    if not isinstance(
        materials,
        list
    ):

        materials = []


    cleaned = []


    for material in materials:

        if not isinstance(
            material,
            str
        ):

            continue


        value = material.strip().lower()


        if value in VALID_MATERIALS:

            cleaned.append(
                VALID_MATERIALS[value]
            )


    attributes["material"] = list(
        dict.fromkeys(
            cleaned
        )
    )


    return attributes



# =========================
# 型号过滤
# =========================


def looks_like_dimension(
    value: str
) -> bool:

    text = value.lower().strip()


    patterns = [

        r"^\d+(\.\d+)?cm$",

        r"^\d+(\.\d+)?mm$",

        r"^\d+\s*x\s*\d+",

        r"^\d+/\d+$",

    ]


    for pattern in patterns:

        if re.match(
            pattern,
            text
        ):

            return True


    return False



def looks_like_unit_value(
    value: str
) -> bool:

    return bool(
        re.match(
            r"^\d+(\.\d+)?(v|w|cm|mm|kg|g|ml|l)$",
            value.lower()
        )
    )



def looks_like_model(
    value: str
) -> bool:

    text = value.strip()


    if not text:

        return False


    # 纯数字不是型号

    if text.isdigit():

        return False



    if looks_like_dimension(
        text
    ):

        return False



    if looks_like_unit_value(
        text
    ):

        return False



    # 至少包含数字和字母

    has_letter = bool(
        re.search(
            r"[A-Za-z]",
            text
        )
    )


    has_number = bool(
        re.search(
            r"\d",
            text
        )
    )


    return (
        has_letter
        and
        has_number
    )



def validate_models(
    compatibility: dict[str, Any]
) -> dict[str, Any]:

    models = compatibility.get(
        "models",
        []
    )


    if not isinstance(
        models,
        list
    ):

        models = []


    cleaned = []


    for model in models:

        if not isinstance(
            model,
            str
        ):

            continue


        if looks_like_model(
            model
        ):

            cleaned.append(
                model.strip()
            )


    compatibility["models"] = list(
        dict.fromkeys(
            cleaned
        )
    )


    return compatibility



def validate_part_numbers(
    compatibility: dict[str, Any]
) -> dict[str, Any]:

    numbers = compatibility.get(
        "part_numbers",
        []
    )


    if not isinstance(
        numbers,
        list
    ):

        numbers = []


    cleaned = []


    for number in numbers:

        if not isinstance(
            number,
            str
        ):

            continue


        text = number.strip()


        if (
            len(text) >= 3
            and
            not text.isdigit()
        ):

            cleaned.append(
                text
            )


    compatibility["part_numbers"] = list(
        dict.fromkeys(
            cleaned
        )
    )


    return compatibility



# =========================
# 总入口
# =========================


def validate_attributes(
    attributes: dict[str, Any]
) -> dict[str, Any]:

    attributes = validate_color(
        attributes
    )


    attributes = validate_material(
        attributes
    )


    return attributes



def validate_compatibility(
    compatibility: dict[str, Any]
) -> dict[str, Any]:

    compatibility = validate_models(
        compatibility
    )


    compatibility = validate_part_numbers(
        compatibility
    )


    return compatibility

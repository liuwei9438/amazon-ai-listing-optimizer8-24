from __future__ import annotations

import re
from typing import Any

# FACT_LOCK_VERSION = "v2.2-conservative-numeric-model-context"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"\s*\|\s*|\n+|;\s*", str(value))

    output: list[str] = []
    seen: set[str] = set()

    for item in values:
        text = _clean(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            output.append(text)

    return output


def _source_text(record: Any) -> str:
    values: list[str] = []

    for name in (
        "title",
        "description",
        "details",
        "color",
        "variant",
        "quantity",
        "material",
        "dimensions",
        "voltage",
        "power",
    ):
        values.append(_clean(getattr(record, name, "")))

    values.extend(_list(getattr(record, "bullets", [])))
    return " ".join(x for x in values if x)


def _first_supported(record: Any, *names: str) -> str:
    source = _source_text(record).casefold()

    for name in names:
        value = _clean(getattr(record, name, ""))
        if value and value.casefold() in source:
            return value

    return ""


def _is_obvious_non_model(value: str) -> bool:
    v = _clean(value)
    if not v:
        return True

    low = v.casefold()

    if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:pcs?|pieces?|sets?|packs?|pairs?)", low):
        return True

    if re.fullmatch(
        r"\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?){0,3}\s*"
        r"(?:mm|cm|m|in|inch|inches|ft)?",
        low,
    ):
        if re.search(r"[x×]|(?:mm|cm|m|in|inch|inches|ft)$", low):
            return True

    if re.fullmatch(
        r"\d+(?:\.\d+)?\s*"
        r"(?:v|w|kw|mw|a|ma|hz|khz|mhz|ghz|mah|ah|wh|kwh|"
        r"g|kg|mg|lb|lbs|oz|ml|l|bar|psi|pa|kpa|mpa|rpm|°c|°f)",
        low,
    ):
        return True

    if re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?\s*k(?:\s*ohms?)?", low):
        return True

    if re.fullmatch(r"\d+\s*[- ]?(?:wire|wires|conductor|conductors)", low):
        return True

    if re.fullmatch(
        r"\d+\s*[- ]?(?:pin|pins|pole|poles|speed|speeds|stage|stages|"
        r"gear|gears|blade|blades|tooth|teeth)",
        low,
    ):
        return True

    if re.fullmatch(r"(?:19|20)\d{2}", low):
        return True

    # Decimal measurements / ratios / scales: 1.9, 2.0, 0.124
    # Dotted multi-part codes such as 5J.JEE05.001 are NOT matched here.
    if re.fullmatch(r"\d+\.\d+", low):
        return True

    # Pure numeric fractions/scales: 1/4, 1/10, 1/14, 24/7.
    # Mixed identifiers such as R3E-5/12 remain eligible.
    if re.fullmatch(r"\d+/\d+\+?", low):
        return True

    # Numbered prose fragments from bullets/details: 6.Technical, 2.Please
    if re.fullmatch(r"\d+\.[a-z]{3,}", low):
        return True

    # Age / year-like configuration fragments: 14+y, 3+years
    if re.fullmatch(r"\d+\+?(?:y|yr|yrs|year|years)", low):
        return True

    # Ranged or compact measurable values not covered by the simple unit rule.
    if re.fullmatch(
        r"(?:to)?\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?"
        r"(?:mm|cm|m|in|inch|inches|ft|v|w|kw|a|ma|hz|"
        r"kg|g|gr|lb|lbs|oz|ml|l|rpm|sec|secs|second|seconds|"
        r"degc|degf|°c|°f)",
        low,
    ):
        return True

    if re.fullmatch(
        r"x\d+(?:\.\d+)?(?:mm|cm|m|in|inch|inches)?",
        low,
    ):
        return True

    # Rates/capacities such as 2000l/min.
    if re.fullmatch(
        r"\d+(?:\.\d+)?(?:ml|l|g|kg|m|cm|mm)/(?:min|h|hr|s|sec)",
        low,
    ):
        return True

    # Configuration descriptors such as 2-stroke.
    if re.fullmatch(
        r"\d+\s*[- ]?(?:stroke|strokes|phase|phases|channel|channels)",
        low,
    ):
        return True

    # Slash-joined prose/configuration fragments are not stable identifiers.
    # True slash identifiers are typically compact code-like segments.
    if "/" in v:
        segments = [seg for seg in v.split("/") if seg]
        lexical_segments = [
            seg for seg in segments
            if re.fullmatch(r"[A-Za-z][A-Za-z-]{3,}", seg)
        ]
        if len(segments) >= 2 and lexical_segments:
            return True

    return False


def _numeric_model_context(text: str, value: str, start: int, end: int) -> bool:
    """
    Conservative pure-numeric model protection.

    Deterministic Fact Lock must avoid treating arbitrary numbers as models.
    Numeric values are accepted only when there is strong local model-list
    grammar or when they are long part-number-like codes.
    """
    if not value.isdigit():
        return False

    # 1-2 digit numeric tokens are too ambiguous for deterministic locking.
    # If they are true models, AI/structured compatibility can still preserve
    # them without poisoning the hard fact lock.
    if len(value) <= 2:
        return False

    # Long numeric codes are commonly part numbers and are high confidence.
    if len(value) >= 7:
        return True

    left = text[max(0, start - 100):start]
    right = text[end:min(len(text), end + 100)]
    left_fold = left.casefold()
    context = f"{left} {right}".casefold()

    # Explicit model grammar.
    if re.search(
        r"\b(?:model|models|compatible\s+with|suitable\s+for|"
        r"fit\s+for|fits|replacement\s+for)\b",
        context,
    ):
        return True

    # Device-specific compatibility grammar used in real marketplace titles.
    if re.search(
        r"\bfor\s+(?:chainsaw|mower|projector|printer|scooter|vacuum|"
        r"machine|equipment)\b",
        context,
    ):
        return True

    # Local "for <numeric model list>" grammar.
    #
    # Examples:
    #   for 14210 16210
    #   for Canon iR 2520 2525 2530
    #
    # We intentionally inspect only the nearest "for" clause and require the
    # material between "for" and the current number to be compact model/brand
    # tokens rather than arbitrary prose.
    for_positions = [
        m.start()
        for m in re.finditer(r"\bfor\b", left_fold)
    ]

    if for_positions:
        nearest = for_positions[-1]
        tail = left[nearest + 3:].strip()

        if len(tail) <= 60:
            tokens = tail.split()

            compact_token = re.compile(
                r"^(?:[A-Za-z][A-Za-z0-9._/+\\-]*|\d{3,6})$"
            )

            if all(compact_token.fullmatch(token) for token in tokens):
                return True

    # Strong numeric cluster only when its own nearby clause has compatibility
    # grammar.  Do not look arbitrarily far back through ordinary prose.
    local = text[max(0, start - 55):min(len(text), end + 55)]
    nums = re.findall(r"(?<![\w.])\d{3,6}(?![\w.])", local)

    if len(nums) >= 3 and re.search(
        r"\b(?:for|models?|compatible|suitable|fits?)\b",
        local,
        flags=re.IGNORECASE,
    ):
        return True

    return False


def _extract_models(text: str) -> list[str]:
    source = _clean(text)
    if not source:
        return []

    pattern = re.compile(
        r"(?<![A-Za-z0-9])"
        r"(?=[A-Za-z0-9._/+\-]{2,40}(?![A-Za-z0-9._/+\-]))"
        r"(?=[A-Za-z0-9._/+\-]*\d)"
        r"[A-Za-z0-9]+(?:[._/+\-][A-Za-z0-9]+)*"
        r"(?![A-Za-z0-9])"
    )

    result: list[str] = []
    seen: set[str] = set()

    for match in pattern.finditer(source):
        value = match.group(0).strip(".,;:")
        if not value:
            continue

        if _is_obvious_non_model(value):
            continue

        # Some measurements are split by punctuation that is not part of the
        # identifier token itself (100°, 130*30*100mm).  Inspect immediate
        # source context before numeric model classification.
        before = source[max(0, match.start() - 12):match.start()]
        after = source[match.end():min(len(source), match.end() + 16)]

        if value.isdigit():
            if (
                re.match(
                    r"^\s*°",
                    after,
                    flags=re.IGNORECASE,
                )
                or
                re.match(
                    r"^\s*(?:deg(?:c|f)?|mm|cm|m|in|inch|v|w|kg|g|gr|lb|"
                    r"oz|rpm|sec|seconds?)\b",
                    after,
                    flags=re.IGNORECASE,
                )
            ):
                continue

            if (
                re.search(r"(?:\d\s*[x×*]\s*)$", before)
                or
                re.match(r"^\s*[x×*]\s*\d", after)
            ):
                continue

        # ISO paper-size tokens are specifications in printing context, not
        # machine models (A3/A4/etc.).
        if re.fullmatch(r"A\d{1,2}", value, flags=re.IGNORECASE):
            nearby = (
                source[max(0, match.start() - 40):
                       min(len(source), match.end() + 40)]
                .casefold()
            )
            if re.search(
                r"\b(?:printer|printing|print|paper|mother\s*board|"
                r"interface\s*board|paper\s*size)\b",
                nearby,
            ):
                continue

        if value.isdigit() and not _numeric_model_context(
            source,
            value,
            match.start(),
            match.end(),
        ):
            continue

        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)

    return result


def build_fact_lock(record: Any) -> dict[str, Any]:
    source = _source_text(record)
    source_cf = source.casefold()

    package_contents = _list(getattr(record, "package_contents", []))
    part_numbers = _list(getattr(record, "part_numbers", []))

    explicit_models = _list(
        getattr(record, "compatible_models", [])
        or getattr(record, "models", [])
    )

    explicit_models = [
        value
        for value in explicit_models
        if value.casefold() in source_cf and not _is_obvious_non_model(value)
    ]

    # Deterministic auto-detection is intentionally title-scoped.
    # Descriptions/bullets contain measurements, numbered prose and service
    # phrases that look code-like; locking those as models creates irreversible
    # downstream pollution because fact_lock overrides AI output.
    title_source = _clean(getattr(record, "title", ""))
    detected_models = _extract_models(title_source)

    models: list[str] = []
    seen: set[str] = set()

    for value in [*explicit_models, *detected_models]:
        key = value.casefold()
        if value and value.casefold() in source_cf and key not in seen:
            seen.add(key)
            models.append(value)

    return {
        "quantity": _first_supported(record, "quantity"),
        "material": _first_supported(record, "material"),
        "color": _first_supported(record, "color"),
        "dimensions": _first_supported(record, "dimensions"),
        "voltage": _first_supported(record, "voltage"),
        "power": _first_supported(record, "power"),
        "compatible_models": models,
        "part_numbers": [
            value for value in part_numbers
            if value.casefold() in source_cf
        ],
        "package_contents": [
            value for value in package_contents
            if value.casefold() in source_cf
        ],
    }


def validate_fact_lock(
    profile: dict[str, Any],
    expected_lock: dict[str, Any],
) -> list[str]:
    actual = profile.get("fact_lock")
    if actual != expected_lock:
        return ["事实锁与原始数据不一致"]
    return []

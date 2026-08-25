from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from typing import Any


class SourceFactExtractor:
    """
    Source Fact Preservation V1.0

    Goal:
    Preserve high-value facts from the raw collected listing BEFORE any AI
    interpretation can drop them.

    This module does NOT decide what belongs in the final title.
    It only creates a durable source-evidence ledger.

    Facts may later be:
    - selected for title
    - moved to bullets/backend
    - filtered for compliance
    - left unresolved for AI classification

    They must not silently disappear.
    """

    SCHEMA_VERSION = "2.0-source-fact-ledger-noise-firewall"

    MARKETING_TERMS = {
        "wholesale",
        "hot sale",
        "best seller",
        "best",
        "premium",
        "original",
        "genuine",
        "official",
        "authentic",
        "oem",
        "#1",
        "high quality",
        "high-quality",
        "good quality",
        "new",
    }

    LOW_VALUE_GENERIC_TERMS = {
        "spare parts",
        "replacement parts",
        "parts accessories",
        "accessories",
        "accessory",
    }

    _TOKEN_RE = re.compile(
        r"[A-Za-z0-9]+(?:[._/+*-][A-Za-z0-9]+)*"
    )

    _IDENTIFIER_RE = re.compile(
        r"^(?=.{3,32}$)(?=.*[A-Za-z])(?=.*\d)"
        r"[A-Za-z0-9][A-Za-z0-9._/+*-]*$"
    )

    _QUANTITY_RE = re.compile(
        r"\b(\d{1,4})\s*(?:pcs?|pieces?|piece|set|sets)\b",
        flags=re.IGNORECASE,
    )

    _DIMENSION_RE = re.compile(
        r"\b\d+(?:\.\d+)?\s*[xX*×]\s*"
        r"\d+(?:\.\d+)?"
        r"(?:\s*[xX*×]\s*\d+(?:\.\d+)?)?"
        r"\s*(?:mm|cm|m|inch|in)?\b",
        flags=re.IGNORECASE,
    )

    _SIMPLE_SPEC_RE = re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:mm|cm|inch|in|V|W|KW|MPa|bar|ohm|rpm)\b",
        flags=re.IGNORECASE,
    )

    _URL_RE = re.compile(
        r"https?://\S+",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def _record_dict(record: Any) -> dict[str, Any]:
        if is_dataclass(record):
            return asdict(record)

        if isinstance(record, dict):
            return dict(record)

        if hasattr(record, "__dict__"):
            return dict(vars(record))

        return {"value": str(record)}

    @staticmethod
    def _clean(value: Any) -> str:
        if value is None:
            return ""

        text = str(value)
        text = SourceFactExtractor._URL_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for value in values:
            value = SourceFactExtractor._clean(value)
            if not value:
                continue

            key = value.casefold()

            if key in seen:
                continue

            seen.add(key)
            result.append(value)

        return result

    @staticmethod
    def _marketing_terms_present(text: str) -> list[str]:
        folded = text.casefold()
        return [
            term
            for term in sorted(
                SourceFactExtractor.MARKETING_TERMS,
                key=len,
                reverse=True,
            )
            if term.casefold() in folded
        ]

    @staticmethod
    def _remove_marketing_terms(text: str) -> str:
        cleaned = SourceFactExtractor._clean(text)

        for term in sorted(
            SourceFactExtractor.MARKETING_TERMS,
            key=len,
            reverse=True,
        ):
            cleaned = re.sub(
                r"(?<![A-Za-z0-9])"
                + re.escape(term)
                + r"(?![A-Za-z0-9])",
                " ",
                cleaned,
                flags=re.IGNORECASE,
            )

        return re.sub(
            r"\s+",
            " ",
            cleaned,
        ).strip(" ,;|-")

    @staticmethod
    def _identifier_candidates(text: str) -> list[str]:
        candidates: list[str] = []

        for token in SourceFactExtractor._TOKEN_RE.findall(
            SourceFactExtractor._clean(text)
        ):
            if not SourceFactExtractor._IDENTIFIER_RE.match(token):
                continue

            # Do not classify obvious dimensions/specs as identifiers.
            if SourceFactExtractor._DIMENSION_RE.fullmatch(token):
                continue

            if SourceFactExtractor._SIMPLE_SPEC_RE.fullmatch(token):
                continue

            # Quantities such as 10PCS are not model identifiers.
            if re.fullmatch(
                r"\d+\s*(?:pcs?|pieces?|piece|sets?)",
                token,
                flags=re.IGNORECASE,
            ):
                continue

            # V2 Noise Firewall:
            # Do not preserve instruction numbering, measurement tolerances,
            # or obvious source-note fragments as model/part candidates.
            if re.fullmatch(
                r"\d+\.(?:please|the|technical|note|due)",
                token,
                flags=re.IGNORECASE,
            ):
                continue

            if re.fullmatch(
                r"\d+(?:[-–—]\d+)+(?:mm|cm|m|in|inch)?",
                token,
                flags=re.IGNORECASE,
            ):
                continue

            if re.fullmatch(
                r"\d+(?:\.\d+)?(?:mm|cm|m|in|inch)",
                token,
                flags=re.IGNORECASE,
            ):
                continue

            candidates.append(token)

        return SourceFactExtractor._unique(candidates)

    @staticmethod
    def _title_segments(title: str) -> list[str]:
        """
        Preserve compact source phrases without claiming their semantic type.

        Example:
        "10PCS for CCE016 Wholesale Conveyor Track Chain Pads for Marnak
         Woodworking Edgebanding Machine Spare Parts"

        yields evidence segments containing:
        - CCE016 Conveyor Track Chain Pads
        - Marnak Woodworking Edgebanding Machine Spare Parts

        AI Strategy can later decide whether a segment is:
        model, compatibility, context, secondary identity, or low value.
        """

        cleaned = SourceFactExtractor._remove_marketing_terms(title)

        cleaned = re.sub(
            r"\b\d{1,4}\s*(?:pcs?|pieces?|piece|sets?)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )

        chunks = re.split(
            r"\b(?:for|compatible with|fits?|fit for)\b",
            cleaned,
            flags=re.IGNORECASE,
        )

        results: list[str] = []

        for chunk in chunks:
            chunk = re.sub(
                r"\s+",
                " ",
                chunk,
            ).strip(" ,;|-")

            if len(chunk) < 3:
                continue

            results.append(chunk)

        return SourceFactExtractor._unique(results)

    @staticmethod
    def _for_phrases(title: str) -> list[str]:
        text = SourceFactExtractor._remove_marketing_terms(title)

        matches = re.findall(
            r"\bfor\s+(.+?)(?=\bfor\b|[,;|]|$)",
            text,
            flags=re.IGNORECASE,
        )

        return SourceFactExtractor._unique(
            [
                re.sub(
                    r"\s+",
                    " ",
                    match,
                ).strip(" ,;|-")
                for match in matches
            ]
        )

    @staticmethod
    def extract(record: Any) -> dict[str, Any]:
        source = SourceFactExtractor._record_dict(record)

        title = SourceFactExtractor._clean(
            source.get("title", "")
        )

        bullets_raw = source.get("bullets", [])

        if isinstance(bullets_raw, (tuple, list)):
            bullets = [
                SourceFactExtractor._clean(item)
                for item in bullets_raw
                if SourceFactExtractor._clean(item)
            ]
        elif bullets_raw:
            bullets = [
                SourceFactExtractor._clean(bullets_raw)
            ]
        else:
            bullets = []

        description = SourceFactExtractor._clean(
            source.get("description", "")
        )

        combined = " ".join(
            [
                title,
                *bullets,
                description,
            ]
        )

        quantities = [
            f"{match.group(1)}pcs"
            for match in SourceFactExtractor._QUANTITY_RE.finditer(
                combined
            )
        ]

        dimensions = SourceFactExtractor._unique(
            [
                match.group(0)
                for match in SourceFactExtractor._DIMENSION_RE.finditer(
                    combined
                )
            ]
        )

        specifications = SourceFactExtractor._unique(
            dimensions
            +
            [
                match.group(0)
                for match in SourceFactExtractor._SIMPLE_SPEC_RE.finditer(
                    combined
                )
            ]
        )

        identifiers_title = (
            SourceFactExtractor._identifier_candidates(
                title
            )
        )

        identifiers_supporting = (
            SourceFactExtractor._identifier_candidates(
                " ".join(
                    bullets
                    +
                    [description]
                )
            )
        )

        all_identifiers = SourceFactExtractor._unique(
            identifiers_title
            +
            identifiers_supporting
        )

        raw_data = source.get(
            "raw_data",
            {},
        )

        if not isinstance(raw_data, dict):
            raw_data = {}

        # Keep a compact snapshot for diagnostics and source-loss auditing.
        source_snapshot = {
            "row_number":
                source.get(
                    "row_number",
                    None,
                ),
            "sku":
                SourceFactExtractor._clean(
                    source.get(
                        "sku",
                        "",
                    )
                ),
            "title":
                title,
            "bullets":
                bullets,
            "description":
                description,
        }

        return {
            "schema_version":
                SourceFactExtractor.SCHEMA_VERSION,

            "source_snapshot":
                source_snapshot,

            "high_confidence": {
                "quantities":
                    SourceFactExtractor._unique(
                        quantities
                    ),
                "identifier_candidates":
                    all_identifiers,
                "identifier_candidates_from_title":
                    identifiers_title,
                "specifications":
                    specifications,
            },

            "source_evidence": {
                "title_segments":
                    SourceFactExtractor._title_segments(
                        title
                    ),
                "for_phrases":
                    SourceFactExtractor._for_phrases(
                        title
                    ),
                "marketing_terms_present":
                    SourceFactExtractor._marketing_terms_present(
                        title
                    ),
            },

            "raw_fields": {
                str(key):
                    SourceFactExtractor._clean(value)
                for key, value in raw_data.items()
                if SourceFactExtractor._clean(value)
            },
        }

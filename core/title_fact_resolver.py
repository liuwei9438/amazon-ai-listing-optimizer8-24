from __future__ import annotations
import re
from typing import Any

from core.representative_model_selector import RepresentativeModelSelector


class TitleFactResolver:
    """Stable Title Pipeline V1.0: source-backed fact resolver only."""

    VERSION = "stable-v1.9-representative-model-ranking"
    STRICT_TYPES = {
        "MODEL", "PART_NUMBER", "COMPATIBILITY_MODEL",
        "COMPATIBILITY_BRAND", "SPECIFICATION",
    }
    MARKETING = {
        "wholesale", "best seller", "#1", "premium", "original",
        "genuine", "official", "authentic", "oem", "hot sale",
        "high quality", "high-quality",
    }
    NOISE = [
        r"\bplease\s+allow\b",
        r"\bmeasurement\s+(?:allowed\s+)?error\b",
        r"\bmanual\s+measurement\b",
        r"\bdue\s+to\s+manual\b",
        r"\bslightly\s+different\b",
        r"\bmainland\s+china\b",
        r"\b\d+\.(?:please|the|technical|note)\b",
    ]

    @staticmethod
    def _clean(v: Any) -> str:
        return re.sub(r"\s+", " ", str(v or "")).strip()

    @staticmethod
    def _flatten(v: Any) -> list[str]:
        out = []
        if isinstance(v, str):
            t = TitleFactResolver._clean(v)
            if t:
                out.append(t)
        elif isinstance(v, dict):
            for x in v.values():
                out.extend(TitleFactResolver._flatten(x))
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                out.extend(TitleFactResolver._flatten(x))
        return out

    @staticmethod
    def _brand_traceable(
        brand: str,
        source_title: str,
    ) -> bool:
        """
        Conservative brand traceability with punctuation tolerance.

        Allowed equivalence:
        - Can-on <-> Canon
        - TR-AXXAS <-> TRAXXAS
        - whitespace / punctuation differences

        Multi-token brands may also contain source connector "for" between
        brand tokens (e.g. "KONICA for MINOLTA").

        This does not use external brand knowledge and does not permit a
        semantically different brand.
        """
        brand = TitleFactResolver._clean(brand)
        source_title = TitleFactResolver._clean(source_title)

        if not brand or not source_title:
            return False

        if brand.casefold() in source_title.casefold():
            return True

        compact_brand = re.sub(
            r"[^a-z0-9]+",
            "",
            brand.casefold(),
        )
        compact_source = re.sub(
            r"[^a-z0-9]+",
            "",
            source_title.casefold(),
        )

        if (
            compact_brand
            and len(compact_brand) >= 3
            and compact_brand in compact_source
        ):
            return True

        tokens = [
            token
            for token in re.findall(
                r"[A-Za-z0-9]+",
                brand,
            )
            if token
        ]

        if len(tokens) >= 2:
            pattern = r"\b" + r"(?:\W+|\W+for\W+)".join(
                re.escape(token)
                for token in tokens
            ) + r"\b"

            if re.search(
                pattern,
                source_title,
                flags=re.IGNORECASE,
            ):
                return True

        return False

    @staticmethod
    def _source_text(profile: dict) -> str:
        ledger = profile.get("source_fact_ledger", {})
        ledger = ledger if isinstance(ledger, dict) else {}
        snap = ledger.get("source_snapshot", {})
        snap = snap if isinstance(snap, dict) else {}
        parts = [
            TitleFactResolver._clean(snap.get("title")),
            *TitleFactResolver._flatten(snap.get("bullets", [])),
            TitleFactResolver._clean(snap.get("description")),
            *TitleFactResolver._flatten(ledger.get("raw_fields", {})),
        ]
        return " ".join(x for x in parts if x)

    @staticmethod
    def _quantity(v: Any) -> str:
        m = re.search(
            r"\b(\d{1,4})\s*(pcs?|pieces?|piece|sets?|packs?|pairs?)\b",
            TitleFactResolver._clean(v),
            flags=re.I,
        )
        if not m:
            return ""

        n = int(m.group(1))

        if n <= 1:
            return ""

        raw_unit = m.group(2).casefold()

        if raw_unit in {"set", "sets"}:
            unit = "sets"
        elif raw_unit in {"pack", "packs"}:
            unit = "packs"
        elif raw_unit in {"pair", "pairs"}:
            unit = "pairs"
        else:
            unit = "pcs"

        return f"{n}{unit}"

    @staticmethod
    def resolve(profile: dict) -> dict:
        profile = profile if isinstance(profile, dict) else {}
        source = TitleFactResolver._source_text(profile)

        ledger = profile.get(
            "source_fact_ledger",
            {},
        )

        if not isinstance(
            ledger,
            dict,
        ):
            ledger = {}

        snapshot = ledger.get(
            "source_snapshot",
            {},
        )

        if not isinstance(
            snapshot,
            dict,
        ):
            snapshot = {}

        source_title = (
            TitleFactResolver
            ._clean(
                snapshot.get(
                    "title",
                    "",
                )
            )
        )

        si = profile.get("title_strategy_input", {})
        si = si if isinstance(si, dict) else {}
        locked = si.get("locked", {})
        locked = locked if isinstance(locked, dict) else {}
        compat = si.get("compatibility_facts", {})
        compat = compat if isinstance(compat, dict) else {}

        relationship_map = compat.get("relationship_map", {})
        relationship_map = relationship_map if isinstance(relationship_map, dict) else {}
        model_to_brand = {}
        for relationship_brand, relationship_models in (relationship_map.get("bindings", {}) or {}).items():
            for relationship_model in relationship_models if isinstance(relationship_models, list) else []:
                model_key = TitleFactResolver._clean(relationship_model).casefold()
                if model_key:
                    model_to_brand[model_key] = TitleFactResolver._clean(relationship_brand)

        cf = si.get("candidate_facts", {})
        cf = cf if isinstance(cf, dict) else {}
        se = si.get("source_evidence", {})
        se = se if isinstance(se, dict) else {}

        approved, rejected, seen = [], [], set()
        seq = 1

        def add(text, typ, priority, required=False, source_key="", trace=None):
            nonlocal seq
            text = TitleFactResolver._clean(text)
            if not text:
                return
            key = (typ, text.casefold())
            if key in seen:
                return
            seen.add(key)

            if trace is None:
                trace = typ in TitleFactResolver.STRICT_TYPES

            if (
                trace
                and typ == "COMPATIBILITY_BRAND"
            ):
                traceable = (
                    TitleFactResolver
                    ._brand_traceable(
                        text,
                        source_title,
                    )
                )
            else:
                traceable = (
                    (not trace)
                    or
                    text.casefold()
                    in
                    source.casefold()
                )

            marketing = any(x in text.casefold() for x in TitleFactResolver.MARKETING)
            noisy = False

            if typ == "SPECIFICATION":

                occurrences = list(
                    re.finditer(
                        re.escape(text),
                        source,
                        flags=re.IGNORECASE,
                    )
                )

                if occurrences:

                    clean_occurrence_found = False

                    for occurrence in occurrences:

                        window = source[
                            max(
                                0,
                                occurrence.start()
                                -
                                70,
                            )
                            :
                            min(
                                len(source),
                                occurrence.end()
                                +
                                100,
                            )
                        ]

                        occurrence_is_noise = any(
                            re.search(
                                pattern,
                                window,
                                flags=re.IGNORECASE,
                            )
                            for pattern
                            in
                            TitleFactResolver.NOISE
                        )

                        if not occurrence_is_noise:
                            clean_occurrence_found = True
                            break

                    noisy = not clean_occurrence_found

            row = {
                "fact_id": f"F{seq:03d}", "text": text, "type": typ,
                "priority": int(priority), "required": bool(required),
                "source_key": source_key, "source_traceable": bool(traceable),
            }

            if typ in {"MODEL", "PART_NUMBER", "COMPATIBILITY_MODEL"}:
                relationship_brand = model_to_brand.get(text.casefold(), "")
                if relationship_brand:
                    row["relationship_brand"] = relationship_brand
            seq += 1

            if not traceable:
                row["rejection_reason"] = "NOT_TRACEABLE_TO_SOURCE"
                rejected.append(row)
            elif noisy:
                row["rejection_reason"] = "SOURCE_NOISE"
                rejected.append(row)
            elif marketing:
                row["rejection_reason"] = "MARKETING_TERM"
                rejected.append(row)
            else:
                approved.append(row)

        # Source-first quantity protection.
        #
        # Explicit source title quantity/unit is authoritative.  AI/Knowledge
        # may abbreviate spelling but may never change semantic packaging unit
        # (for example 5SET must not become 5pcs).
        q = TitleFactResolver._quantity(source_title)

        if not q:
            q = TitleFactResolver._quantity(
                cf.get("important_quantity")
            )

        if not q:
            qs = se.get("quantities", [])
            if isinstance(qs, list) and qs:
                q = TitleFactResolver._quantity(qs[0])

        if q:
            add(
                q,
                "QUANTITY",
                100,
                True,
                "source_title_quantity"
                if TitleFactResolver._quantity(source_title)
                else "quantity",
                False,
            )

        identity = locked.get("identity", {})
        if isinstance(identity, dict):
            add(identity.get("text"), "IDENTITY", 95, True, "locked.identity.text", False)

        compatible_brands = [
            TitleFactResolver._clean(x)
            for x in (compat.get("brands", []) or [])
            if TitleFactResolver._clean(x)
        ]

        for index, brand in enumerate(compatible_brands):
            add(
                brand,
                "COMPATIBILITY_BRAND",
                90 if index == 0 else 66,
                index == 0,
                "compatibility_facts.brands",
            )

        models = locked.get("models", {})
        if isinstance(models, dict):
            primary = TitleFactResolver._clean(models.get("primary"))
            if primary:
                add(primary, "MODEL", 85, True, "locked.models.primary")
            compatibility_model_literals = {
                TitleFactResolver._clean(value).casefold()
                for value in (
                    (compat.get("models", []) or [])
                    +
                    (compat.get("part_numbers", []) or [])
                )
                if TitleFactResolver._clean(value)
            }

            def likely_leading_year(value):
                value = TitleFactResolver._clean(value)

                if not re.fullmatch(
                    r"20\d{2}",
                    value,
                ):
                    return False

                if (
                    value.casefold()
                    in
                    compatibility_model_literals
                ):
                    return False

                return (
                    source_title.casefold()
                    .startswith(
                        value.casefold()
                        +
                        " "
                    )
                )

            for secondary_index, x in enumerate(
                models.get("secondary", []) or []
            ):

                if likely_leading_year(x):
                    continue

                # Preserve upstream model priority/order.  Earlier secondary
                # identifiers receive a tiny deterministic advantage so a
                # shorter later code cannot displace a more important earlier
                # model merely because it consumes fewer characters.
                add(
                    x,
                    "MODEL",
                    max(74, 80 - secondary_index),
                    False,
                    "locked.models.secondary",
                )

            for x in models.get("all", []) or []:

                if likely_leading_year(x):
                    continue

                add(
                    x,
                    "MODEL",
                    72,
                    False,
                    "locked.models.all",
                )

        for x in compat.get("models", []) or []:
            add(x, "COMPATIBILITY_MODEL", 80, False, "compatibility_facts.models")
        for x in compat.get("part_numbers", []) or []:
            add(x, "PART_NUMBER", 82, False, "compatibility_facts.part_numbers")
        compatible_brand_literals = {
            TitleFactResolver._clean(value).casefold()
            for value in (
                compat.get(
                    "brands",
                    [],
                )
                or
                []
            )
            if TitleFactResolver._clean(value)
        }

        for x in compat.get(
            "important_compatibility",
            [],
        ) or []:

            t = TitleFactResolver._clean(x)

            if (
                not t
                or
                t.casefold()
                in
                compatible_brand_literals
            ):
                continue

            if re.fullmatch(
                r"[A-Za-z0-9._/+*-]+",
                t,
            ):
                add(
                    t,
                    "COMPATIBILITY_MODEL",
                    74,
                    False,
                    "compatibility_facts.important_compatibility",
                )

        for field in ("source_specifications", "important_specifications", "specifications"):
            for x in cf.get(field, []) or []:
                add(x, "SPECIFICATION", 55, False, f"candidate_facts.{field}")

        for field, typ, priority in (
            ("important_context", "CONTEXT", 50),
            ("usage_scenarios", "CONTEXT", 45),
            ("design_features", "FEATURE", 42),
            ("functional_features", "FEATURE", 42),
            ("search_primary_keywords", "SEARCH_TERM", 38),
            ("search_secondary_keywords", "SEARCH_TERM", 30),
            ("source_title_segments", "SOURCE_CONTEXT", 35),
        ):
            for x in cf.get(field, []) or []:
                add(x, typ, priority, False, f"candidate_facts.{field}", True)

        # Remove scalar sub-specifications when the same value is already
        # contained inside a more complete approved specification.
        #
        # Example:
        # "15 × 8 × 4cm" + "4cm"
        # Keep the complete dimension; suppress standalone "4cm".
        specification_facts = [
            fact
            for fact in approved
            if fact.get(
                "type"
            )
            ==
            "SPECIFICATION"
        ]

        redundant_fact_ids = set()

        for fact in specification_facts:

            text_value = (
                TitleFactResolver
                ._clean(
                    fact.get(
                        "text"
                    )
                )
            )

            if not re.fullmatch(
                r"\d+(?:\.\d+)?\s*(?:mm|cm|m|in|inch|V|W|KW)",
                text_value,
                flags=re.IGNORECASE,
            ):
                continue

            for other in specification_facts:

                if (
                    other.get(
                        "fact_id"
                    )
                    ==
                    fact.get(
                        "fact_id"
                    )
                ):
                    continue

                other_text = (
                    TitleFactResolver
                    ._clean(
                        other.get(
                            "text"
                        )
                    )
                )

                if (
                    len(
                        other_text
                    )
                    >
                    len(
                        text_value
                    )
                    and
                    text_value.casefold()
                    in
                    other_text.casefold()
                ):
                    redundant_fact_ids.add(
                        fact.get(
                            "fact_id"
                        )
                    )
                    break

        if redundant_fact_ids:

            retained = []

            for fact in approved:

                if (
                    fact.get(
                        "fact_id"
                    )
                    in
                    redundant_fact_ids
                ):
                    rejected_fact = {
                        **fact,
                        "rejection_reason":
                            "REDUNDANT_SUBSPEC",
                    }

                    rejected.append(
                        rejected_fact
                    )

                else:
                    retained.append(
                        fact
                    )

            approved = retained

        approved = RepresentativeModelSelector.annotate(
            approved,
            source_title,
        )

        return {
            "version": TitleFactResolver.VERSION,
            "approved_facts": approved,
            "rejected_facts": rejected,
            "source_available": bool(source),
        }

from __future__ import annotations

import re
from typing import Any

from generator.title_quality_gate import TitleQualityGate
from core.specification_dominance import SpecificationDominance


class TitlePostRepairCompletion:
    """
    Refill factual budget after deterministic cleanup.

    Deterministic Repair may remove duplicated/noisy text from an otherwise
    valid <=75 title.  This module may use the newly freed budget, but only
    with facts already approved by the stable title pipeline.

    It never invents text, crops identifiers, or re-adds a fact when doing so
    would recreate a Quality Gate failure.
    """

    VERSION = "v1.1-specification-dominance-aware-refill"
    MIN_TARGET = 61
    MAX_LENGTH = 75

    HIGH_VALUE_TYPES = {
        "COMPATIBILITY_BRAND": 0,
        "MODEL": 1,
        "PART_NUMBER": 1,
        "COMPATIBILITY_MODEL": 2,
        "SPECIFICATION": 4,
        "CONTEXT": 5,
        "SOURCE_CONTEXT": 5,
        "FEATURE": 6,
        "SEARCH_TERM": 7,
        "MATERIAL": 8,
        "COLOR": 9,
    }

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _contains(title: str, expression: str, fact_type: str) -> bool:
        title = TitlePostRepairCompletion._clean(title)
        expression = TitlePostRepairCompletion._clean(expression)

        if not title or not expression:
            return False

        fact_type = TitlePostRepairCompletion._clean(
            fact_type
        ).upper()

        if fact_type == "SPECIFICATION":
            title_tokens = re.findall(
                r"[A-Za-z0-9]+(?:[._/+*×-][A-Za-z0-9]+)*",
                title,
            )
            if expression.casefold() in {token.casefold() for token in title_tokens}:
                return True
            return any(
                SpecificationDominance.dominates(token, expression)
                for token in title_tokens
            )

        if fact_type in {
            "MODEL",
            "PART_NUMBER",
            "COMPATIBILITY_MODEL",
        }:
            title_tokens = {
                token.casefold()
                for token in re.findall(
                    r"[A-Za-z0-9]+(?:[._/+*×-][A-Za-z0-9]+)*",
                    title,
                )
            }
            return expression.casefold() in title_tokens

        return expression.casefold() in title.casefold()

    @staticmethod
    def _candidate_expressions(fact: dict) -> list[tuple[str, str]]:
        full = TitlePostRepairCompletion._clean(
            fact.get("full_text")
            or fact.get("text")
        )
        short = TitlePostRepairCompletion._clean(
            fact.get("short_text")
        )

        variants = []

        if short:
            variants.append((short, "short_text"))

        if full and full.casefold() != short.casefold():
            variants.append((full, "full_text"))

        return variants

    @staticmethod
    def complete(
        title: str,
        plan: dict,
        composed: dict,
        target_language: str = "English",
    ) -> dict:
        original = TitlePostRepairCompletion._clean(title)

        if len(original) >= TitlePostRepairCompletion.MIN_TARGET:
            return {
                "version": TitlePostRepairCompletion.VERSION,
                "title": original,
                "status": "NOT_NEEDED",
                "added_facts": [],
                "character_count": len(original),
                "budget_remaining": max(
                    0,
                    TitlePostRepairCompletion.MAX_LENGTH - len(original),
                ),
            }

        facts = plan.get("facts", [])
        if not isinstance(facts, list):
            facts = []

        used_ids = {
            item.get("fact_id")
            for item in composed.get("used_facts", []) or []
            if isinstance(item, dict)
        }

        # Reconsider facts that were not selected or whose selected expression
        # disappeared during cleanup.  Required facts are not specially added
        # here; FinalValidator remains responsible for required-fact presence.
        candidates = []

        for fact in facts:
            if not isinstance(fact, dict):
                continue

            typ = TitlePostRepairCompletion._clean(
                fact.get("type")
            ).upper()

            if typ in {"QUANTITY", "IDENTITY"}:
                continue

            expressions = (
                TitlePostRepairCompletion
                ._candidate_expressions(fact)
            )

            if not expressions:
                continue

            # Skip the fact only when some complete approved expression is
            # still present after cleanup.
            if any(
                TitlePostRepairCompletion._contains(
                    original,
                    expression,
                    typ,
                )
                for expression, _
                in expressions
            ):
                continue

            candidates.append({
                **fact,
                "_expressions": expressions,
                "_type_rank":
                    TitlePostRepairCompletion.HIGH_VALUE_TYPES.get(
                        typ,
                        99,
                    ),
                "_was_used":
                    fact.get("fact_id") in used_ids,
            })

        candidates.sort(
            key=lambda fact: (
                int(fact.get("_type_rank", 99)),
                0 if fact.get("_was_used") else 1,
                -float(fact.get("value_score", 0) or 0),
                int(fact.get("order_index", 999)),
                len(
                    TitlePostRepairCompletion._clean(
                        fact.get("full_text")
                        or fact.get("text")
                    )
                ),
            )
        )

        # Bounded state search.  State text is the actual repaired title plus
        # complete approved expressions.  Quality Gate is checked for every
        # candidate state, so deleted duplicates/noise cannot be reintroduced.
        states = [{
            "title": original,
            "value": 0.0,
            "added": [],
        }]

        for fact in candidates:
            next_states = list(states)

            for state in states:
                for expression, source_name in fact["_expressions"]:
                    if TitlePostRepairCompletion._contains(
                        state["title"],
                        expression,
                        fact.get("type", ""),
                    ):
                        continue

                    candidate_title = (
                        TitlePostRepairCompletion._clean(
                            state["title"]
                            + " "
                            + expression
                        )
                    )

                    if (
                        len(candidate_title)
                        >
                        TitlePostRepairCompletion.MAX_LENGTH
                    ):
                        continue

                    quality = TitleQualityGate.validate(
                        candidate_title,
                        target_language,
                    )

                    if quality["status"] != "PASS":
                        continue

                    next_states.append({
                        "title": candidate_title,
                        "value": (
                            float(state["value"])
                            +
                            float(
                                fact.get(
                                    "value_score",
                                    0,
                                )
                                or
                                0
                            )
                        ),
                        "added": (
                            state["added"]
                            +
                            [{
                                "fact_id":
                                    fact.get("fact_id"),
                                "type":
                                    fact.get("type"),
                                "selected_text":
                                    expression,
                                "selected_source":
                                    source_name,
                            }]
                        ),
                    })

            # prune by exact length; preserve several value-diverse states
            by_length = {}

            for state in next_states:
                length = len(state["title"])
                by_length.setdefault(
                    length,
                    [],
                ).append(state)

            pruned = []

            for length, bucket in by_length.items():
                bucket.sort(
                    key=lambda state: (
                        -float(state["value"]),
                        -len(state["added"]),
                    )
                )
                pruned.extend(bucket[:6])

            states = sorted(
                pruned,
                key=lambda state: (
                    -float(state["value"]),
                    -len(state["title"]),
                ),
            )[:450]

        target_states = [
            state
            for state in states
            if (
                TitlePostRepairCompletion.MIN_TARGET
                <=
                len(state["title"])
                <=
                TitlePostRepairCompletion.MAX_LENGTH
            )
        ]

        if target_states:
            # Once 61 is reachable, choose factual value first, then the
            # shorter adequate title. This avoids padding toward 75.
            target_states.sort(
                key=lambda state: (
                    -float(state["value"]),
                    len(state["title"]),
                    -len(state["added"]),
                )
            )
            best = target_states[0]
            status = "COMPLETED"
        else:
            # No complete approved fact combination can reach 61.  Preserve the
            # longest source-backed quality-safe title as proof of insufficiency.
            states.sort(
                key=lambda state: (
                    -len(state["title"]),
                    -float(state["value"]),
                )
            )
            best = states[0]
            status = "SOURCE_FACTS_INSUFFICIENT"

        return {
            "version": TitlePostRepairCompletion.VERSION,
            "title": best["title"],
            "status": status,
            "added_facts": best["added"],
            "character_count": len(best["title"]),
            "budget_remaining": max(
                0,
                TitlePostRepairCompletion.MAX_LENGTH
                - len(best["title"]),
            ),
        }

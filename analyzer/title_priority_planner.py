from __future__ import annotations
from typing import Any
import re


class TitlePriorityPlanner:
    """Stable Title Pipeline V1.0: rank approved facts; never compose title."""

    VERSION = "stable-v1.3-priority-planner-short-text-safety"
    TYPE_ORDER = {
        "QUANTITY": 0, "IDENTITY": 1, "COMPATIBILITY_BRAND": 2,
        "MODEL": 3, "PART_NUMBER": 3, "COMPATIBILITY_MODEL": 4,
        "SECONDARY_IDENTITY": 5, "SPECIFICATION": 6,
        "CONTEXT": 7, "SOURCE_CONTEXT": 7, "FEATURE": 8,
        "MATERIAL": 9, "COLOR": 10, "SEARCH_TERM": 11,
    }

    @staticmethod
    def _clean(v: Any) -> str:
        return " ".join(str(v or "").split())


    @staticmethod
    def _safe_short_text(
        full_text: str,
        short_text: str,
        fact_type: str,
    ) -> str:
        """
        Generic safety gate for AI short_text.

        Goals:
        - short_text must actually be shorter
        - never introduce new model/spec-like numeric tokens
        - never return a one-word semantic fragment for long identity/context
        - preserve model/part/spec literals exactly (no shortening there)

        This is intentionally generic and product-agnostic.
        """

        full_text = TitlePriorityPlanner._clean(
            full_text
        )

        short_text = TitlePriorityPlanner._clean(
            short_text
        )

        fact_type = TitlePriorityPlanner._clean(
            fact_type
        ).upper()

        if not full_text or not short_text:
            return ""

        if len(short_text) >= len(full_text):
            return ""

        # Never rewrite factual identifier/spec values.
        if fact_type in {
            "MODEL",
            "PART_NUMBER",
            "COMPATIBILITY_MODEL",
            "SPECIFICATION",
            "QUANTITY",
            "COMPATIBILITY_BRAND",
        }:
            return ""

        full_tokens = set(
            re.findall(
                r"[A-Za-z]*\d+[A-Za-z0-9._/+*-]*",
                full_text,
                flags=re.IGNORECASE,
            )
        )

        short_tokens = set(
            re.findall(
                r"[A-Za-z]*\d+[A-Za-z0-9._/+*-]*",
                short_text,
                flags=re.IGNORECASE,
            )
        )

        if not short_tokens.issubset(
            full_tokens
        ):
            return ""

        # Long semantic facts must not collapse to a single fragment word.
        semantic_words = re.findall(
            r"[A-Za-zÀ-ÿ]+",
            short_text,
        )

        if (
            fact_type
            in {
                "IDENTITY",
                "SECONDARY_IDENTITY",
                "CONTEXT",
                "SOURCE_CONTEXT",
                "FEATURE",
                "SEARCH_TERM",
            }
            and
            len(full_text)
            >=
            20
            and
            len(semantic_words)
            <
            2
        ):
            return ""

        return short_text


    @staticmethod
    def _compatibility_phrase(brand: str, language: str) -> str:
        brand = TitlePriorityPlanner._clean(brand)
        lang = TitlePriorityPlanner._clean(language).casefold()
        mapping = {
            "english": "Compatible with",
            "spanish": "Compatible con",
            "español": "Compatible con",
            "french": "Compatible avec",
            "français": "Compatible avec",
            "german": "Kompatibel mit",
            "deutsch": "Kompatibel mit",
            "italian": "Compatibile con",
            "portuguese": "Compatível com",
            "dutch": "Compatibel met",
            "swedish": "Kompatibel med",
        }
        if "japanese" in lang or "日本" in lang:
            return f"{brand} 対応"
        prefix = "Compatible with"
        for key, value in mapping.items():
            if key in lang:
                prefix = value
                break
        return f"{prefix} {brand}"

    @staticmethod
    def build(resolved_facts: dict, ai_plan: dict | None = None, target_language="English") -> dict:
        facts = resolved_facts.get("approved_facts", [])
        facts = facts if isinstance(facts, list) else []
        ai_plan = ai_plan if isinstance(ai_plan, dict) else {}

        ai_map = {}
        for item in ai_plan.get("fact_priorities", []) or []:
            if isinstance(item, dict):
                fid = TitlePriorityPlanner._clean(item.get("fact_id"))
                if fid:
                    ai_map[fid] = item

        out = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fid = TitlePriorityPlanner._clean(fact.get("fact_id"))
            full = TitlePriorityPlanner._clean(fact.get("text"))
            if not fid or not full:
                continue

            ai = ai_map.get(fid, {})

            if fact.get("type") == "COMPATIBILITY_BRAND":
                full = TitlePriorityPlanner._compatibility_phrase(
                    full,
                    target_language,
                )

            short = (
                TitlePriorityPlanner
                ._safe_short_text(
                    full,
                    ai.get(
                        "short_text",
                        "",
                    ),
                    fact.get(
                        "type",
                        "",
                    ),
                )
            )

            fallback_order = (
                TitlePriorityPlanner.TYPE_ORDER.get(
                    fact.get("type"),
                    99,
                )
                *
                10
            )

            try:
                order_index = int(
                    ai.get(
                        "order_index",
                        fallback_order,
                    )
                )
            except Exception:
                order_index = fallback_order

            if order_index == 999:
                order_index = fallback_order

            try:
                value = float(ai.get("value_score", fact.get("priority", 0)))
            except Exception:
                value = float(fact.get("priority", 0) or 0)

            out.append({
                **fact,
                "full_text": full,
                "short_text": short,
                "value_score": value,
                "selection_rank":
                    TitlePriorityPlanner.TYPE_ORDER.get(
                        fact.get("type"),
                        99,
                    ),
                "order_index":
                    order_index,
                "language": target_language,
                "ai_reason": TitlePriorityPlanner._clean(ai.get("reason")),
            })

        out.sort(key=lambda x: (
            0 if x.get("required") else 1,
            int(x.get("selection_rank", 99)),
            -float(x.get("value_score", 0)),
            len(x.get("full_text", "")),
        ))

        return {
            "version": TitlePriorityPlanner.VERSION,
            "target_language": target_language,
            "facts": out,
        }

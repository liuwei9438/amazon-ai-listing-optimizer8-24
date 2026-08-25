from __future__ import annotations

import json
from typing import Any

from .title_priority_planner_prompt import (
    TITLE_PRIORITY_PLANNER_SYSTEM_PROMPT,
    build_title_priority_planner_prompt,
)


class TitlePriorityPlannerError(Exception):
    pass


class TitlePriorityPlannerGenerator:
    """
    Stable Title Pipeline V1.0

    AI responsibilities:
    - rank approved facts
    - optionally provide safe COMPLETE short_text

    AI does NOT:
    - write the final title
    - invent facts
    - decide final pass/fail
    - enforce final 75-char budget
    """

    VERSION = "stable-v1.0-ai-priority-planner"

    @staticmethod
    def _clean(value: Any) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def generate(
        resolved_facts: dict,
        api_key: str,
        model: str = "gpt-4.1-mini",
        target_language: str = "English",
    ) -> dict:

        if not isinstance(resolved_facts, dict):
            raise TitlePriorityPlannerError(
                "resolved_facts must be a dictionary"
            )

        approved = resolved_facts.get(
            "approved_facts",
            [],
        )

        if not isinstance(approved, list):
            raise TitlePriorityPlannerError(
                "resolved_facts.approved_facts must be a list"
            )

        try:
            from openai import OpenAI
        except Exception as exc:
            raise TitlePriorityPlannerError(
                f"OpenAI client unavailable: {exc}"
            )

        client = OpenAI(
            api_key=api_key,
            timeout=90,
            max_retries=0,
        )

        user_prompt = build_title_priority_planner_prompt(
            resolved_facts,
            target_language=target_language,
        )

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role":
                            "system",
                        "content":
                            TITLE_PRIORITY_PLANNER_SYSTEM_PROMPT,
                    },
                    {
                        "role":
                            "user",
                        "content":
                            user_prompt,
                    },
                ],
                response_format={
                    "type":
                        "json_object"
                },
            )
        except Exception as exc:
            raise TitlePriorityPlannerError(
                f"AI priority planner call failed: {exc}"
            )

        try:
            result = json.loads(
                response.choices[0]
                .message
                .content
            )
        except Exception as exc:
            raise TitlePriorityPlannerError(
                f"AI priority planner parse failed: {exc}"
            )

        if not isinstance(result, dict):
            raise TitlePriorityPlannerError(
                "AI priority planner result must be a dictionary"
            )

        raw_items = result.get(
            "fact_priorities",
            [],
        )

        if not isinstance(raw_items, list):
            raw_items = []

        approved_by_id = {
            str(item.get("fact_id", "")).strip():
                item
            for item in approved
            if isinstance(item, dict)
            and str(item.get("fact_id", "")).strip()
        }

        normalized = []

        for item in raw_items:

            if not isinstance(item, dict):
                continue

            fact_id = (
                TitlePriorityPlannerGenerator
                ._clean(
                    item.get(
                        "fact_id",
                        "",
                    )
                )
            )

            if (
                not fact_id
                or
                fact_id not in approved_by_id
            ):
                continue

            try:
                value_score = float(
                    item.get(
                        "value_score",
                        approved_by_id[
                            fact_id
                        ].get(
                            "priority",
                            0,
                        ),
                    )
                )
            except Exception:
                value_score = float(
                    approved_by_id[
                        fact_id
                    ].get(
                        "priority",
                        0,
                    )
                    or
                    0
                )

            value_score = max(
                0.0,
                min(
                    100.0,
                    value_score,
                ),
            )

            short_text = (
                TitlePriorityPlannerGenerator
                ._clean(
                    item.get(
                        "short_text",
                        "",
                    )
                )
            )

            reason = (
                TitlePriorityPlannerGenerator
                ._clean(
                    item.get(
                        "reason",
                        "",
                    )
                )
            )

            normalized.append(
                {
                    "fact_id":
                        fact_id,
                    "value_score":
                        value_score,
                    "short_text":
                        short_text,
                    "reason":
                        reason,
                }
            )

        # Fill missing facts deterministically so AI omission cannot delete facts.
        seen = {
            item[
                "fact_id"
            ]
            for item in normalized
        }

        for fact_id, fact in approved_by_id.items():

            if fact_id in seen:
                continue

            try:
                fallback_score = float(
                    fact.get(
                        "priority",
                        0,
                    )
                    or
                    0
                )
            except Exception:
                fallback_score = 0.0

            normalized.append(
                {
                    "fact_id":
                        fact_id,
                    "value_score":
                        fallback_score,
                    "short_text":
                        "",
                    "reason":
                        "Deterministic fallback priority",
                }
            )

        return {
            "version":
                TitlePriorityPlannerGenerator.VERSION,
            "fact_priorities":
                normalized,
        }

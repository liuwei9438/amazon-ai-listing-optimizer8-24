from __future__ import annotations

import json
import re
from typing import Any


class RequiredCoreRepairError(Exception):
    pass


class TitleRequiredCoreRepair:
    """
    Rare fallback for Stable Title Pipeline required-core overflow.

    It may only shorten the required IDENTITY expression. Brand/model facts are
    immutable. The returned short identity must pass deterministic token/subset
    checks before it can be used by the normal priority planner/composer.
    """

    VERSION = "stable-v1.0-required-core-overflow-repair"

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [
            token.casefold()
            for token in re.findall(
                r"[A-Za-zÀ-ÿ0-9]+(?:[-/][A-Za-zÀ-ÿ0-9]+)*",
                TitleRequiredCoreRepair._clean(text),
            )
        ]

    @staticmethod
    def _required_non_identity_texts(plan: dict) -> list[str]:
        facts = plan.get("facts", []) if isinstance(plan, dict) else []
        result: list[str] = []
        for fact in facts if isinstance(facts, list) else []:
            if not isinstance(fact, dict) or not fact.get("required"):
                continue
            if TitleRequiredCoreRepair._clean(fact.get("type")).upper() == "IDENTITY":
                continue
            text = TitleRequiredCoreRepair._clean(fact.get("full_text"))
            if text:
                result.append(text)
        return result

    @staticmethod
    def identity_budget(plan: dict, max_length: int = 75) -> int:
        other = TitleRequiredCoreRepair._required_non_identity_texts(plan)
        # One separating space per non-identity required expression.
        occupied = len(" ".join(other))
        separators = 1 if other else 0
        return max(0, int(max_length) - occupied - separators)

    @staticmethod
    def validate_short_identity(
        full_text: str,
        short_text: str,
        max_identity_chars: int,
    ) -> bool:
        full = TitleRequiredCoreRepair._clean(full_text)
        short = TitleRequiredCoreRepair._clean(short_text)

        if not full or not short:
            return False
        if len(short) >= len(full):
            return False
        if len(short) > int(max_identity_chars):
            return False

        full_tokens = TitleRequiredCoreRepair._tokens(full)
        short_tokens = TitleRequiredCoreRepair._tokens(short)
        if len(short_tokens) < 2:
            return False

        full_set = set(full_tokens)
        if any(token not in full_set for token in short_tokens):
            return False

        # Preserve the final product-head token. This blocks reductions such as
        # "Cooling Fan Assembly" -> "Cooling Fan" when Assembly is the head.
        if full_tokens and short_tokens and full_tokens[-1] != short_tokens[-1]:
            return False

        # Prevent over-aggressive semantic collapse. Symbolic compression (&)
        # and removal of repeated/generic modifiers are allowed, but most of the
        # original identity vocabulary must remain.
        unique_full = set(full_tokens)
        unique_short = set(short_tokens)
        retention = len(unique_short) / max(1, len(unique_full))
        if retention < 0.55:
            return False

        # Numeric/code-like identity tokens that remain in the short expression
        # must be exact; no newly invented numeric token can appear because of
        # the subset rule above.
        return True

    @staticmethod
    def generate(
        plan: dict,
        api_key: str,
        model: str = "gpt-4.1-mini",
        target_language: str = "English",
        max_length: int = 75,
    ) -> dict:
        facts = plan.get("facts", []) if isinstance(plan, dict) else []
        required = [
            f for f in facts
            if isinstance(f, dict) and f.get("required")
        ]
        identities = [
            f for f in required
            if TitleRequiredCoreRepair._clean(f.get("type")).upper() == "IDENTITY"
        ]
        if len(identities) != 1:
            raise RequiredCoreRepairError(
                "required-core repair needs exactly one required identity"
            )

        identity = identities[0]
        full_identity = TitleRequiredCoreRepair._clean(identity.get("full_text"))
        fact_id = TitleRequiredCoreRepair._clean(identity.get("fact_id"))
        budget = TitleRequiredCoreRepair.identity_budget(plan, max_length=max_length)

        if not full_identity or not fact_id or budget <= 0:
            raise RequiredCoreRepairError("invalid identity or repair budget")

        try:
            from openai import OpenAI
        except Exception as exc:
            raise RequiredCoreRepairError(f"OpenAI client unavailable: {exc}")

        system_prompt = """
You repair ONE overlong required product identity for a deterministic marketplace title.

STRICT ROLE:
- Return only a shorter expression of the SAME product identity.
- You may DELETE redundant/generic/repeated words from the supplied identity.
- You may replace the word "and" with "&".
- You MUST NOT introduce synonyms or any word/token that is not already present in the full identity.
- You MUST NOT change or add brand, model, quantity, size, material, or other facts.
- Preserve the physical product/component meaning and the final product-head token.
- The result must be a complete natural identity, not a fragment.
- Respect max_identity_chars exactly.

Return JSON only:
{"short_text":"...","reason":"..."}
""".strip()

        payload = {
            "target_language": target_language,
            "fact_id": fact_id,
            "full_identity": full_identity,
            "max_identity_chars": budget,
            "immutable_required_facts": TitleRequiredCoreRepair._required_non_identity_texts(plan),
        }

        try:
            client = OpenAI(api_key=api_key, timeout=90, max_retries=0)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
        except Exception as exc:
            raise RequiredCoreRepairError(f"required-core repair call failed: {exc}")

        short = TitleRequiredCoreRepair._clean(
            result.get("short_text", "") if isinstance(result, dict) else ""
        )
        if not TitleRequiredCoreRepair.validate_short_identity(
            full_identity,
            short,
            budget,
        ):
            raise RequiredCoreRepairError("AI short identity failed deterministic safety validation")

        return {
            "version": TitleRequiredCoreRepair.VERSION,
            "fact_id": fact_id,
            "full_text": full_identity,
            "short_text": short,
            "max_identity_chars": budget,
            "reason": TitleRequiredCoreRepair._clean(
                result.get("reason", "") if isinstance(result, dict) else ""
            ),
        }

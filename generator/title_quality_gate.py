from __future__ import annotations

import re
from typing import Any


class TitleQualityGate:
    """
    Deterministic title-quality invariants.

    This gate is intentionally separate from factual validation:
    - FinalValidator protects hard facts / required facts.
    - QualityGate protects presentation quality and obvious SEO regressions.

    It never repairs text and never reinterprets the product.
    """

    VERSION = "v1.0-cumulative-quality-regression-gate"

    NOISE_PATTERNS = (
        (r"\bas shown\b", "NOISE_AS_SHOWN"),
        (r"\bmodel number\b", "NOISE_MODEL_NUMBER"),
        (r"\bwholesale\b", "NOISE_WHOLESALE"),
    )

    ORPHAN_ENDINGS = {
        "suitable", "models", "model", "accessories",
    }

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _word_tokens(title: str) -> list[str]:
        return re.findall(
            r"[A-Za-z0-9]+(?:[._/+*-][A-Za-z0-9]+)*",
            title,
        )

    @staticmethod
    def _code_tokens(title: str) -> list[str]:
        return [
            token
            for token in TitleQualityGate._word_tokens(title)
            if any(ch.isdigit() for ch in token)
            and len(token) >= 2
        ]

    @staticmethod
    def _normalized_code(token: str) -> str:
        return TitleQualityGate._clean(token).casefold()

    @staticmethod
    def validate(
        title: str,
        target_language: str = "English",
    ) -> dict:
        title = TitleQualityGate._clean(title)
        fold = title.casefold()
        errors: list[str] = []

        # 1. Repeated compatibility qualifier.
        qualifier_map = {
            "english": "compatible with",
            "spanish": "compatible con",
            "french": "compatible avec",
            "german": "kompatibel mit",
            "italian": "compatibile con",
            "portuguese": "compatível com",
            "dutch": "compatibel met",
            "swedish": "kompatibel med",
        }
        lang = TitleQualityGate._clean(target_language).casefold()
        qualifier = "compatible with"
        for key, value in qualifier_map.items():
            if key in lang:
                qualifier = value
                break

        if len(re.findall(
            rf"\b{re.escape(qualifier)}\b",
            fold,
        )) > 1:
            errors.append("REPEATED_COMPATIBILITY_QUALIFIER")

        # 2. Exact adjacent word duplication.
        words = [
            x.casefold()
            for x in TitleQualityGate._word_tokens(title)
        ]
        for left, right in zip(words, words[1:]):
            if left == right and len(left) > 1:
                errors.append("ADJACENT_TOKEN_DUPLICATION")
                break

        # 3. Repeated identifier/spec token.
        #
        # Exact repetition is always wasteful in a <=75 char title.  This
        # catches 7260R ... 7260R, CC1 ... CC1, 160mm ... 160mm, etc.
        codes = [
            TitleQualityGate._normalized_code(x)
            for x in TitleQualityGate._code_tokens(title)
        ]
        if len(codes) != len(set(codes)):
            errors.append("REPEATED_IDENTIFIER_OR_SPEC")

        # 4. Known low-value metadata/noise.
        for pattern, code in TitleQualityGate.NOISE_PATTERNS:
            if re.search(pattern, fold, flags=re.IGNORECASE):
                errors.append(code)

        # 5. Broken/orphan ending.
        if words and words[-1] in TitleQualityGate.ORPHAN_ENDINGS:
            errors.append("ORPHAN_LOW_VALUE_ENDING")

        # 6. A few structurally obvious repeated noun phrases.
        # Detect repeated 2-token phrase, excluding pure numeric pairs.
        bigrams = []
        for i in range(len(words) - 1):
            pair = (words[i], words[i + 1])
            if all(re.fullmatch(r"\d+(?:\.\d+)?", x) for x in pair):
                continue
            bigrams.append(pair)

        seen = set()
        for pair in bigrams:
            if pair in seen:
                errors.append("REPEATED_TWO_TOKEN_PHRASE")
                break
            seen.add(pair)

        errors = list(dict.fromkeys(errors))

        return {
            "version": TitleQualityGate.VERSION,
            "status": "PASS" if not errors else "FAIL",
            "errors": errors,
        }

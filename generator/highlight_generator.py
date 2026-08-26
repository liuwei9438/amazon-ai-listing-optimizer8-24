from __future__ import annotations

import re
from typing import Any


class HighlightGenerator:
    """
    Highlight Generator V3.1

    Goal:
    - product identity + a small number of highest-value confirmed features
    - no re-understanding, no invented benefits, no title/model stuffing
    - deterministic cleaning/deduplication
    """

    VERSION = "v3.1-fact-focused-highlights"
    MAX_HIGHLIGHTS = 6
    MAX_SHORT_HIGHLIGHTS = 3

    BLOCKED_WORDS = [
        "best", "best seller", "#1", "premium", "original", "genuine",
        "official", "authentic", "discount", "promotion", "perfect",
        "amazing", "top quality", "high quality", "oem",
    ]

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip(" ,;:")

    @staticmethod
    def _list(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        out, seen = [], set()
        for item in value:
            text = HighlightGenerator._clean(item)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    @staticmethod
    def _normalized(value: str) -> str:
        value = HighlightGenerator._clean(value).casefold()
        value = value.replace("×", "x").replace("*", "x")
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    @staticmethod
    def _blocked(value: str) -> bool:
        lower = HighlightGenerator._clean(value).casefold()
        return any(word in lower for word in HighlightGenerator.BLOCKED_WORDS)

    @staticmethod
    def _redundant(candidate: str, accepted: list[str]) -> bool:
        c = HighlightGenerator._normalized(candidate)
        if not c:
            return True
        for item in accepted:
            a = HighlightGenerator._normalized(item)
            if not a:
                continue
            if c == a:
                return True
            # Avoid repeating a short feature already contained by identity or
            # another richer highlight. Require at least two words so numeric
            # specs such as 240W are not swallowed accidentally.
            c_words = c.split()
            if len(c_words) >= 2 and c in a:
                return True
        return False

    @staticmethod
    def _identity(profile: dict) -> str:
        normalized = profile.get("normalized_knowledge", {}) or {}
        identity = normalized.get("identity", {}) or {}
        text = HighlightGenerator._clean(identity.get("text", ""))
        if text:
            return text

        knowledge = profile.get("product_knowledge", {}) or {}
        identity = knowledge.get("identity", {}) or {}
        return HighlightGenerator._clean(
            identity.get("product_name")
            or identity.get("object_name")
            or identity.get("product_type")
            or ""
        )

    @staticmethod
    def _feature_pool(profile: dict) -> list[str]:
        knowledge = profile.get("product_knowledge", {}) or {}
        identity = knowledge.get("identity", {}) or {}
        classification = knowledge.get("feature_classification", {}) or {}
        strategy = knowledge.get("generation_strategy", {}) or {}

        # Functional facts first, then materials/specs, then design. This keeps
        # highlights concise and buyer-relevant without inventing benefits.
        groups = [
            identity.get("functional_features", []),
            classification.get("functional_features", []),
            classification.get("materials", []),
            classification.get("specifications", []),
            identity.get("design_features", []),
        ]

        pool = []
        for group in groups:
            pool.extend(HighlightGenerator._list(group))

        if not pool:
            pool.extend(HighlightGenerator._list(strategy.get("highlight_focus", [])))

        return pool

    @staticmethod
    def _compatibility(profile: dict) -> str:
        normalized = profile.get("normalized_knowledge", {}) or {}
        compat = normalized.get("compatibility", {}) or {}
        phrase = HighlightGenerator._clean(compat.get("phrase", ""))
        if phrase:
            return phrase

        knowledge = profile.get("product_knowledge", {}) or {}
        relation = knowledge.get("relationship", {}) or {}
        brands = HighlightGenerator._list(relation.get("brands", []))
        if brands:
            return "Compatible with " + ", ".join(brands[:3])
        return ""

    @staticmethod
    def generate(profile: dict) -> dict:
        profile = profile if isinstance(profile, dict) else {}

        highlights: list[dict] = []
        accepted_texts: list[str] = []

        identity = HighlightGenerator._identity(profile)
        if identity and not HighlightGenerator._blocked(identity):
            highlights.append({"type": "product", "text": identity})
            accepted_texts.append(identity)

        for feature in HighlightGenerator._feature_pool(profile):
            text = HighlightGenerator._clean(feature)
            if not text or HighlightGenerator._blocked(text):
                continue
            if HighlightGenerator._redundant(text, accepted_texts):
                continue
            highlights.append({"type": "feature", "text": text})
            accepted_texts.append(text)
            if len(highlights) >= HighlightGenerator.MAX_HIGHLIGHTS - 1:
                break

        compatibility = HighlightGenerator._compatibility(profile)
        if (
            compatibility
            and not HighlightGenerator._blocked(compatibility)
            and not HighlightGenerator._redundant(compatibility, accepted_texts)
            and len(highlights) < HighlightGenerator.MAX_HIGHLIGHTS
        ):
            highlights.append({"type": "compatibility", "text": compatibility})

        blocked = HighlightGenerator.check_blocked_words(str(highlights))

        return {
            "version": HighlightGenerator.VERSION,
            "highlights": highlights,
            "short_highlights": highlights[: HighlightGenerator.MAX_SHORT_HIGHLIGHTS],
            "validation": {"compliance_ok": len(blocked) == 0},
            "blocked_words": blocked,
        }

    @staticmethod
    def check_blocked_words(text):
        lower = HighlightGenerator._clean(text).casefold()
        return [word for word in HighlightGenerator.BLOCKED_WORDS if word in lower]

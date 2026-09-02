from __future__ import annotations

import re
from typing import Any


class HighlightGenerator:
    """
    Highlight Generator V3.2

    Root-cause fixes:
    - semantic fact dedupe (e.g. "30MM" vs "30MM size")
    - suppress features already expressed by product identity
    - rank concrete buyer-relevant facts above generic function fragments
    - remain source-backed and deterministic; never invent benefits
    """

    VERSION = "v3.2-semantic-dedupe-ranked-highlights"
    MAX_HIGHLIGHTS = 6
    MAX_SHORT_HIGHLIGHTS = 3

    BLOCKED_WORDS = [
        "best", "best seller", "#1", "premium", "original", "genuine",
        "official", "authentic", "discount", "promotion", "perfect",
        "amazing", "top quality", "high quality", "oem",
    ]

    # Words that add little fact meaning when comparing equivalent facts.
    SEMANTIC_NOISE = {
        "size", "dimension", "dimensions", "material", "materials",
        "color", "colour", "feature", "features", "function", "functions",
        "type", "design", "shape",
    }

    # Low-value function fragments that should not displace concrete specs.
    GENERIC_FUNCTION_WORDS = {
        "cooling", "heating", "sealing", "sensing", "pressing", "cleaning",
        "polishing", "cutting", "charging", "mounting", "supporting",
    }

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip(" ,;:")

    @staticmethod
    def _list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
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
        value = re.sub(r"(?<=\d)\s+(?=[a-z]{1,5}\b)", "", value)
        value = re.sub(r"[^a-z0-9]+", " ", value).strip()
        return value

    @staticmethod
    def _semantic_tokens(value: str) -> list[str]:
        tokens = HighlightGenerator._normalized(value).split()
        return [t for t in tokens if t not in HighlightGenerator.SEMANTIC_NOISE]

    @staticmethod
    def _semantic_key(value: str) -> str:
        return " ".join(HighlightGenerator._semantic_tokens(value))

    @staticmethod
    def _blocked(value: str) -> bool:
        lower = HighlightGenerator._clean(value).casefold()
        return any(word in lower for word in HighlightGenerator.BLOCKED_WORDS)

    @staticmethod
    def _token_overlap(a: str, b: str) -> float:
        aa = set(HighlightGenerator._semantic_tokens(a))
        bb = set(HighlightGenerator._semantic_tokens(b))
        if not aa or not bb:
            return 0.0
        return len(aa & bb) / min(len(aa), len(bb))

    @staticmethod
    def _numeric_atoms(value: str) -> set[str]:
        text = HighlightGenerator._clean(value).casefold().replace("×", "x").replace("*", "x")
        atoms: set[str] = set()

        # Expand combined dimensions such as 65x12x28 mm into 65mm/12mm/28mm
        # so a later partial spec like "outer diameter 65mm" can be recognized
        # as already covered by the richer combined dimension.
        for m in re.finditer(r"(\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)+)\s*(mm|cm|m|in|inch|kg|g|lb)?", text):
            values = m.group(1).split("x")
            unit = m.group(2) or ""
            for number in values:
                atoms.add(number + unit)

        for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(mm|cm|m|in|inch|oz|ml|l|v|w|kw|rpm|hz|kg|g|lb|mah|ah|ohm|k)\b", text):
            atoms.add(number + unit)
        return atoms

    @staticmethod
    def _redundant(candidate: str, accepted: list[str]) -> bool:
        c = HighlightGenerator._semantic_key(candidate)
        if not c:
            return True

        c_atoms = HighlightGenerator._numeric_atoms(candidate)

        for item in accepted:
            a = HighlightGenerator._semantic_key(item)
            if not a:
                continue
            if c == a:
                return True

            # Equivalent fact with only a wrapper word added/removed:
            # "30MM" vs "30MM size", "rubber" vs "rubber material".
            if c in a or a in c:
                c_tokens = c.split()
                a_tokens = a.split()
                if min(len(c_tokens), len(a_tokens)) <= 2:
                    return True

            # A partial numeric specification is redundant when an already
            # accepted richer combined spec contains every numeric atom.
            a_atoms = HighlightGenerator._numeric_atoms(item)
            if c_atoms and a_atoms and c_atoms.issubset(a_atoms) and len(a_atoms) > len(c_atoms):
                return True

            # Suppress a short feature already expressed by identity, e.g.
            # "cooling" under "Hot End Cooling Fan".
            if len(c.split()) <= 2 and HighlightGenerator._token_overlap(candidate, item) >= 1.0:
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
    def _fact_score(text: str, fact_type: str, identity: str) -> float:
        norm = HighlightGenerator._normalized(text)
        tokens = HighlightGenerator._semantic_tokens(text)
        score = {
            "specification": 95.0,
            "material": 82.0,
            "functional": 78.0,
            "design": 70.0,
        }.get(fact_type, 60.0)

        # Concrete numeric specifications are especially useful.
        if re.search(r"\d", text):
            score += 12.0
        if re.search(r"\b(?:mm|cm|m|in|inch|oz|ml|l|v|w|kw|rpm|hz|kg|g|lb|mah|ah|ohm|k)\b", norm):
            score += 6.0

        # Penalize generic one-word function fragments and identity overlap.
        if len(tokens) == 1 and tokens[0] in HighlightGenerator.GENERIC_FUNCTION_WORDS:
            score -= 28.0
        overlap = HighlightGenerator._token_overlap(text, identity)
        if overlap >= 1.0 and len(tokens) <= 2:
            score -= 35.0
        elif overlap >= 0.75:
            score -= 18.0

        # Very long prose is less suitable as a compact highlight.
        if len(text) > 70:
            score -= 20.0
        elif len(text) > 45:
            score -= 8.0

        return score

    @staticmethod
    def _feature_pool(profile: dict, identity: str) -> list[tuple[float, int, str]]:
        knowledge = profile.get("product_knowledge", {}) or {}
        ident = knowledge.get("identity", {}) or {}
        classification = knowledge.get("feature_classification", {}) or {}
        strategy = knowledge.get("generation_strategy", {}) or {}

        groups = [
            ("specification", classification.get("specifications", [])),
            ("material", classification.get("materials", [])),
            ("functional", ident.get("functional_features", [])),
            ("functional", classification.get("functional_features", [])),
            ("design", ident.get("design_features", [])),
            ("design", classification.get("design_features", [])),
        ]

        candidates: list[tuple[float, int, str]] = []
        source_order = 0
        for fact_type, group in groups:
            for text in HighlightGenerator._list(group):
                candidates.append((
                    HighlightGenerator._fact_score(text, fact_type, identity),
                    source_order,
                    text,
                ))
                source_order += 1

        if not candidates:
            for text in HighlightGenerator._list(strategy.get("highlight_focus", [])):
                candidates.append((
                    HighlightGenerator._fact_score(text, "functional", identity),
                    source_order,
                    text,
                ))
                source_order += 1

        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates

    # 不是品牌的"品牌"：AI 偶尔把 "3D Printer" 拆开，
    # 把 Print / 3D 当成兼容品牌输出（"Compatible with Print"）。
    JUNK_BRAND_WORDS = {
        "print", "prints", "3d", "printer", "printing",
        "compatible", "with", "and", "models", "model",
    }

    @staticmethod
    def _compatibility(profile: dict) -> str:
        normalized = profile.get("normalized_knowledge", {}) or {}
        compat = normalized.get("compatibility", {}) or {}
        phrase = HighlightGenerator._clean(compat.get("phrase", ""))

        if phrase:
            # 剔除假品牌词后若已不剩任何实际品牌，整句丢弃，
            # 避免 "Compatible with Print" 这种残句进入亮点。
            stripped = phrase
            for junk in HighlightGenerator.JUNK_BRAND_WORDS:
                stripped = re.sub(
                    r"(?i)\b" + re.escape(junk) + r"\b",
                    " ",
                    stripped,
                )
            leftover = re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-/]*", stripped)
            if leftover:
                return phrase
            return ""

        knowledge = profile.get("product_knowledge", {}) or {}
        relation = knowledge.get("relationship", {}) or {}
        brands = [
            b for b in HighlightGenerator._list(relation.get("brands", []))
            if b.casefold() not in {
                "print", "prints", "3d", "printer", "printing",
            }
        ]
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

        for _score, _order, feature in HighlightGenerator._feature_pool(profile, identity):
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

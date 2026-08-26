from __future__ import annotations

import re
from typing import Any

from core.specification_dominance import SpecificationDominance
from core.semantic_containment import SemanticContainment


class TitleDeterministicRepair:
    """
    Low-risk deterministic repair for final title presentation.

    Scope is intentionally narrow:
    - exact duplicate identifiers/specs
    - adjacent duplicate tokens
    - repeated compatibility qualifier
    - known low-value metadata/noise
    - orphan low-value endings
    - repeated exact two-token phrases

    It does NOT invent facts, rename products, infer brands/models, or change
    quantity units.
    """

    VERSION = "v1.0-low-risk-presentation-repair"

    NOISE = (
        r"\bas shown\b",
        r"\bmodel number\b",
    )

    ORPHAN_ENDINGS = {
        "suitable", "models", "model", "accessories",
    }

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip(" ,;:-")

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(
            r"[A-Za-z0-9]+(?:[._/+*-][A-Za-z0-9]+)*|[^\w\s]",
            text,
        )

    @staticmethod
    def _word_tokens(text: str) -> list[str]:
        return re.findall(
            r"[A-Za-z0-9]+(?:[._/+*-][A-Za-z0-9]+)*",
            text,
        )

    @staticmethod
    def _is_identifier_or_spec(token: str) -> bool:
        return (
            len(token) >= 2
            and any(ch.isdigit() for ch in token)
        )

    @staticmethod
    def _dedupe_exact_code_tokens(text: str) -> str:
        parts = text.split()
        seen = set()
        out = []

        for part in parts:
            core = part.strip(" ,;:()[]{}")
            key = core.casefold()

            if (
                core
                and TitleDeterministicRepair._is_identifier_or_spec(core)
                and key in seen
            ):
                continue

            if core and TitleDeterministicRepair._is_identifier_or_spec(core):
                seen.add(key)

            out.append(part)

        return TitleDeterministicRepair._clean(" ".join(out))

    @staticmethod
    def _dedupe_adjacent_words(text: str) -> str:
        parts = text.split()
        out = []

        for part in parts:
            core = part.strip(" ,;:()[]{}").casefold()

            if out:
                prev = out[-1].strip(" ,;:()[]{}").casefold()
                if core and core == prev and len(core) > 1:
                    continue

            out.append(part)

        return TitleDeterministicRepair._clean(" ".join(out))

    @staticmethod
    def _dedupe_repeated_bigram(text: str) -> str:
        """
        Remove only an exact repeated two-word phrase occurrence.

        Example:
          "... Brake Disc Brake Disc ..." -> "... Brake Disc ..."

        Non-adjacent repeated phrases are left alone because removing them can
        alter meaning/order.
        """
        parts = text.split()
        if len(parts) < 4:
            return text

        out = []
        i = 0

        while i < len(parts):
            if i + 3 < len(parts):
                a = parts[i].strip(" ,;:()[]{}").casefold()
                b = parts[i+1].strip(" ,;:()[]{}").casefold()
                c = parts[i+2].strip(" ,;:()[]{}").casefold()
                d = parts[i+3].strip(" ,;:()[]{}").casefold()

                if a and b and a == c and b == d:
                    out.extend(parts[i:i+2])
                    i += 4
                    continue

            out.append(parts[i])
            i += 1

        return TitleDeterministicRepair._clean(" ".join(out))

    @staticmethod
    def _repair_compatibility_qualifier(text: str, target_language: str) -> str:
        lang = TitleDeterministicRepair._clean(target_language).casefold()

        qualifiers = {
            "english": "Compatible with",
            "spanish": "Compatible con",
            "french": "Compatible avec",
            "german": "Kompatibel mit",
            "italian": "Compatibile con",
            "portuguese": "Compatível com",
            "dutch": "Compatibel met",
            "swedish": "Kompatibel med",
        }

        qualifier = "Compatible with"
        for key, value in qualifiers.items():
            if key in lang:
                qualifier = value
                break

        pattern = re.compile(
            re.escape(qualifier),
            flags=re.IGNORECASE,
        )
        matches = list(pattern.finditer(text))

        if len(matches) <= 1:
            return text

        # Keep the first qualifier; later qualifiers become a comma separator.
        # No brand text is deleted.
        first = True

        def repl(match):
            nonlocal first
            if first:
                first = False
                return qualifier
            return ","

        repaired = pattern.sub(repl, text)
        repaired = re.sub(r"\s*,\s*", ", ", repaired)
        return TitleDeterministicRepair._clean(repaired)

    @staticmethod
    def _dedupe_later_exact_bigram(text: str) -> str:
        """
        Remove a later exact two-word phrase only when the same phrase already
        appeared earlier. The first occurrence is preserved.

        This is intentionally conservative: only exact lexical duplicates are
        removed; no synonym or semantic rewriting occurs.
        """
        parts = text.split()

        while True:
            words = [
                part.strip(" ,;:()[]{}").casefold()
                for part in parts
            ]

            seen = {}
            duplicate_start = None

            for i in range(len(words) - 1):
                a, b = words[i], words[i + 1]

                if not a or not b:
                    continue

                if (
                    re.fullmatch(r"\d+(?:\.\d+)?", a)
                    and
                    re.fullmatch(r"\d+(?:\.\d+)?", b)
                ):
                    continue

                pair = (a, b)

                if pair in seen:
                    duplicate_start = i
                    break

                seen[pair] = i

            if duplicate_start is None:
                break

            del parts[
                duplicate_start:
                duplicate_start + 2
            ]

        return TitleDeterministicRepair._clean(
            " ".join(parts)
        )

    @staticmethod
    def _quantity_semantics(text: str):
        text = TitleDeterministicRepair._clean(text).casefold()

        match = re.search(
            r"\b(\d+)\s*(pcs?|pieces?|sets?|packs?|pairs?)\b",
            text,
        )

        if not match:
            return None

        unit_map = {
            "pc": "piece",
            "pcs": "piece",
            "piece": "piece",
            "pieces": "piece",
            "set": "set",
            "sets": "set",
            "pack": "pack",
            "packs": "pack",
            "pair": "pair",
            "pairs": "pair",
        }

        return (
            int(match.group(1)),
            unit_map[match.group(2)],
        )

    @staticmethod
    def quantity_semantics_match(
        source_title: str,
        output_title: str,
    ) -> bool:
        source = TitleDeterministicRepair._quantity_semantics(
            source_title
        )
        output = TitleDeterministicRepair._quantity_semantics(
            output_title
        )

        # If the source has an explicit count+unit, the output must preserve
        # the same semantic unit. "5SET" must never become "5pcs".
        if source is not None:
            return output == source

        return True

    @staticmethod
    def _repair_specification_dominance(text: str) -> str:
        text=TitleDeterministicRepair._clean(text)
        if not text:return text
        unit=(r"mm|cm|m|in|inch|inches|v|kv|w|kw|a|ma|hz|khz|mhz|ghz|mah|ah|wh|kwh|bar|psi|pa|kpa|mpa|rpm|cc|ml|l|kg|g|lb|oz")
        num=r"\d+(?:\.\d+)?"
        pat=re.compile(rf"\b(?:{num}(?:\s*[xX×*]\s*{num}){{1,3}}\s*(?:{unit})|{num}\s*(?:{unit})?\s*/\s*{num}\s*(?:{unit})|{num}\s*(?:{unit}))\b",re.I)
        matches=list(pat.finditer(text)); remove=set()
        for i,c in enumerate(matches):
            for j,d in enumerate(matches):
                if i==j:continue
                if not SpecificationDominance.dominates(d.group(0),c.group(0)):continue
                a=SpecificationDominance.normalize(d.group(0));b=SpecificationDominance.normalize(c.group(0))
                if not a or not b:continue
                if a.family==b.family and a.values==b.values and a.unit==b.unit:
                    if j<i:remove.add((c.start(),c.end()));break
                else:
                    remove.add((c.start(),c.end()));break
        if not remove:return text
        chars=list(text)
        for s,e in remove:
            for k in range(s,e):chars[k]=" "
        return TitleDeterministicRepair._clean("".join(chars))

    @staticmethod
    def _repair_provable_range_containment(text: str) -> str:
        text=TitleDeterministicRepair._clean(text)
        if not text:return text
        scalar=re.compile(r"\b\d+(?:\.\d+)?\s*(?:V|kV|W|kW|A|mA|Hz|kHz|MHz|GHz|bar|psi|rpm)\b",re.I)
        matches=list(scalar.finditer(text));remove=[]
        range_pat=re.compile(r"(?:AC|DC)?\s*\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?\s*(?:V|kV|W|kW|A|mA|Hz|kHz|MHz|GHz|bar|psi|rpm)",re.I)
        ranges=[m.group(0) for m in range_pat.finditer(text)]
        for m in matches:
            cand=m.group(0)
            # if scalar is physically inside a range occurrence, keep it
            if any(m.start()>=rm.start() and m.end()<=rm.end() for rm in range_pat.finditer(text)):
                continue
            if any(SemanticContainment.dominates(r,cand) for r in ranges):
                remove.append((m.start(),m.end()))
        if not remove:return text
        chars=list(text)
        for s,e in remove:
            for k in range(s,e):chars[k]=" "
        return TitleDeterministicRepair._clean("".join(chars))

    @staticmethod
    def repair(title: str, target_language: str = "English") -> dict:
        original = TitleDeterministicRepair._clean(title)
        text = original
        changes = []

        for pattern in TitleDeterministicRepair.NOISE:
            updated = re.sub(
                pattern,
                "",
                text,
                flags=re.IGNORECASE,
            )
            updated = TitleDeterministicRepair._clean(updated)
            if updated != text:
                changes.append("remove_known_noise")
                text = updated

        updated = TitleDeterministicRepair._repair_specification_dominance(text)
        if updated != text:
            changes.append("remove_dominated_or_equivalent_specification")
            text = updated

        updated = TitleDeterministicRepair._repair_provable_range_containment(text)
        if updated != text:
            changes.append("remove_provably_contained_range_endpoint")
            text = updated

        updated = TitleDeterministicRepair._repair_compatibility_qualifier(
            text,
            target_language,
        )
        if updated != text:
            changes.append("merge_repeated_compatibility_qualifier")
            text = updated

        updated = TitleDeterministicRepair._dedupe_adjacent_words(text)
        if updated != text:
            changes.append("remove_adjacent_duplicate")
            text = updated

        updated = TitleDeterministicRepair._dedupe_repeated_bigram(text)
        if updated != text:
            changes.append("remove_adjacent_repeated_bigram")
            text = updated

        updated = TitleDeterministicRepair._dedupe_exact_code_tokens(text)
        if updated != text:
            changes.append("remove_repeated_identifier_or_spec")
            text = updated

        updated = TitleDeterministicRepair._dedupe_later_exact_bigram(text)
        if updated != text:
            changes.append("remove_later_repeated_bigram")
            text = updated

        # Earlier removals can create a new adjacent duplicate (for example
        # "FSA 60R FSA 86R" after repeated numeric-model removal).
        updated = TitleDeterministicRepair._dedupe_adjacent_words(text)
        if updated != text:
            changes.append("remove_adjacent_duplicate")
            text = updated

        words = text.split()
        while (
            words
            and words[-1].strip(" ,;:.").casefold()
            in TitleDeterministicRepair.ORPHAN_ENDINGS
        ):
            words.pop()
            changes.append("remove_orphan_low_value_ending")

        text = TitleDeterministicRepair._clean(" ".join(words))

        return {
            "version": TitleDeterministicRepair.VERSION,
            "original_title": original,
            "title": text,
            "changed": text != original,
            "changes": list(dict.fromkeys(changes)),
        }

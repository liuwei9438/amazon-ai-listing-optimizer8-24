from __future__ import annotations
import re
from typing import Any


class TitleFactGate:
    """V7 deterministic source-trace gate for title facts."""

    VERSION = "V7.0-source-trace-fact-gate"

    NOISE_PATTERNS = [
        r"measurement\s+(?:allowed\s+)?error",
        r"(?:allow|allowed)\s+(?:an?\s+)?error",
        r"manual\s+measurement",
        r"slightly\s+different",
        r"due\s+to\s+manual",
        r"please\s+allow",
        r"mainland\s+china",
    ]

    @staticmethod
    def _clean(value: Any) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _flatten(value: Any) -> list[str]:
        result = []
        if isinstance(value, str):
            text = TitleFactGate._clean(value)
            if text:
                result.append(text)
        elif isinstance(value, dict):
            for child in value.values():
                result.extend(TitleFactGate._flatten(child))
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                result.extend(TitleFactGate._flatten(child))
        return result

    @staticmethod
    def _source_text(profile: dict) -> str:
        ledger = profile.get("source_fact_ledger", {})
        if not isinstance(ledger, dict):
            ledger = {}
        snapshot = ledger.get("source_snapshot", {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        parts = [
            TitleFactGate._clean(snapshot.get("title", "")),
            *TitleFactGate._flatten(snapshot.get("bullets", [])),
            TitleFactGate._clean(snapshot.get("description", "")),
        ]
        return " ".join(x for x in parts if x)

    @staticmethod
    def _traceable(value: str, source: str) -> bool:
        value = TitleFactGate._clean(value)
        return bool(value and value.casefold() in source.casefold())

    @staticmethod
    def _noise_spec(value: str, source: str) -> bool:
        value = TitleFactGate._clean(value)
        if not value:
            return True
        for pattern in TitleFactGate.NOISE_PATTERNS:
            for match in re.finditer(pattern, source, flags=re.I):
                window = source[max(0, match.start()-100):min(len(source), match.end()+140)]
                if value.casefold() in window.casefold():
                    return True
        return False

    @staticmethod
    def build(profile: dict) -> dict:
        if not isinstance(profile, dict):
            profile = {}

        source = TitleFactGate._source_text(profile)
        strategy_input = profile.get("title_strategy_input", {})
        if not isinstance(strategy_input, dict):
            strategy_input = {}
        locked = strategy_input.get("locked", {})
        if not isinstance(locked, dict):
            locked = {}
        compatibility = strategy_input.get("compatibility_facts", {})
        if not isinstance(compatibility, dict):
            compatibility = {}
        candidate_facts = strategy_input.get("candidate_facts", {})
        if not isinstance(candidate_facts, dict):
            candidate_facts = {}

        approved, rejected, seen = [], [], set()

        def add(value, fact_type, priority, required=False, source_key=""):
            text = TitleFactGate._clean(value)
            if not text:
                return
            key = (fact_type, text.casefold())
            if key in seen:
                return
            seen.add(key)

            strict_trace = fact_type in {
                "MODEL", "PART_NUMBER", "COMPATIBILITY_MODEL",
                "COMPATIBILITY_BRAND", "SPECIFICATION",
            }
            traceable = (not strict_trace) or TitleFactGate._traceable(text, source)
            noisy = fact_type == "SPECIFICATION" and TitleFactGate._noise_spec(text, source)

            record = {
                "text": text, "type": fact_type, "priority": priority,
                "required": bool(required), "source_key": source_key,
                "source_traceable": bool(traceable),
                "noise_rejected": bool(noisy),
            }
            if traceable and not noisy:
                approved.append(record)
            else:
                record["rejection_reason"] = "source_noise" if noisy else "not_traceable_to_source"
                rejected.append(record)

        identity = locked.get("identity", {})
        if isinstance(identity, dict):
            add(identity.get("text", ""), "IDENTITY", "S", True, "locked.identity.text")

        models = locked.get("models", {})
        if isinstance(models, dict):
            add(models.get("primary", ""), "MODEL", "A", bool(models.get("primary")), "locked.models.primary")
            for x in models.get("secondary", []) or []:
                add(x, "MODEL", "B", False, "locked.models.secondary")
            for x in models.get("all", []) or []:
                add(x, "MODEL", "B", False, "locked.models.all")

        for x in compatibility.get("brands", []) or []:
            add(x, "COMPATIBILITY_BRAND", "A", True, "compatibility_facts.brands")
        for x in compatibility.get("models", []) or []:
            add(x, "COMPATIBILITY_MODEL", "A", False, "compatibility_facts.models")
        for x in compatibility.get("part_numbers", []) or []:
            add(x, "PART_NUMBER", "A", False, "compatibility_facts.part_numbers")
        for x in compatibility.get("important_compatibility", []) or []:
            t = TitleFactGate._clean(x)
            if re.fullmatch(r"[A-Za-z0-9._/+*-]+", t):
                add(t, "COMPATIBILITY_MODEL", "B", False, "compatibility_facts.important_compatibility")

        for field in ("source_specifications", "important_specifications", "specifications"):
            for x in candidate_facts.get(field, []) or []:
                add(x, "SPECIFICATION", "B", False, f"candidate_facts.{field}")

        return {
            "version": TitleFactGate.VERSION,
            "approved_facts": approved,
            "rejected_facts": rejected,
            "source_text_available": bool(source),
        }

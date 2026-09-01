from __future__ import annotations
import re
from typing import Any
from core.specification_dominance import SpecificationDominance
class SemanticContainment:
    VERSION="v1.0-provable-containment-only"
    @staticmethod
    def _clean(v:Any)->str:return re.sub(r"\s+"," ",str(v or "")).strip()
    @staticmethod
    def _num(v):
        try:
            n=float(v);return str(int(n)) if n.is_integer() else ("%f"%n).rstrip("0").rstrip(".")
        except Exception:return str(v).casefold()
    @staticmethod
    def _range(v):
        m=re.fullmatch(r"(?:ac|dc)?\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*(v|kv|w|kw|a|ma|hz|khz|mhz|ghz|bar|psi|rpm)",SemanticContainment._clean(v).casefold(),re.I)
        return (SemanticContainment._num(m.group(1)),SemanticContainment._num(m.group(2)),m.group(3).casefold()) if m else None
    @staticmethod
    def _scalar(v):
        m=re.fullmatch(r"(\d+(?:\.\d+)?)\s*(v|kv|w|kw|a|ma|hz|khz|mhz|ghz|bar|psi|rpm)",SemanticContainment._clean(v).casefold(),re.I)
        return (SemanticContainment._num(m.group(1)),m.group(2).casefold()) if m else None
    @staticmethod
    def dominates(a,b):
        if SpecificationDominance.dominates(a,b):return True
        r=SemanticContainment._range(a); s=SemanticContainment._scalar(b)
        return bool(r and s and r[2]==s[1] and s[0] in {r[0],r[1]})

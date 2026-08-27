from __future__ import annotations
import re
from typing import Any
class SourceCompatibilitySeriesExtractor:
    VERSION="v1.0-source-series-recovery"
    STOP_WORDS={"compatible","with","for","use","fits","fit","electric","e-scooter","scooter","chainsaw","printer","washing","machine","refrigerator","freezer","controller","brushless","accessories","accessory","parts","part","replacement","assembly","assy","motor","wheel","handle","cover","grip","front","rear","side","gas","models","model"}
    @staticmethod
    def _clean(v:Any)->str:return re.sub(r"\s+"," ",str(v or "")).strip(" ,;:")
    @staticmethod
    def _list(v):
        if not isinstance(v,(list,tuple,set)):return []
        out=[];seen=set()
        for x in v:
            t=SourceCompatibilitySeriesExtractor._clean(x);k=t.casefold()
            if t and k not in seen:seen.add(k);out.append(t)
        return out
    @staticmethod
    def _source_text(p):
        l=p.get("source_fact_ledger",{}) or {};s=l.get("source_snapshot",{}) or {};vals=[s.get("title",""),s.get("description","")]
        vals+=s.get("bullets",[]) if isinstance(s.get("bullets"),list) else []
        return " ".join(SourceCompatibilitySeriesExtractor._list(vals))
    @staticmethod
    def _supported(v,src):
        v=SourceCompatibilitySeriesExtractor._clean(v)
        return bool(v and re.search(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])",src,re.I))
    @staticmethod
    def extract(p):
        c=p.get("compatibility",{}) or {}; brands=SourceCompatibilitySeriesExtractor._list(c.get("brands",[])); notes=SourceCompatibilitySeriesExtractor._list(c.get("compatibility_notes",[])); src=SourceCompatibilitySeriesExtractor._source_text(p); rec=[];ev=[]
        for note in notes:
            m=re.search(r"\bmodels?\s*:?\s*([A-Za-z0-9][A-Za-z0-9._/+*\-\s,]{0,120})",note,re.I)
            if m:
                tail=re.split(r"\b(?:chainsaws?|printers?|machines?|scooters?|parts?|accessories?)\b",m.group(1),maxsplit=1,flags=re.I)[0]
                for token in re.split(r"\s*,\s*|\s+",tail):
                    token=SourceCompatibilitySeriesExtractor._clean(token)
                    if token and token.casefold() not in SourceCompatibilitySeriesExtractor.STOP_WORDS and SourceCompatibilitySeriesExtractor._supported(token,src) and (any(ch.isdigit() for ch in token) or len(token)>=3) and token not in rec:
                        rec.append(token);ev.append({"value":token,"evidence_type":"explicit_models_note","source_text":note})
        boundary=r"electric\s+scooter|e-scooter|scooter|chainsaw|printer|washing\s+machine|refrigerator|freezer|machine"
        for note in notes:
            for brand in brands:
                m=re.search(rf"\bcompatible\s+with\s+{re.escape(brand)}\s+(.+?)\s+(?:{boundary})\b",note,re.I)
                if not m:continue
                phrase=SourceCompatibilitySeriesExtractor._clean(m.group(1)); words=phrase.split()
                if 2<=len(words)<=5 and all(w.casefold() not in SourceCompatibilitySeriesExtractor.STOP_WORDS for w in words) and SourceCompatibilitySeriesExtractor._supported(phrase,src) and phrase not in rec:
                    rec.append(phrase);ev.append({"value":phrase,"evidence_type":"bounded_series_note","source_text":note})
        return {"version":SourceCompatibilitySeriesExtractor.VERSION,"recovered_models":rec,"evidence":ev}

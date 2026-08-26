from __future__ import annotations
import re
from typing import Any
from core.specification_dominance import SpecificationDominance
class TitleFactTraceabilityGate:
    VERSION="v1.1-compound-brand-source-equivalence"
    CRITICAL_TYPES={"QUANTITY","COMPATIBILITY_BRAND","MODEL","PART_NUMBER","COMPATIBILITY_MODEL","SPECIFICATION"}
    @staticmethod
    def _clean(v:Any)->str:return re.sub(r"\s+"," ",str(v or "")).strip()
    @staticmethod
    def _flatten(v):
        out=[]
        if isinstance(v,str):
            t=TitleFactTraceabilityGate._clean(v)
            if t:out.append(t)
        elif isinstance(v,dict):
            for x in v.values():out+=TitleFactTraceabilityGate._flatten(x)
        elif isinstance(v,(list,tuple,set)):
            for x in v:out+=TitleFactTraceabilityGate._flatten(x)
        return out
    @staticmethod
    def _parts(p):
        l=p.get("source_fact_ledger",{}) if isinstance(p,dict) else {};l=l if isinstance(l,dict) else {};s=l.get("source_snapshot",{}) if isinstance(l,dict) else {};s=s if isinstance(s,dict) else {}
        out=[]
        for path,val in (("source_snapshot.title",s.get("title","")),("source_snapshot.bullets",s.get("bullets",[])),("source_snapshot.description",s.get("description","")),("raw_fields",l.get("raw_fields",{}))):
            for t in TitleFactTraceabilityGate._flatten(val):out.append({"source_path":path,"text":t})
        return out
    @staticmethod
    def _compact(v):return re.sub(r"[^a-z0-9]+","",TitleFactTraceabilityGate._clean(v).casefold())
    @staticmethod
    def _q(v):
        m=re.search(r"\b(\d+)\s*(pcs?|pieces?|sets?|packs?|pairs?)\b",TitleFactTraceabilityGate._clean(v),re.I)
        if not m:return None
        mp={"pc":"piece","pcs":"piece","piece":"piece","pieces":"piece","set":"set","sets":"set","pack":"pack","packs":"pack","pair":"pair","pairs":"pair"}
        return int(m.group(1)),mp[m.group(2).casefold()]
    @staticmethod
    def trace_fact(text,typ,parts):
        text=TitleFactTraceabilityGate._clean(text);typ=TitleFactTraceabilityGate._clean(typ).upper()
        if typ=="QUANTITY":
            target=TitleFactTraceabilityGate._q(text)
            if not target:return None
            for p in parts:
                for m in re.finditer(r"\b\d+\s*(?:pcs?|pieces?|sets?|packs?|pairs?)\b",p["text"],re.I):
                    if TitleFactTraceabilityGate._q(m.group(0))==target:return {"source_path":p["source_path"],"source_text":m.group(0),"match_type":"quantity_semantic_equivalence"}
            return None
        if typ=="COMPATIBILITY_BRAND":
            f=text.casefold();c=TitleFactTraceabilityGate._compact(text)
            brand_tokens=re.findall(r"[A-Za-zÀ-ÿ0-9]+",text)
            compound_pattern=None
            if len(brand_tokens)>=2:
                sep=r"(?:\s+(?:for|and|&)\s+|\s+|[-_/,.]+\s*)"
                compound_pattern=re.compile(r"\b"+sep.join(re.escape(t) for t in brand_tokens)+r"\b",re.I)
            for p in parts:
                if f in p["text"].casefold():return {"source_path":p["source_path"],"source_text":text,"match_type":"exact_casefold"}
                if c and len(c)>=3 and c in TitleFactTraceabilityGate._compact(p["text"]):return {"source_path":p["source_path"],"source_text":text,"match_type":"punctuation_normalized_brand"}
                if compound_pattern:
                    m=compound_pattern.search(p["text"])
                    if m:return {"source_path":p["source_path"],"source_text":m.group(0),"match_type":"compound_brand_connector_equivalence"}
            return None
        if typ=="SPECIFICATION" and SpecificationDominance.normalize(text):
            unit=r"mm|cm|m|in|inch|inches|v|kv|w|kw|a|ma|hz|khz|mhz|ghz|mah|ah|wh|kwh|bar|psi|pa|kpa|mpa|rpm|cc|ml|l|kg|g|lb|oz";num=r"\d+(?:\.\d+)?"
            pat=re.compile(rf"\b(?:{num}(?:\s*[xX×*]\s*{num}){{1,3}}\s*(?:{unit})|{num}\s*(?:{unit})?\s*/\s*{num}\s*(?:{unit})|{num}\s*(?:{unit}))\b",re.I)
            for p in parts:
                for m in pat.finditer(p["text"]):
                    if SpecificationDominance.equivalent(text,m.group(0)):return {"source_path":p["source_path"],"source_text":m.group(0),"match_type":"specification_semantic_equivalence"}
        for p in parts:
            if text.casefold() in p["text"].casefold():return {"source_path":p["source_path"],"source_text":text,"match_type":"exact_casefold"}
        return None
    @staticmethod
    def validate(profile,composed):
        parts=TitleFactTraceabilityGate._parts(profile);used=(composed or {}).get("used_facts",[]) or [];aud=[];bad=[];seen=set()
        for f in used:
            if not isinstance(f,dict):continue
            typ=TitleFactTraceabilityGate._clean(f.get("type","")).upper()
            if typ not in TitleFactTraceabilityGate.CRITICAL_TYPES:continue
            text=TitleFactTraceabilityGate._clean(f.get("text") or f.get("full_text") or f.get("selected_text"))
            key=(typ,text.casefold())
            if not text or key in seen:continue
            seen.add(key);trace=TitleFactTraceabilityGate.trace_fact(text,typ,parts);rec={"fact_id":f.get("fact_id",""),"type":typ,"text":text,"traceable":trace is not None,"trace":trace or {}};aud.append(rec)
            if not trace:bad.append(rec)
        return {"version":TitleFactTraceabilityGate.VERSION,"status":"PASS" if not bad else "FAIL","audited_facts":aud,"untraceable_facts":bad,"errors":[] if not bad else ["UNTRACEABLE_CRITICAL_FACT"]}

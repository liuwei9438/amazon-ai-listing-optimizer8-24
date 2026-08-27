from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class NormalizedSpecification:
    raw:str; family:str; values:tuple[str,...]; unit:str; canonical:str

class SpecificationDominance:
    VERSION="v1.1-fallback-aware-structural-spec-dominance"
    _NUM=r"\d+(?:\.\d+)?"
    _UNIT=(r"mm|cm|m|in|inch|inches|v|kv|w|kw|a|ma|hz|khz|mhz|ghz|mah|ah|wh|kwh|bar|psi|pa|kpa|mpa|rpm|cc|ml|l|kg|g|lb|oz")
    @staticmethod
    def _clean(v:Any)->str: return re.sub(r"\s+"," ",str(v or "")).strip()
    @staticmethod
    def _num(v:str)->str:
        try:
            n=float(v)
            return str(int(n)) if n.is_integer() else ("%f"%n).rstrip("0").rstrip(".")
        except Exception: return v.casefold()
    @staticmethod
    def normalize(v:Any):
        raw=SpecificationDominance._clean(v)
        if not raw: return None
        c=raw.casefold().replace("×","x").replace("*","x")
        c=re.sub(r"\s+","",c)
        dim=re.fullmatch(rf"({SpecificationDominance._NUM}(?:x{SpecificationDominance._NUM}){{1,3}})({SpecificationDominance._UNIT})",c,re.I)
        if dim:
            vals=tuple(SpecificationDominance._num(x) for x in dim.group(1).split("x")); u=dim.group(2).casefold()
            return NormalizedSpecification(raw,"dimension",vals,u,"x".join(vals)+u)
        slash=re.fullmatch(rf"({SpecificationDominance._NUM})(?:({SpecificationDominance._UNIT}))?/({SpecificationDominance._NUM})({SpecificationDominance._UNIT})",c,re.I)
        if slash:
            lu=(slash.group(2) or slash.group(4)).casefold(); ru=slash.group(4).casefold()
            if lu==ru:
                vals=(SpecificationDominance._num(slash.group(1)),SpecificationDominance._num(slash.group(3)))
                return NormalizedSpecification(raw,"value_group",vals,ru,"/".join(vals)+ru)
        scalar=re.fullmatch(rf"({SpecificationDominance._NUM})({SpecificationDominance._UNIT})",c,re.I)
        if scalar:
            n=SpecificationDominance._num(scalar.group(1)); u=scalar.group(2).casefold()
            return NormalizedSpecification(raw,"scalar",(n,),u,n+u)
        return None
    @staticmethod
    def equivalent(a,b)->bool:
        x=SpecificationDominance.normalize(a); y=SpecificationDominance.normalize(b)
        return bool(x and y and x.family==y.family and x.unit==y.unit and x.values==y.values)
    @staticmethod
    def dominates(dominant,candidate)->bool:
        a=SpecificationDominance.normalize(dominant); b=SpecificationDominance.normalize(candidate)
        if not a or not b: return False
        if a.family==b.family and a.unit==b.unit and a.values==b.values: return True
        if a.unit!=b.unit: return False
        if a.family=="dimension" and b.family=="scalar" and b.values[0] in a.values: return True
        if a.family=="value_group" and b.family=="scalar" and b.values[0] in a.values: return True
        return False
    @staticmethod
    def filter_dominated_facts(facts:list[dict]):
        if not isinstance(facts,list): return [],[]
        specs=[f for f in facts if isinstance(f,dict) and str(f.get("type","")).upper()=="SPECIFICATION" and SpecificationDominance.normalize(f.get("full_text") or f.get("text"))]
        eq_remove=set(); fallback={}; audit=[]
        for c in specs:
            cid=str(c.get("fact_id","")); ct=c.get("full_text") or c.get("text"); cn=SpecificationDominance.normalize(ct)
            for p in specs:
                if p is c: continue
                pid=str(p.get("fact_id","")); pt=p.get("full_text") or p.get("text"); pn=SpecificationDominance.normalize(pt)
                if not pn or not SpecificationDominance.dominates(pt,ct): continue
                same=pn.family==cn.family and pn.values==cn.values and pn.unit==cn.unit
                if same:
                    pk=(int(p.get("order_index",999)),-float(p.get("value_score",0) or 0))
                    ck=(int(c.get("order_index",999)),-float(c.get("value_score",0) or 0))
                    if pk<ck:
                        eq_remove.add(cid); audit.append({"removed_fact_id":cid,"dominated_by_fact_id":pid,"relationship":"equivalent_duplicate"}); break
                else:
                    richness={"scalar":1,"value_group":2,"dimension":len(pn.values)}.get(pn.family,1)
                    cr={"scalar":1,"value_group":2,"dimension":len(cn.values)}.get(cn.family,1)
                    if richness>cr:
                        fallback[cid]=pid; audit.append({"fallback_fact_id":cid,"dominated_by_fact_id":pid,"relationship":"contained_fallback"}); break
        kept=[]
        for f in facts:
            fid=str(f.get("fact_id",""))
            if fid in eq_remove: continue
            kept.append({**f,"dominated_by_fact_id":fallback[fid]} if fid in fallback else f)
        return kept,audit

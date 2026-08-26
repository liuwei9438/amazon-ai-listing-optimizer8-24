from __future__ import annotations
import re
from typing import Any
class CompatibilityRelationshipMap:
    VERSION="v1.0-source-backed-brand-model-binding"
    @staticmethod
    def _clean(v:Any)->str:return re.sub(r"\s+"," ",str(v or "")).strip()
    @staticmethod
    def _list(v):
        if not isinstance(v,(list,tuple,set)):return []
        out=[];seen=set()
        for x in v:
            t=CompatibilityRelationshipMap._clean(x);k=t.casefold()
            if t and k not in seen:seen.add(k);out.append(t)
        return out
    @staticmethod
    def _source_parts(p):
        l=p.get("source_fact_ledger",{}) or {}; s=l.get("source_snapshot",{}) or {}; vals=[s.get("title",""),s.get("description","")]
        vals+=s.get("bullets",[]) if isinstance(s.get("bullets"),list) else []
        if isinstance(l.get("raw_fields"),dict): vals+=list(l["raw_fields"].values())
        return [CompatibilityRelationshipMap._clean(x) for x in vals if CompatibilityRelationshipMap._clean(x)]
    @staticmethod
    def build(p):
        b=p.get("brand_info",{}) or {}; c=p.get("compatibility",{}) or {}; ids=p.get("identifiers",{}) or {}
        brands=CompatibilityRelationshipMap._list(c.get("brands",[]) or b.get("third_party_brands",[]) or b.get("detected_brands",[]))
        models=CompatibilityRelationshipMap._list((c.get("models",[]) or [])+(ids.get("model_numbers",[]) or []))
        bindings={x:[] for x in brands}; evidence=[]; model_keys={m.casefold():m for m in models}
        for source in CompatibilityRelationshipMap._source_parts(p):
            clauses=[CompatibilityRelationshipMap._clean(x) for x in re.split(r"(?=\b\d+\)\s*)|[.;/]",source) if CompatibilityRelationshipMap._clean(x)]
            for clause in clauses:
                for brand in brands:
                    m=re.search(rf"(?:\bfor\s+)?{re.escape(brand)}\s*(?:Sewing\s+Machine\s+)?Models?\s*:\s*(.+)$",clause,re.I)
                    if not m:continue
                    seg=m.group(1)
                    for _,model in model_keys.items():
                        if re.search(rf"(?<![A-Za-z0-9]){re.escape(model)}(?![A-Za-z0-9])",seg,re.I) and model not in bindings[brand]:
                            bindings[brand].append(model); evidence.append({"brand":brand,"model":model,"source_text":clause,"evidence_type":"explicit_brand_models_clause"})
            if "/" in source and len(source)<=300 and "technical supports" not in source.casefold() and "compatibility this" not in source.casefold():
                for group in [CompatibilityRelationshipMap._clean(x) for x in source.split("/")]:
                    gb=[x for x in brands if re.search(rf"(?<![A-Za-z0-9]){re.escape(x)}(?![A-Za-z0-9])",group,re.I)]
                    if len(gb)!=1:continue
                    brand=gb[0]
                    for model in models:
                        if re.search(rf"(?<![A-Za-z0-9]){re.escape(model)}(?![A-Za-z0-9])",group,re.I) and model not in bindings[brand]:
                            bindings[brand].append(model); evidence.append({"brand":brand,"model":model,"source_text":group,"evidence_type":"slash_group"})
        bound={m.casefold() for xs in bindings.values() for m in xs}
        return {"version":CompatibilityRelationshipMap.VERSION,"bindings":bindings,"unbound_models":[m for m in models if m.casefold() not in bound],"evidence":evidence,"has_multi_brand_binding":sum(1 for v in bindings.values() if v)>=2}

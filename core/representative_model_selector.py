from __future__ import annotations
import re
from typing import Any
class RepresentativeModelSelector:
    VERSION="v1.0-source-backed-representative-ranking"
    SOURCE_WEIGHTS={"locked.models.primary":100,"compatibility_facts.part_numbers":94,"compatibility_facts.models":92,"locked.models.secondary":88,"locked.models.all":82,"compatibility_facts.important_compatibility":78}
    @staticmethod
    def _clean(v:Any)->str:return re.sub(r"\s+"," ",str(v or "")).strip()
    @staticmethod
    def _pos(v,src):
        v=RepresentativeModelSelector._clean(v);src=RepresentativeModelSelector._clean(src)
        if not v or not src:return 9999
        m=re.search(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])",src,re.I);return m.start() if m else 9999
    @staticmethod
    def annotate(facts,source_title):
        facts=facts if isinstance(facts,list) else []; out=[]
        model_facts=[f for f in facts if isinstance(f,dict) and RepresentativeModelSelector._clean(f.get("type","")).upper() in {"MODEL","PART_NUMBER","COMPATIBILITY_MODEL"}]
        groups={}
        for f in model_facts:groups.setdefault(RepresentativeModelSelector._clean(f.get("source_key","")),[]).append(f)
        bonus={}
        for _,g in groups.items():
            ordered=sorted(g,key=lambda f:(RepresentativeModelSelector._pos(f.get("text",""),source_title),str(f.get("fact_id","")))); total=len(ordered)
            for i,f in enumerate(ordered):bonus[f.get("fact_id")]=min(3,max(0,total-i-1))
        for f in facts:
            if not isinstance(f,dict):continue
            typ=RepresentativeModelSelector._clean(f.get("type","")).upper()
            if typ not in {"MODEL","PART_NUMBER","COMPATIBILITY_MODEL"}:out.append(f);continue
            sk=RepresentativeModelSelector._clean(f.get("source_key","")); base=RepresentativeModelSelector.SOURCE_WEIGHTS.get(sk,int(f.get("priority",0) or 0))
            if f.get("required"):base=max(base,100)
            if RepresentativeModelSelector._clean(f.get("relationship_brand","")):base+=3
            base+=bonus.get(f.get("fact_id"),0)
            out.append({**f,"representative_model_score":base,"source_title_position":RepresentativeModelSelector._pos(f.get("text",""),source_title)})
        return out

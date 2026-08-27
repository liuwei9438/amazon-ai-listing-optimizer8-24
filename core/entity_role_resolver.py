from __future__ import annotations
import re
from typing import Any
class EntityRoleResolver:
    VERSION="v1.0-brand-model-evidence-arbitration"
    @staticmethod
    def _clean(v:Any)->str:return re.sub(r"\s+"," ",str(v or "")).strip()
    @staticmethod
    def _dict(v):return v if isinstance(v,dict) else {}
    @staticmethod
    def _list(v):
        if not isinstance(v,(list,tuple,set)): return []
        out=[];seen=set()
        for x in v:
            t=EntityRoleResolver._clean(x); k=t.casefold()
            if t and k not in seen: seen.add(k);out.append(t)
        return out
    @staticmethod
    def _keys(vs):return {EntityRoleResolver._clean(v).casefold() for v in vs if EntityRoleResolver._clean(v)}
    @staticmethod
    def model_evidence(p):
        ids=EntityRoleResolver._dict(p.get("identifiers")); c=EntityRoleResolver._dict(p.get("compatibility")); fl=EntityRoleResolver._dict(p.get("fact_lock")); n=EntityRoleResolver._dict(p.get("normalized_knowledge")); nm=EntityRoleResolver._dict(n.get("models"))
        vals=[]
        for k in ("model_numbers","part_numbers","series_numbers","unknown_codes"): vals+=EntityRoleResolver._list(ids.get(k,[]))
        for k in ("models","part_numbers"): vals+=EntityRoleResolver._list(c.get(k,[]))
        for k in ("compatible_models","part_numbers"): vals+=EntityRoleResolver._list(fl.get(k,[]))
        vals+=EntityRoleResolver._list(nm.get("all",[]))+EntityRoleResolver._list(nm.get("secondary",[]))
        if EntityRoleResolver._clean(nm.get("primary","")): vals.append(EntityRoleResolver._clean(nm.get("primary")))
        return EntityRoleResolver._keys(vals)
    @staticmethod
    def strong_brand_evidence(p):
        c=EntityRoleResolver._dict(p.get("compatibility")); b=EntityRoleResolver._dict(p.get("brand_info")); k=EntityRoleResolver._dict(p.get("product_knowledge")); r=EntityRoleResolver._dict(k.get("relationship"))
        return EntityRoleResolver._keys(EntityRoleResolver._list(c.get("brands",[]))+EntityRoleResolver._list(b.get("third_party_brands",[]))+EntityRoleResolver._list(r.get("brands",[])))
    @staticmethod
    def detected_brand_evidence(p): return EntityRoleResolver._keys(EntityRoleResolver._list(EntityRoleResolver._dict(p.get("brand_info")).get("detected_brands",[])))
    @staticmethod
    def looks_identifier_like(v):
        v=EntityRoleResolver._clean(v)
        return bool(v and ((any(c.isalpha() for c in v) and any(c.isdigit() for c in v)) or re.fullmatch(r"[A-Za-z0-9]+(?:[._/+][A-Za-z0-9]+)+",v)))
    @staticmethod
    def brand_candidate_decision(candidate,p):
        c=EntityRoleResolver._clean(candidate); key=c.casefold()
        if not c:return {"accepted":False,"reason":"EMPTY"}
        models=EntityRoleResolver.model_evidence(p); strong=EntityRoleResolver.strong_brand_evidence(p); detected=EntityRoleResolver.detected_brand_evidence(p); shape=EntityRoleResolver.looks_identifier_like(c)
        if key in strong:return {"accepted":True,"reason":"STRONG_BRAND_EVIDENCE","model_collision":key in models,"identifier_shape":shape}
        if key in models:return {"accepted":False,"reason":"MODEL_EVIDENCE_OUTRANKS_WEAK_BRAND_INFERENCE","model_collision":True,"identifier_shape":shape}
        if shape and key not in detected:return {"accepted":False,"reason":"IDENTIFIER_SHAPE_WITHOUT_BRAND_CORROBORATION","model_collision":False,"identifier_shape":True}
        return {"accepted":True,"reason":"DETECTED_BRAND_CORROBORATION" if key in detected else "NO_ROLE_CONFLICT","model_collision":False,"identifier_shape":shape}

# -*- coding: utf-8 -*-
"""pt.matcher —— 模块 4：AI 分类匹配（编号统一见 2026-10-12 批复：
4=AI 匹配 / 5=验证与 Browse Node / 6=Schema 与属性 / 7=批量任务 / 8=UI）。

AI 只做一件事：**从模块 3 检索出的真实 API 候选里选一个（或不选）**。
铁律（模块 4 批复 11 条）：
1. 只能选真实 API 返回的 Product Type 候选——AI 返回候选集之外的
   PT 一律拒绝采信（编造=拒绝），更没有「PRODUCT」兜底。
2. 不允许编造 Product Type：chosen 必须逐字等于某个候选 pt_name。
3. 综合判断：产品身份、功能、用途、适配设备（辅助）一起喂给 AI；
   兼容品牌绝不当作商品品牌参与判断（material.auxiliary_signals 已隔离）。
4. 不能仅凭关键词重叠定分类：词重叠只作语义一致性分量之一，
   决策主体是 AI 理由 + 证据结构 + 候选间差距的合成。
5. 输出候选评分、匹配理由、置信度、待复核状态（MatchResult）。
6. 置信度 ≠ AI 自报概率：confidence = 证据强度×0.35 + 语义一致性×0.30
   + 候选间差距×0.20 + AI 自报×0.15（自报只占小头且可为 None）。
7. 候选不足（0 个/仅 1 个无法对比）或证据冲突（证据最优≠AI 所选且
   分差小于 CONFLICT_EPS）→ needs_review=True 进人工复核。
8. AI 选择绝不等于官方验证：MatchResult.official_verified 恒 False，
   验证属模块 5；本模块不写 store、不碰验证状态。
9. 不改上传 / worker / AI 理解 / 图片模块（零 import，纯函数）。
10. 通用阈值常量在文件头（REVIEW_THRESHOLD/CONFLICT_EPS/EVIDENCE_FULL），
    绝不针对单个商品硬编码分类规则。
11. AI 决策器由装配期注入（ai_decide 回调）：本模块不联网、不调模型，
    Mock/真实 GPT 对它只是不同的回调实现；候选来源环境
    （CandidatesResult.env_mode）原样透传，mock 环境匹配结果绝不冒充
    live 官方流程。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from pt.candidates import Candidate, CandidatesResult
from pt.material import ProductMaterial

# ---- 通用阈值（与商品无关；调优改这里，不许加 if 某商品） ----
REVIEW_THRESHOLD = 0.55   # 综合置信度低于它 → 人工复核
CONFLICT_EPS = 0.5        # 证据最优与 AI 所选分差小于它 → 视为证据冲突
EVIDENCE_FULL = 5.0       # 候选证据满分（S1+S2 满额；S3 弱证据天然低分）
W_EVIDENCE, W_SEMANTIC, W_GAP, W_AI = 0.35, 0.30, 0.20, 0.15
_AI_NONE_PLACEHOLDER = 0.5    # AI 不自报概率时该分量取中性值

_CJK_RE = re.compile(r"[一-鿿]")
AiDecide = Callable[[dict], dict]
"""ai_decide(payload) -> {"chosen": pt_name|None,
                          "reasons": {pt_name: 理由文本},
                          "self_confidence": 0..1|None}
真实实现（GPT API）在装配层注入；测试注入脚本化假决策器。"""


@dataclass
class MatchResult:
    """模块 4 输出：一次匹配的完整决策记录（只读结果，不落库）。"""
    pid: str
    marketplace_id: str
    env_mode: str                       # 候选来源环境（mock|live），透传
    chosen: str | None = None           # AI 选中的 PT（必属候选集）
    confidence: float = 0.0             # 综合置信度 0..1
    confidence_breakdown: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    # 每项：pt_name/display_name/evidence_score/semantic_score/final_score/
    #       ai_reason/chosen(bool)
    needs_review: bool = True           # 缺省进复核，明确够格才放行
    review_reasons: list[str] = field(default_factory=list)
    official_verified: bool = False     # 恒 False（验证属模块 5，铁律 8）


def _tokens(text: str) -> set[str]:
    """与 material/candidates 同法分词（拉丁词 + 中文 2 字滑窗，小写）。"""
    out = {w.lower() for w in
           re.findall(r"[A-Za-z0-9][\w\-\+\.]*", str(text or ""))
           if len(w) >= 2}
    cjk = "".join(_CJK_RE.findall(str(text or "")))
    for i in range(len(cjk) - 1):
        out.add(cjk[i:i + 2])
    return out


def _semantic_terms(material: ProductMaterial) -> set[str]:
    """语义一致性用词：本体身份/型号/用途场景/适配设备（辅助）。
    **不含兼容品牌**（铁律 3：兼容品牌绝不参与商品语义）。"""
    aux = material.auxiliary_signals()
    parts = [material.product_identity, material.model,
             material.basic_info_product_type,
             " ".join(material.body_terms()),
             " ".join(aux["usage_scenarios"]),
             " ".join(f["value"] for f in aux["fits"])]
    terms: set[str] = set()
    for p in parts:
        terms |= _tokens(p)
    return terms


def _semantic_score(terms: set[str], cand: Candidate) -> float:
    """候选 PT 显示名/名 itself 与本体语义词的重叠率（0..1）。"""
    pt_tokens = _tokens(
        (cand.display_name or "") + " " +
        (cand.pt_name or "").replace("_", " "))
    if not terms or not pt_tokens:
        return 0.0
    overlap = terms & pt_tokens
    return round(min(len(overlap) / min(len(terms), 4), 1.0), 4)


def build_payload(material: ProductMaterial,
                  cands: CandidatesResult) -> dict:
    """喂给 AI 的决策载荷（铁律 3：身份/功能/用途/适配一起给，
    兼容品牌带禁用说明；候选带证据来源）。"""
    aux = material.auxiliary_signals()
    return {
        "product": {
            "pid": material.pid,
            "identity": material.product_identity,
            "identity_source": material.identity_source,
            "model": material.model,
            "title": material.title,
            "bullets": material.bullets[:5],
            "usage_scenarios": aux["usage_scenarios"],
            "fitment": aux["fits"],            # 适配设备/型号（辅助）
            "compatible_brands": aux["compatible_brands"],
            "compatible_brands_note": aux["note"],
            "language_guess": material.language_guess,
        },
        "candidates": [{
            "pt_name": c.pt_name,
            "display_name": c.display_name,
            "strategies": sorted(c.sources.keys()),
            "evidence": {k: {kk: vv for kk, vv in v.items()
                             if kk in ("asins", "classification_ids",
                                       "keywords", "item_name")}
                         for k, v in c.sources.items()},
        } for c in cands.candidates],
        "env_mode": cands.env_mode,
        "instruction": "只能从 candidates 里选 pt_name；都不合适返回"
                       " null；禁止编造新的 Product Type",
    }


def _validate_ai(ai_out: "dict | None", valid: set[str]
                 ) -> tuple[str | None, dict[str, str], float | None,
                            list[str]]:
    """校验 AI 输出：chosen 必属候选集；reasons 只留已知 PT；
    self_confidence 收敛到 0..1 或 None。返回 (chosen, reasons,
    self_confidence, problems)。"""
    problems: list[str] = []
    if not isinstance(ai_out, dict):
        return None, {}, None, ["AI 输出不是对象"]
    chosen = ai_out.get("chosen")
    if chosen is not None:
        chosen = str(chosen).strip()
        if chosen not in valid:
            problems.append(
                f"AI 返回了候选集之外的 PT：{chosen!r}（拒绝采信，绝不"
                "编造/兜底）")
            chosen = None
    raw_reasons = ai_out.get("reasons") or {}
    reasons = {str(k): str(v)[:300] for k, v in raw_reasons.items()
               if isinstance(raw_reasons, dict) and str(k) in valid} \
        if isinstance(raw_reasons, dict) else {}
    sc = ai_out.get("self_confidence")
    if sc is not None:
        try:
            sc = min(max(float(sc), 0.0), 1.0)
        except (TypeError, ValueError):
            sc = None
    return chosen, reasons, sc, problems


def match_product(material: ProductMaterial, cands: CandidatesResult,
                  ai_decide: AiDecide, *, marketplace_id: str
                  ) -> MatchResult:
    """纯函数：同输入同输出。不落库、不联网、不改传入对象。"""
    result = MatchResult(pid=material.pid, marketplace_id=marketplace_id,
                         env_mode=cands.env_mode)
    cand_list = list(cands.candidates)
    valid = {c.pt_name for c in cand_list}
    reasons: dict[str, str] = {}
    self_conf: float | None = None
    ai_called = False

    # ---- 候选不足（铁律 7）：0 个直接复核；1 个无法对比也复核 ----
    if not cand_list:
        result.review_reasons.append("候选为空（无可选 PT）")
        return result
    if len(cand_list) == 1:
        result.review_reasons.append("仅单一候选，无从对比（候选不足）")

    # ---- 语义一致性（铁律 3/4：排除兼容品牌） ----
    sem_terms = _semantic_terms(material)

    # ---- AI 决策（铁律 1/2：只认候选集内） ----
    payload = build_payload(material, cands)
    problems: list[str] = []
    chosen: str | None = None
    try:
        ai_out = ai_decide(payload)
        chosen, reasons, self_conf, problems = _validate_ai(ai_out, valid)
        result.review_reasons.extend(problems)
    except Exception as e:  # noqa: BLE001 —— AI 层任何异常都不打断流程
        chosen = None
        result.review_reasons.append(
            f"AI 决策失败：{type(e).__name__}（进人工复核）")
    if chosen is None and not problems and not result.review_reasons:
        result.review_reasons.append("AI 未给出选择（返回 null）")

    # ---- 评分与置信度（铁律 5/6） ----
    ev = {c.pt_name: min((c.score or 0.0) / EVIDENCE_FULL, 1.0)
          for c in cand_list}
    sem = {c.pt_name: _semantic_score(sem_terms, c) for c in cand_list}
    ranked = sorted(cand_list,
                    key=lambda c: (-(ev[c.pt_name] + sem[c.pt_name]),
                                   c.pt_name))
    for c in ranked:
        final = round(0.6 * ev[c.pt_name] + 0.4 * sem[c.pt_name], 4)
        result.candidates.append({
            "pt_name": c.pt_name,
            "display_name": c.display_name,
            "strategies": sorted(c.sources.keys()),
            "evidence_score": round(ev[c.pt_name], 4),
            "semantic_score": sem[c.pt_name],
            "final_score": final,
            "ai_reason": reasons.get(c.pt_name, ""),
            "chosen": c.pt_name == chosen,
        })
    result.chosen = chosen

    if chosen is not None:
        others = [ev[c.pt_name] for c in cand_list if c.pt_name != chosen]
        best_other = max(others) if others else 0.0
        gap = max(ev[chosen] - best_other, 0.0)
        ai_part = self_conf if self_conf is not None \
            else _AI_NONE_PLACEHOLDER
        result.confidence = round(
            W_EVIDENCE * ev[chosen] + W_SEMANTIC * sem[chosen]
            + W_GAP * gap + W_AI * ai_part, 4)
        result.confidence_breakdown = {
            "evidence": round(ev[chosen], 4),
            "semantic": sem[chosen],
            "gap": round(gap, 4),
            "ai_self": self_conf,
            "weights": {"evidence": W_EVIDENCE, "semantic": W_SEMANTIC,
                        "gap": W_GAP, "ai": W_AI},
        }
        # ---- 证据冲突（铁律 7）：证据最优≠AI 所选且分差小 ----
        best_ev_pt = max(cand_list, key=lambda c: (ev[c.pt_name],
                                                   c.pt_name)).pt_name
        if best_ev_pt != chosen and \
                (ev[best_ev_pt] - ev[chosen]) < CONFLICT_EPS:
            result.review_reasons.append(
                f"证据冲突：证据最优 {best_ev_pt} 与 AI 选择 {chosen} "
                f"分差 {ev[best_ev_pt] - ev[chosen]:.2f} < {CONFLICT_EPS}")
        if result.confidence < REVIEW_THRESHOLD:
            result.review_reasons.append(
                f"置信度 {result.confidence} 低于复核阈值"
                f" {REVIEW_THRESHOLD}")
    else:
        result.confidence = 0.0

    result.needs_review = bool(result.review_reasons)
    return result

# -*- coding: utf-8 -*-
"""pt.matcher —— 模块 4：AI 分类匹配（编号统一见 2026-10-12 批复：
4=AI 匹配 / 5=验证与 Browse Node / 6=Schema 与属性 / 7=批量任务 / 8=UI）。

AI 只做一件事：**从模块 3 检索出的真实 API 候选里选一个（或不选）**。
铁律（模块 4 批复 11 条 + 2026-10-12 第二轮批复修正 2/3）：
1. 只能选真实 API 返回的 Product Type 候选——AI 返回候选集之外的
   PT 一律拒绝采信（编造=拒绝），更没有「PRODUCT」兜底。
2. 不允许编造 Product Type：chosen 必须逐字等于某个候选 pt_name。
3. 综合判断：产品身份、功能、用途、适配设备（辅助）一起喂给 AI；
   兼容品牌绝不当作商品品牌参与判断（material.auxiliary_signals 已隔离）。
4. 不能仅凭关键词重叠定分类：词重叠只作语义一致性分量之一，
   决策主体是 AI 理由 + 证据结构 + 候选间差距的合成。
5. 输出候选评分、匹配理由、置信度、待复核状态（MatchResult）。
6. 置信度 ≠ AI 自报概率，也 **不是真实准确概率——只是启发式匹配评分**
   （修正 2）：confidence = 证据强度×0.35 + 语义一致性×0.30 +
   候选间差距×0.20 + AI 自报×0.15。启发式分再高也跨不过绝对质量
   门槛（ABS_MIN_EVIDENCE / ABS_MIN_SEMANTIC，集中常量，与商品无关）。
7. 复核门：候选为空直接复核；证据最优≠AI 所选且分差小于 CONFLICT_EPS
   → 证据冲突复核；综合置信度低于 REVIEW_THRESHOLD → 低置信复核。
8. AI 选择绝不等于官方验证：MatchResult.official_verified 恒 False，
   验证属模块 5；本模块不写 store、不碰验证状态。
9. 不改上传 / worker / AI 理解 / 图片模块（零 import，纯函数）。
10. 通用阈值常量在文件头（REVIEW_THRESHOLD/CONFLICT_EPS/EVIDENCE_FULL/
    ABS_MIN_EVIDENCE/ABS_MIN_SEMANTIC），绝不针对单个商品硬编码分类规则。
11. AI 决策器由装配期注入（ai_decide 回调）：本模块不联网、不调模型，
    Mock/真实 GPT 对它只是不同的回调实现；候选来源环境
    （CandidatesResult.env_mode）原样透传，mock 环境匹配结果绝不冒充
    live 官方流程。
12. 绝对质量门槛（修正 2，防「全员不合适仍选最高分」）：证据与语义
    双下限。**全部候选不达标 → status="all_candidates_unsuitable"，
    不强行匹配最高分**（此时连 AI 都不必问）；AI 所选不达标而其他
    候选达标 → 不采纳该选择，转人工。
13. 单候选（修正 3）：证据充分且语义一致（过双门槛）且置信度达标的
    单一候选允许自动进入下一步官方验证（status="matched"，
    needs_review=False；official_verified 仍恒 False）；不达标才转
    人工复核——「仅单一候选无从对比」不再一票否决。
14. P2 语义评分优化（2026-10-16 批准令任务一）：字面语义分
    （_semantic_score，保留不动）之外增加两类**独立语义证据**，
    语义门统一取 max(字面, S3 官方目录, GPT 裁决)——阈值常量
    ABS_MIN_EVIDENCE/ABS_MIN_SEMANTIC/REVIEW_THRESHOLD 一律不变：
    a) S3 官方目录身份一致性证据（s3_semantic_score）：官方目录里
       ≥2 个与本体词一致且同站点的相似商品挂该 PT = 官方背书的语义
       证据（可信条件见函数 docstring；与证据分的关系=回答不同问题，
       且互不单独放行）；
    b) 受限 GPT 语义裁决（ai_semantic 回调，装配期注入，可缺省）：
       仅当（不含它）全员不达标、且有候选 证据≥0.4 且字面语义=0 时
       问一次「商品是否属于该类型」；确认只把语义抬到下限
       （ABS_MIN_SEMANTIC），拒绝/低置信/异常一律维持门槛（fail-
       closed）。GPT 只解释语义关系，**不代替 PTD 官方有效性验证与
       Browse Node 证据验证**（模块 5 门槛原样在后头）。
15. P2 安全加固（2026-10-16 批准令·防错误分类自动放行）：
    a) **GPT-only 语义不自动确认**：候选的语义分若只有 GPT 裁决一个
       来源（字面=0 且 S3 官方目录=0——裁决资格本身就排除了其他来源），
       它的「合适」只是 AI 单方判断、缺乏独立语义证据 → 即使证据分与
       置信度都过线，也只作**人工复核辅助信号**：保留 chosen 推荐、
       强制 needs_review，绝不 status=matched。
    b) 证据依赖可追踪（EVIDENCE_DEPENDENCY）：每类检索证据喂了哪些
       评分维度一表可查；S3 同时喂证据分（弱权重，度量「是否召回」）
       与语义分（度量「官方目录同类是不是这个 PT」）——同一 API 响应
       回答两个不同问题，且纯 S3 语义恒 0、双门槛都过才算 suitable，
       结构上保证任何单一证据不能被当成两份独立证据放行。
    c) 原始三源评分（lexical/S3/GPT）在 semantic_detail 与
       confidence_breakdown 原样保留，合成分绝不掩盖原始证据。
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
ABS_MIN_EVIDENCE = 0.4    # 修正 2：候选证据绝对下限（≈至少 S2 命中 2.0/5.0）
ABS_MIN_SEMANTIC = 0.25   # 修正 2：语义绝对下限（官方 PT 名至少对上一个本体词）
# ---- P2（铁律 14a）：S3 官方目录身份一致性语义证据参数 ----
S3_SEM_MIN_ASINS = 2      # 最低采信相似商品数（单个=检索噪音；与 BN 证据
                          # 「≥2 官方商品引用节点」同一先例）
S3_SEM_CAP = 0.75         # 语义值上限——官方目录相似=强而间接的证据，
                          # 永不冒充满值语义（字面重叠才可能到 1.0）
W_EVIDENCE, W_SEMANTIC, W_GAP, W_AI = 0.35, 0.30, 0.20, 0.15
_AI_NONE_PLACEHOLDER = 0.5    # AI 不自报概率时该分量取中性值
# ---- P2 加固（铁律 15b）：证据来源→评分维度依赖表（可追踪、可审计）----
EVIDENCE_DEPENDENCY = {
    "s1_identity_item_name": {"evidence_score": True, "semantic": False},
    "s2_keywords": {"evidence_score": True, "semantic": False},
    "s3_catalog_similar": {
        "evidence_score": True,       # 弱权重 1.0+0.1n（度量「是否召回」）
        "semantic": "仅当另有 S1/S2 伴随（s3_semantic_score，度量「官方"
                    "目录同类商品是不是这个 PT」）——纯 S3 语义恒 0"},
    "gpt_semantic_verdict": {
        "evidence_score": False, "semantic": True,
        "note": "确认恰抬到语义下限；GPT-only 抬分不自动确认（铁律 15a）"},
}
_HEURISTIC_NOTE = ("confidence 只是启发式匹配评分，不是真实准确概率；"
                   "绝对质量门槛见 abs_gate（修正 2）")

_CJK_RE = re.compile(r"[一-鿿]")
AiDecide = Callable[[dict], dict]
"""ai_decide(payload) -> {"chosen": pt_name|None,
                          "reasons": {pt_name: 理由文本},
                          "self_confidence": 0..1|None}
真实实现（GPT API）在装配层注入；测试注入脚本化假决策器。"""


@dataclass
class MatchResult:
    """模块 4 输出：一次匹配的完整决策记录（只读结果，不落库）。

    status：matched（可自动进入下一步官方验证）| review（有人采信的
    chosen 或无 chosen，转人工）| no_candidates（空候选）|
    all_candidates_unsuitable（修正 2/3：全员未达绝对门槛，不强行匹配）。
    chosen 只在被采纳时非空（必属候选集且过双门槛）；candidates[].ai_picked
    记录 AI 原始选择供人工参考（被门槛拒绝时 ai_picked 与 chosen 可不一致）。
    """
    pid: str
    marketplace_id: str
    env_mode: str                       # 候选来源环境（mock|live），透传
    chosen: str | None = None           # 被采纳的 PT（None=未采纳/未匹配）
    status: str = "review"              # 见上；缺省复核，明确够格才放行
    confidence: float = 0.0             # 启发式评分 0..1（不是准确概率）
    confidence_breakdown: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    # 每项：pt_name/display_name/strategies/evidence_score/semantic_score/
    #       final_score/suitable/ai_reason/ai_picked(bool)
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


def semantic_terms(material: ProductMaterial) -> set[str]:
    """公开入口：_semantic_terms（模块 5 验证层复用同一语义配方，
    保证「匹配用的语义」与「验证用的语义」是同一套，不另立标准）。"""
    return _semantic_terms(material)


def _semantic_score(terms: set[str], cand: Candidate) -> float:
    """候选 PT 显示名/名 itself 与本体语义词的重叠率（0..1）。

    P2（铁律 14）：本函数=字面语义分，**保留原样不动**（批准令任务
    一.2）；新增证据只在外层取 max，不改本公式。"""
    pt_tokens = _tokens(
        (cand.display_name or "") + " " +
        (cand.pt_name or "").replace("_", " "))
    if not terms or not pt_tokens:
        return 0.0
    overlap = terms & pt_tokens
    return round(min(len(overlap) / min(len(terms), 4), 1.0), 4)


_S3 = "s3_catalog_similar"
_S3_COMPANIONS = ("s1_identity_item_name", "s2_keywords")


def s3_semantic_score(cand: Candidate, env_mode: str) -> float:
    """S3 官方目录身份一致性语义证据（P2 铁律 14a，0..S3_SEM_CAP）。

    可信条件（全部满足才算数，缺一即 0）：
    - env live：mock 检索结果绝不冒充官方语义（铁律 5/29）；
    - 候选另有 S1/S2 检索证据伴随：防同一 S3 响应把证据/语义两道门
      一起顶开（任务一.4）——纯 S3 候选证据分上限 ≈(1+0.1n)/5 本就
      过不了 0.4 证据门，语义门同样不认它单独说话；
    - S3 采信商品数 ≥S3_SEM_MIN_ASINS：采信=candidates.s3 已过身份
      一致性核验（本体词重叠+本站点 PT），单个相似商品可能是检索
      噪音。
    值 = S3_SEM_CAP × min((n-1)/3, 1)：2 个=0.25（恰好语义下限）、
    3 个=0.5、≥4 个=0.75 封顶。
    与证据分的关系：证据分度量「官方检索是否**召回**该 PT」（S3 分量
    1.0+0.1n，弱权重）；本函数度量「官方目录里与本体**一致的同类商品**
    是不是这个 PT」——同一 API 响应回答两个不同问题，且任何单一证据
    都不能同时独立打开两道门（双门槛都过才算 suitable）。"""
    if env_mode != "live":
        return 0.0
    n = len((cand.sources.get(_S3) or {}).get("asins") or [])
    if n < S3_SEM_MIN_ASINS:
        return 0.0
    if not any(s in cand.sources for s in _S3_COMPANIONS):
        return 0.0
    return round(S3_SEM_CAP * min((n - 1) / 3.0, 1.0), 4)


def build_semantic_payload(material: ProductMaterial, cands: CandidatesResult,
                           eligible: list[Candidate]) -> dict:
    """受限 GPT 语义裁决载荷（P2 铁律 14b，任务一.5/6）：只问「商品
    是否属于该类型」，绝不选最终分类、不代替官方验证。"""
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
            "fitment": aux["fits"],
            "compatible_brands_note": aux["note"],
            "language_guess": material.language_guess,
        },
        "candidates": [{
            "pt_name": c.pt_name,
            "display_name": c.display_name,
            "strategies": sorted(c.sources.keys()),
        } for c in eligible],
        "env_mode": cands.env_mode,
        "instruction": "只判断商品与各候选 Product Type 的语义归属"
                       "（match + confidence + 一句话理由）；不选择最终"
                       "分类，不代替 PTD 官方有效性验证与 Browse Node "
                       "证据验证",
    }


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
                  ai_decide: AiDecide, *, marketplace_id: str,
                  ai_semantic: AiDecide | None = None,
                  ) -> MatchResult:
    """纯函数：同输入同输出。不落库、不联网、不改传入对象。

    ai_semantic（P2 铁律 14b，可缺省）：受限语义裁决回调——仅当
    （不含它）全员不达标时对「证据≥0.4 且字面语义=0」的候选问一次；
    确认只把语义抬到 ABS_MIN_SEMANTIC，绝不更高、绝不代替官方验证。"""
    result = MatchResult(pid=material.pid, marketplace_id=marketplace_id,
                         env_mode=cands.env_mode)
    cand_list = list(cands.candidates)
    if not cand_list:
        result.status = "no_candidates"
        result.review_reasons.append("候选为空（无可选 PT）")
        return result

    valid = {c.pt_name for c in cand_list}

    # ---- 评分（铁律 5 + P2 铁律 14）：证据 / 语义（max 三源）逐候选 ----
    sem_terms = _semantic_terms(material)
    ev = {c.pt_name: min((c.score or 0.0) / EVIDENCE_FULL, 1.0)
          for c in cand_list}
    lexical = {c.pt_name: _semantic_score(sem_terms, c) for c in cand_list}
    s3_sem = {c.pt_name: s3_semantic_score(c, cands.env_mode)
              for c in cand_list}
    sem = {c.pt_name: max(lexical[c.pt_name], s3_sem[c.pt_name])
           for c in cand_list}
    gpt_sem: dict[str, dict] = {}      # pt -> {"match","confidence","reason"}

    def _suitable(pt: str) -> bool:
        return (ev[pt] >= ABS_MIN_EVIDENCE and sem[pt] >= ABS_MIN_SEMANTIC)

    # ---- P2 受限 GPT 语义裁决（铁律 14b）：只在「否则全员不达标」的
    # 死路上问一次（有任一候选已达标就不问——GPT 调用数不增）。
    # 资格=证据≥0.4 且 字面语义=0 且 当前语义<下限；确认=语义抬到
    # 下限（不多给），拒绝/低置信/形状不对/异常=维持门槛（fail-closed）。
    eligible = [c for c in cand_list
                if ev[c.pt_name] >= ABS_MIN_EVIDENCE
                and lexical[c.pt_name] == 0.0
                and sem[c.pt_name] < ABS_MIN_SEMANTIC]
    if eligible and ai_semantic is not None \
            and not any(_suitable(c.pt_name) for c in cand_list):
        try:
            verdict = ai_semantic(
                build_semantic_payload(material, cands, eligible)) or {}
            sem_map = verdict.get("semantic") \
                if isinstance(verdict.get("semantic"), dict) else {}
            for c in eligible:
                v = sem_map.get(c.pt_name) if sem_map else None
                if not isinstance(v, dict) or v.get("match") is not True:
                    continue
                try:
                    conf = float(v.get("confidence"))
                except (TypeError, ValueError):
                    continue
                if 0.0 <= conf <= 1.0 and conf >= REVIEW_THRESHOLD:
                    gpt_sem[c.pt_name] = {
                        "match": True, "confidence": round(conf, 4),
                        "reason": str(v.get("reason") or "")[:200]}
                    sem[c.pt_name] = ABS_MIN_SEMANTIC
        except Exception as e:  # noqa: BLE001 —— 裁决层任何异常都不放行
            result.review_reasons.append(
                f"GPT 语义裁决失败：{type(e).__name__}"
                "（门槛维持，进人工复核）")

    suitable = {c.pt_name: _suitable(c.pt_name) for c in cand_list}

    def _rows() -> list[dict]:
        rows = []
        for c in sorted(cand_list,
                        key=lambda c: (-(ev[c.pt_name] + sem[c.pt_name]),
                                       c.pt_name)):
            rows.append({
                "pt_name": c.pt_name,
                "display_name": c.display_name,
                "strategies": sorted(c.sources.keys()),
                "evidence_score": round(ev[c.pt_name], 4),
                "semantic_score": sem[c.pt_name],
                "semantic_detail": {   # P2：语义三源可解释（谁抬的分）
                    "lexical": lexical[c.pt_name],
                    "s3_official": s3_sem[c.pt_name],
                    "gpt": gpt_sem.get(c.pt_name)},
                "final_score": round(0.6 * ev[c.pt_name]
                                     + 0.4 * sem[c.pt_name], 4),
                "suitable": suitable[c.pt_name],
                "ai_reason": "",
                "ai_picked": False,
            })
        return rows

    # ---- 绝对质量门槛（铁律 12）：全员不合适 → 不强行匹配，AI 也不必问 ----
    if not any(suitable.values()):
        result.status = "all_candidates_unsuitable"
        result.candidates = _rows()
        result.review_reasons.append(
            f"全部 {len(cand_list)} 个候选未达绝对质量门槛（证据≥"
            f"{ABS_MIN_EVIDENCE} 且 语义≥{ABS_MIN_SEMANTIC}）——启发式评分"
            "再高也不强行匹配最高分，转人工复核")
        result.needs_review = True
        return result

    # ---- AI 决策（铁律 1/2：只认候选集内） ----
    payload = build_payload(material, cands)
    problems: list[str] = []
    chosen: str | None = None
    reasons: dict[str, str] = {}
    self_conf: float | None = None
    try:
        ai_out = ai_decide(payload)
        chosen, reasons, self_conf, problems = _validate_ai(ai_out, valid)
        result.review_reasons.extend(problems)
    except Exception as e:  # noqa: BLE001 —— AI 层任何异常都不打断流程
        result.review_reasons.append(
            f"AI 决策失败：{type(e).__name__}（进人工复核）")

    # ---- 门槛作用于 AI 所选（铁律 12 后半）：不合适 → 不采纳 ----
    adopted = chosen
    if chosen is not None and not suitable[chosen]:
        adopted = None
        result.review_reasons.append(
            f"AI 所选 {chosen} 未达绝对质量门槛（证据 {ev[chosen]:.2f} /"
            f" 语义 {sem[chosen]:.2f}）——不采纳，转人工复核")
    if adopted is None and chosen is None and not problems \
            and not result.review_reasons:
        result.review_reasons.append("AI 未给出选择（返回 null）")

    # ---- P2 加固（铁律 15a）：GPT-only 语义不自动确认 ----
    # 语义门若只有 GPT 裁决一个来源把候选抬过下限（字面=0 且官方目录=0
    # ——裁决资格本身排除了其他来源），「合适」只是 AI 单方判断，缺乏
    # 任何独立语义证据。此时即使证据分与置信度全部过线，也只作为人工
    # 复核辅助信号：chosen 推荐保留（P5 证据链可见），强制 needs_review。
    # 对抗性场景（错误候选 ev≥0.6 + GPT 误确认 + AI 高自报 → conf≥0.55）
    # 由本闸拦下，绝不 status=matched。
    if adopted is not None and adopted in gpt_sem \
            and lexical[adopted] < ABS_MIN_SEMANTIC \
            and s3_sem[adopted] < ABS_MIN_SEMANTIC:
        result.review_reasons.append(
            f"所选 {adopted} 的语义证据仅来自 GPT 裁决（字面 "
            f"{lexical[adopted]:.2f} / 官方目录 {s3_sem[adopted]:.2f}，"
            "无独立语义证据）——只作人工复核辅助，不自动确认（P2 加固）")

    result.candidates = _rows()
    for row in result.candidates:
        row["ai_reason"] = reasons.get(row["pt_name"], "")
        row["ai_picked"] = row["pt_name"] == chosen
    result.chosen = adopted

    if adopted is not None:
        others = [ev[c.pt_name] for c in cand_list if c.pt_name != adopted]
        best_other = max(others) if others else 0.0
        gap = max(ev[adopted] - best_other, 0.0)
        ai_part = self_conf if self_conf is not None \
            else _AI_NONE_PLACEHOLDER
        result.confidence = round(
            W_EVIDENCE * ev[adopted] + W_SEMANTIC * sem[adopted]
            + W_GAP * gap + W_AI * ai_part, 4)
        result.confidence_breakdown = {
            "evidence": round(ev[adopted], 4),
            "semantic": sem[adopted],
            "semantic_detail": {   # P2：语义分来源（lexical/s3/gpt）
                "lexical": lexical[adopted],
                "s3_official": s3_sem[adopted],
                "gpt": gpt_sem.get(adopted)},
            "gap": round(gap, 4),
            "ai_self": self_conf,
            "weights": {"evidence": W_EVIDENCE, "semantic": W_SEMANTIC,
                        "gap": W_GAP, "ai": W_AI},
            # P2 加固（铁律 15b/15c）：证据依赖表随结果可审计；
            # 原始三源见 semantic_detail，合成分不掩盖原始证据。
            "evidence_dependency": EVIDENCE_DEPENDENCY,
            "abs_gate": {"min_evidence": ABS_MIN_EVIDENCE,
                         "min_semantic": ABS_MIN_SEMANTIC,
                         "chosen_suitable": True},
            "nature": _HEURISTIC_NOTE,
        }
        # ---- 证据冲突（铁律 7）：证据最优≠AI 所选且分差小 ----
        best_ev_pt = max(cand_list, key=lambda c: (ev[c.pt_name],
                                                   c.pt_name)).pt_name
        if best_ev_pt != adopted and \
                (ev[best_ev_pt] - ev[adopted]) < CONFLICT_EPS:
            result.review_reasons.append(
                f"证据冲突：证据最优 {best_ev_pt} 与 AI 选择 {adopted} "
                f"分差 {ev[best_ev_pt] - ev[adopted]:.2f} < {CONFLICT_EPS}")
        if result.confidence < REVIEW_THRESHOLD:
            result.review_reasons.append(
                f"置信度 {result.confidence} 低于复核阈值"
                f" {REVIEW_THRESHOLD}")

    # 单候选过双门槛且无复核理由 → matched：允许自动进入下一步官方验证
    # （铁律 13；official_verified 仍恒 False，验证属模块 5）
    result.status = "matched" if (adopted is not None
                                  and not result.review_reasons) else "review"
    result.needs_review = bool(result.review_reasons)
    return result

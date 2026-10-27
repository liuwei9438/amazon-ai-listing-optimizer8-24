# -*- coding: utf-8 -*-
"""pt.fill —— S2a 属性候选生成（Facts + 官方 Schema → 候选值行）。

只消费 Facts 层与 SchemaAnalysis——Schema 解析（analyze_schema）与值级
校验（schema._validate_fact）全部复用模块 6 既有能力，不另造一套规则。
产出候选行（证据分级 + 出处 + 置信度 + 校验结论），由调用方
（pt_api.attr_fill → store.put_attributes）落库——一律 proposed，
confirmed 只能人工（S1 铁律，put_attributes 层再锁一道）。

证据分级（S2 前置审计交付五契约）：
  A = facts.origin=="raw"（采集器列直录 / 索引字段）
  B = facts.origin=="rule"（确定性规则：父SKU 回退 / verified PT 链）
  C = facts.origin=="opt"（AI 优化文案——OPT_CONFIDENCE 固定 0.5 占位，
      绝不给 A/B；confidence 是「我们对值的把握」，与官方校验无关）
  D = 缺失占位 / 冲突留证（value 可为 None 或 {"conflict": […]}）

保守边界（fail-closed）：
  * 未映射属性一律不生成值（missing 上报，绝不编造）；
  * 兼容品牌绝不写入 brand/manufacturer 类属性（COMPAT_FORBIDDEN_
    TARGETS 显式拦截，即便未来误加映射也挡住）；
  * 业务敏感列（库存/成本/运费/币种）没有映射入口（facts.internal
    留档仅供内部，本模块不读）；
  * optional 属性不生成候选（与 attribute_gap_report 口径一致）；
  * 官方约束校验不了（unknown）→ validation_status=pending，绝不
    passed；mismatch → failed + 结构化 errors（值保留为证据，人定夺）；
  * feed_product_type 唯一来源=验证链 pt_name（assignment 携带），
    AI 理解词绝不经此路径（模块 6 修正 2 的同一纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pt.facts import FactValue, Facts
from pt.schema import SchemaAnalysis, _validate_fact

OPT_CONFIDENCE = 0.5            # C 级（AI 优化文案）固定占位置信度
_CONFIDENCE_BY_ORIGIN = {"raw": 1.0, "rule": 0.9, "opt": OPT_CONFIDENCE}

# 兼容品牌禁止写入的自身品牌类属性（防未来映射表误加——双保险）
COMPAT_FORBIDDEN_TARGETS = frozenset({
    "brand", "manufacturer", "brand_name", "manufacturer_brand"})

_MAX_MISSING = 200              # missing 上报上限（防大 Schema 刷屏）
_MAX_ERRORS_PER_VALUE = 5       # 单值结构化 errors 上限（模型层 ≤20）


@dataclass(frozen=True)
class _Spec:
    """schema 属性名 → Facts 字段的保守映射规格。"""
    fact_key: str               # Facts 字段名
    opt_key: str = ""           # 允许的 AI 优化文案回退字段（""=不许回退）
    note: str = ""


_PROPERTY_MAP: dict[str, _Spec] = {
    "item_name": _Spec("title", "opt_title"),
    "product_description": _Spec("description", "opt_description"),
    "bullet_point": _Spec("bullets", "opt_bullets"),
    "model_number": _Spec("model"),
    "material": _Spec("material"),
    "color": _Spec("color"),
    "brand": _Spec("brand"),
    "part_number": _Spec("part_number"),   # 采集器无零件号列→D 占位
}

# 各字段专属的缺失理由（比通用文案更可操作）
_MISSING_REASON = {
    "brand": "采集器 20 列无品牌字段；兼容品牌绝不充数——需人工提供"
             "（店铺声明默认品牌机制 S2a 未接）",
    "part_number": "采集器无零件号列（fits_part 是适配对象不是自身零件号）"
                   "——需人工提供",
    "model_number": "索引与资料均无型号——需人工提供",
    "color": "变体轴颜色逐行不同——按 SKU 级填充（S2a 不做变体级属性）",
}


def _has_value(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (str, list, tuple, dict)):
        return len(v) > 0
    return True


def _candidate(path: str, *, value, grade: str, source_type: str,
               source_reference: str, confidence: float, prop) -> dict:
    """组装单个候选行并跑官方值级校验（复用 schema._validate_fact）。"""
    verdict, why = _validate_fact(value, prop)
    if verdict == "ok":
        vstat, errs = "passed", []
    elif verdict == "mismatch":
        vstat, errs = "failed", [{"code": "schema_constraint",
                                  "message": why[:200]}]
    else:                           # unknown——校验不了，绝不当通过
        vstat, errs = "pending", []
    return {
        "attribute_path": path,
        "value": value,
        "source_grade": grade,
        "source_type": source_type,
        "source_reference": source_reference,
        "confidence": confidence,
        "validation_status": vstat,
        "validation_errors": errs,
    }


def fill_attributes(facts: Facts, analysis: SchemaAnalysis, *,
                    pt_name: str = "", assignment_id: int | None = None
                    ) -> dict:
    """Facts + SchemaAnalysis → 候选行 + 缺失/冲突上报。

    返回 {candidates:[put_attributes 行…], missing:[{path,name,reason}],
    conflicts:[…], counts:{…}, compat:{…}, notes:[…]}。
    纯函数；不落库、不抛业务异常（坏输入按缺处理）。
    """
    candidates: list[dict] = []
    missing: list[dict] = []
    notes: list[str] = []
    counts = {"a": 0, "b": 0, "c": 0, "d": 0, "passed": 0, "failed": 0,
              "pending": 0, "optional_skipped": 0}
    conflict_fields = facts.conflict_fields()

    for p in analysis.properties:
        if p.requiredness == "optional":
            counts["optional_skipped"] += 1
            continue
        name = p.name

        # feed_product_type：唯一来源=验证链 pt_name（模块 6 修正 2）
        if name == "feed_product_type":
            pt = str(pt_name or "").strip()
            if pt:
                candidates.append(_candidate(
                    p.path, value=pt, grade="B",
                    source_type="rule.verified_pt",
                    source_reference=f"assignment={assignment_id or '?'} "
                                     f"pt_name={pt}（模块4匹配+模块5验证"
                                     f"链产物）",
                    confidence=_CONFIDENCE_BY_ORIGIN["rule"], prop=p))
                counts["b"] += 1
            else:
                candidates.append(_candidate(
                    p.path, value=None, grade="D",
                    source_type="gap.missing",
                    source_reference="官方 Product Type 须经「模块 4 匹配+"
                                     "模块 5 验证」链传入——AI 理解词不充数",
                    confidence=0.0, prop=p))
                counts["d"] += 1
            continue

        # 兼容品牌禁区：自身品牌类属性绝不接收兼容值（双保险）
        if name in COMPAT_FORBIDDEN_TARGETS and name not in _PROPERTY_MAP:
            missing.append({"path": p.path, "name": name,
                            "reason": "兼容品牌禁止写入自身品牌类属性"})
            continue

        spec = _PROPERTY_MAP.get(name)
        if spec is None:
            if len(missing) < _MAX_MISSING:
                missing.append({
                    "path": p.path, "name": name,
                    "reason": "无映射来源（S2a 保守映射表未覆盖——"
                              "不编造，需人工或扩充映射）"})
            continue

        fact: FactValue | None = getattr(facts, spec.fact_key, None)
        used_opt = False
        if not _has_value(getattr(fact, "value", None)) and spec.opt_key:
            of = getattr(facts, spec.opt_key, None)
            if _has_value(getattr(of, "value", None)):
                fact, used_opt = of, True

        if fact is not None and _has_value(fact.value):
            grade = "C" if fact.origin == "opt" else \
                ("A" if fact.origin == "raw" else "B")
            ref = fact.reference + ("；原始列无值，回退 AI 优化文案"
                                    if used_opt else "")
            row = _candidate(
                p.path, value=fact.value, grade=grade,
                source_type=fact.source, source_reference=ref,
                confidence=_CONFIDENCE_BY_ORIGIN.get(fact.origin, 0.0),
                prop=p)
            if fact.note:
                row["_fact_note"] = fact.note        # 供调用方日志/展示
            candidates.append(row)
            counts[grade.lower()] += 1
        elif spec.fact_key in conflict_fields:
            c = next(c for c in facts.conflicts
                     if c.field == spec.fact_key)
            candidates.append(_candidate(
                p.path,
                value={"conflict": list(c.values)},
                grade="D", source_type="gap.conflict",
                source_reference=f"多行互异不择一：{c.note}；样本 "
                                 f"{list(c.values)[:3]}（行 "
                                 f"{list(c.rows)[:6]}）",
                confidence=0.0, prop=p))
            counts["d"] += 1
        else:
            # 缺失：required/conditional/unknown 一律 D 占位（不编造）
            reason = _MISSING_REASON.get(
                spec.fact_key, f"facts.{spec.fact_key} 无值——需人工提供")
            candidates.append(_candidate(
                p.path, value=None, grade="D",
                source_type="gap.missing", source_reference=reason,
                confidence=0.0, prop=p))
            counts["d"] += 1

    for row in candidates:
        counts[row["validation_status"]] = counts.get(
            row["validation_status"], 0) + 1

    cb = facts.compatible_brands
    notes.append("A/B/C/D 是证据分级，不是官方校验状态，更不是可刊登结论；"
                 "全部候选一律 proposed，确认只能人工")
    if cb is not None and cb.value:
        notes.append(f"兼容品牌 {list(cb.value)} 只作适配事实留档——S2a "
                     "未映射到任何兼容类属性（各 PT 兼容属性语义未核实，"
                     "防污染）")
    if facts.uncertain_compat:
        notes.append(f"{len(facts.uncertain_compat)} 条适配值边界不确定——"
                     "待人工确认，未进任何候选")
    return {
        "candidates": [{k: v for k, v in row.items()
                        if not k.startswith("_")}
                       for row in candidates],
        "missing": missing,
        "conflicts": [
            {"field": c.field, "values": list(c.values),
             "rows": list(c.rows), "note": c.note}
            for c in facts.conflicts],
        "counts": counts,
        "compat": {
            "brands": list(cb.value) if cb is not None else [],
            "models": list(facts.fits_models.value)
            if facts.fits_models is not None else [],
            "parts": list(facts.fits_parts.value)
            if facts.fits_parts is not None else [],
            "uncertain": list(facts.uncertain_compat),
        },
        "notes": notes,
    }

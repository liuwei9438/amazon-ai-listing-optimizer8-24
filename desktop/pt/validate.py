# -*- coding: utf-8 -*-
"""pt.validate —— S2d PTD 属性校验引擎与异常复核模型（纯函数，零 IO）。

输入 = 官方 SchemaAnalysis（模块 6）+ S1 属性行（现行 active）+ 变体/
分类上下文；输出 = 逐属性校验报告（规则/字段/原因/来源/修复建议）+
异常清单（七类复核队列词表）。落库（pt_validation / pt_review_exception
v8）与 API 权限闸在 store / pt_api 层——本模块只算不写。

铁律（S2d 验收令）：
1. 本地 validated ≠ 亚马逊平台最终接受——validated 只代表「此刻本地
   规则全跑过且全过」，报告常驻防冒充声明。
2. 完整适用校验跑完才可能 validated：缺失/冲突/低置信/规则不确定/
   分类未决/变体不完整，任何一项在即阻断（fail-closed）。
3. 证据分级 A/B ≠ 官方校验通过（S1 铁律延续）；C 级候选绝不因置信度
   自动确认——未确认行一律 insufficient_evidence 阻断，确认只能经人工
   显式授权路径（引擎无权调用）。
4. 解析不了的规则（anyOf/oneOf/not/外部 $ref/不认识的 format/类型词/
   复杂条件）一律 unknown 绝不当通过；带值属性遇 unknown 即阻断
   （无法证实=不能放行）；无值的 unknown-requiredness 属性只记备注
   不阻断（无法对「不知道是否存在」提要求——边界如实入报告）。
5. 兼容品牌绝不进自身品牌位（brand/manufacturer 类——与 fill 的
   COMPAT_FORBIDDEN_TARGETS 同一纪律，此为提交侧第二道闸）。
6. 条件必填按 JSON Schema 语义求值简单 if 子集
   （properties 标量约束 + required）：属性缺位时 properties 约束
   空真、required 缺位即假；求值不了的 if → 规则不确定 → 阻断。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pt.schema import FORMAT_CHECKERS, SchemaAnalysis, _validate_fact

# —— 异常复核队列词表（七类；store v8 落库同词表）——
EXC_MISSING_REQUIRED = "missing_required"        # 缺失必填属性
EXC_VALUE_CONFLICT = "value_conflict"            # 属性值冲突（值互斥/值与规则冲突）
EXC_INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # 证据不足/低置信（候选未确认）
EXC_SCHEMA_UNCERTAIN = "schema_uncertain"        # Schema 不确定或不支持
EXC_CLASSIFICATION_REVIEW = "classification_review"  # 分类需要复核（含官方验证未过）
EXC_VARIANT_INCOMPLETE = "variant_incomplete"    # 变体关系不完整
EXC_CONTEXT_STALE = "context_stale"              # 商品或 Schema 已变化（需重校验）
EXCEPTION_TYPES = (EXC_MISSING_REQUIRED, EXC_VALUE_CONFLICT,
                   EXC_INSUFFICIENT_EVIDENCE, EXC_SCHEMA_UNCERTAIN,
                   EXC_CLASSIFICATION_REVIEW, EXC_VARIANT_INCOMPLETE,
                   EXC_CONTEXT_STALE)
BLOCKING_TYPES = frozenset(EXCEPTION_TYPES)      # 七类全部阻断本地 validated

# 复核处置动作（人工 API 词表；引擎只开异常，从不 resolve）
RESOLUTION_ACTIONS = ("confirm", "edit", "dismiss", "reclassify")

# 自身品牌位（与 fill.COMPAT_FORBIDDEN_TARGETS 同源；此处独立声明避免
# 引擎依赖 fill——提交侧第二道闸，双保险）
COMPAT_BRAND_TARGETS = frozenset({
    "brand", "manufacturer", "brand_name", "manufacturer_brand"})

LOCAL_GUARD = ("validated 仅代表本地校验引擎全规则通过，绝不等于亚马逊"
               "平台最终接受；平台结论以提交后平台返回为准")

_MAX_ENTRIES = 500               # 单报告条目帽（防大 Schema 刷屏）
_MAX_LIST_PREVIEW = 3            # 数组元素失败预览条数


@dataclass
class ValidationEntry:
    """单属性/单项校验结论。"""
    path: str
    rule: str                     # 触发的规则（required/type/enum/pattern/
                                 # format/range/length/items/conditional/
                                 # compat_guard/variant/classification…）
    category: str                 # 异常类型（EXC_*）；备注行为 ""
    verdict: str                  # passed|failed|unknown|pending|skipped
    reason: str
    source: str = ""              # 值来源（行 source_reference 级）
    suggestion: str = ""          # 可操作修复建议


@dataclass
class ValidationReport:
    entries: list[ValidationEntry] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    blocking: list[str] = field(default_factory=list)   # 去重异常类型
    validated: bool = False
    notes: list[str] = field(default_factory=list)
    guard: str = LOCAL_GUARD
    summary: str = ""

    def add(self, e: ValidationEntry) -> None:
        if len(self.entries) >= _MAX_ENTRIES:
            self.notes.append("条目超上限截断")
            return
        self.entries.append(e)
        if e.category:
            self.counts[e.category] = self.counts.get(e.category, 0) + 1
            if e.category not in self.blocking:
                self.blocking.append(e.category)


# ---------------------------------------------------------------------------
# 值解析：S1 属性行 → 按点径/下标聚成值树（只有进载荷的行=确认/编辑行）
# ---------------------------------------------------------------------------

def _parse_value(row: dict) -> Any:
    try:
        return json.loads(str(row.get("value_json") or "null"))
    except (TypeError, ValueError):
        return None


def _row_source(row: dict) -> str:
    bits = [str(row.get("source_type") or ""), str(row.get("source_grade") or "")]
    ref = str(row.get("source_reference") or "")
    if ref:
        bits.append(ref[:80])
    return "|".join(b for b in bits if b)


def _is_conflict_value(val: Any) -> bool:
    return isinstance(val, dict) and "conflict" in val


class _ValueTree:
    """把（载荷行的）值按路径聚成嵌套树；数组元素路径 a[i] 与分析路径
    a[] 都能取到。只读视图，绝不回写。"""

    def __init__(self, rows: list[dict]):
        self.root: Any = None
        self.by_leaf: dict[str, list[tuple[Any, dict]]] = {}
        for r in rows:
            path = str(r.get("attribute_path") or "")
            val = _parse_value(r)
            self.by_leaf.setdefault(_norm_key(path), []).append((val, r))
            segs = [s for s in path.split(".") if s != ""]
            self.root = _insert(self.root, segs, val)

    def get(self, path: str) -> list[tuple[Any, str]]:
        """取某分析路径的全部取值（含数组展开）；返回 (值, 来源) 列表。"""
        out: list[tuple[Any, str]] = []
        norm = _norm_key(path)
        for val, r in self.by_leaf.get(norm, []):
            out.append((val, _row_source(r)))
        if out:
            return out                       # 行已直接命中——树下钻是同一值的重复
        segs = [s for s in path.split(".") if s != ""]   # 保留 []——数组语义交给 _walk
        for val, src in _walk(self.root, segs):
            out.append((val, src))
        return out


def _norm_seg(s: str) -> str:
    return re.sub(r"\[\d*\]$", "", s)


def _norm_key(path: str) -> str:
    return ".".join(_norm_seg(s) for s in path.split(".") if s != "")


def _insert(node: Any, segs: list[str], val: Any) -> Any:
    if not segs:
        return val
    head, rest = segs[0], segs[1:]
    m = re.fullmatch(r"(.*)\[(\d+)\]$", head)
    if m:                                   # a[i] → dict 键 a 下的列表第 i 位
        key, idx = m.group(1), int(m.group(2))
        if node is None or not isinstance(node, dict):
            node = {}
        child = node.get(key)
        if not isinstance(child, list):
            child = []
        while len(child) <= idx:
            child.append(None)
        child[idx] = _insert(child[idx], rest, val)
        node[key] = child
        return node
    if node is None or not isinstance(node, dict):
        node = {}
    node[head] = _insert(node.get(head), rest, val)
    return node


def _walk(node: Any, segs: list[str]) -> list[tuple[Any, str]]:
    """沿分析路径下钻（a[] → 数组逐元素）。来源标记用嵌套路径。"""
    if not segs:
        return [(node, "")] if node is not None else []
    head, rest = segs[0], segs[1:]
    is_arr = head.endswith("[]")
    key = head[:-2] if is_arr else head
    out: list[tuple[Any, str]] = []
    if isinstance(node, dict) and key in node:
        child = node[key]
        if is_arr and isinstance(child, (list, tuple)):
            for item in child:
                out.extend(_walk(item, rest))
        else:
            out.extend(_walk(child, rest))
    elif isinstance(node, (list, tuple)) and is_arr:
        for item in node:
            out.extend(_walk(item, rest))
    return out


# ---------------------------------------------------------------------------
# 条件求值（if 节点简单子集，JSON Schema 语义）
# ---------------------------------------------------------------------------

def _as_num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError:
            return None
    return None


def eval_cond_if(if_node: Any, tree: _ValueTree) -> tuple[bool | None, str]:
    """求值 if/then 的 if 节点 → (是否命中, 说明)；None=求值不了。

    支持子集：{"properties": {k: {minimum/maximum/exclusiveMinimum/
    exclusiveMaximum/enum/pattern/const/type}}, "required": [k…]}。
    语义：properties 约束只对「有值」的属性生效（缺位=空真）；
    required 里的属性缺位=if 不成立。其它关键词（anyOf/allOf/if/not/
    minProperties…）→ 求值不了。
    """
    if not isinstance(if_node, dict):
        return None, "if 节点不是对象——无法求值"
    allowed = {"properties", "required"}
    if any(k not in allowed for k in if_node):
        return None, f"if 含不支持关键词：{sorted(set(if_node) - allowed)}"
    for r in (if_node.get("required") or []):
        if not isinstance(r, str):
            return None, "if.required 含非字符串"
        if not tree.get(r):
            return False, f"条件属性 {r} 缺值（required 不满足）"
    props = if_node.get("properties") or {}
    if not isinstance(props, dict):
        return None, "if.properties 形状异常"
    why_true: list[str] = []
    for key, spec in props.items():
        if not isinstance(spec, dict):
            return None, f"if.properties.{key} 不是对象"
        vals = tree.get(str(key))
        if not vals:                       # 缺位=空真（JSON Schema 语义）
            continue
        for val, _src in vals:
            ok, why = _eval_scalar(spec, val, str(key))
            if ok is None:
                return None, f"if.properties.{key} 含不支持约束（{why}）"
            if not ok:
                return False, f"{key}={val!r} 不满足 {why}"
        why_true.append(f"{key} 命中")
    return True, "；".join(why_true) if why_true else "条件空真（全部缺位）"


def _eval_scalar(spec: dict, val: Any, key: str) -> tuple[bool | None, str]:
    allowed = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "enum", "pattern", "const", "type"}
    if any(k not in allowed for k in spec):
        return None, f"关键词 {sorted(set(spec) - allowed)}"
    if "const" in spec and val != spec["const"]:
        return False, f"const={spec['const']!r}"
    if "enum" in spec:
        vals = [str(e) for e in (spec["enum"] or [])]
        if str(val) not in vals and str(val).upper() not in {
                v.upper() for v in vals}:
            return False, f"enum={vals[:6]}"
    if "pattern" in spec:
        try:
            if re.search(str(spec["pattern"]), str(val)) is None:
                return False, f"pattern={spec['pattern']}"
        except re.error:
            return None, "pattern 无法编译"
    for bound, op in (("minimum", ">="), ("maximum", "<=")):
        b = spec.get(bound)
        n = _as_num(val) if b is not None else None
        if b is None:
            continue
        if n is None:
            return False, f"{bound}={b}（值非数值）"
        if op == ">=" and n < float(b):
            return False, f"minimum={b}"
        if op == "<=" and n > float(b):
            return False, f"maximum={b}"
    for bound in ("exclusiveMinimum", "exclusiveMaximum"):
        b = spec.get(bound)
        if b is None:
            continue
        if isinstance(b, bool):
            continue                        # draft-04 布尔型：跳过（罕见）
        n = _as_num(val)
        if n is None:
            return False, f"{bound}={b}（值非数值）"
        if bound == "exclusiveMinimum" and n <= float(b):
            return False, f"exclusiveMinimum={b}"
        if bound == "exclusiveMaximum" and n >= float(b):
            return False, f"exclusiveMaximum={b}"
    if "type" in spec:
        t = spec["type"]
        tlist = t if isinstance(t, list) else [t]
        if not _type_ok(val, [str(x) for x in tlist]):
            return False, f"type={t}"
    return True, ""


def _type_ok(val: Any, types: list[str]) -> bool:
    def is_num(x: Any) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool) \
            or (isinstance(x, str) and re.fullmatch(
                r"-?\d+(\.\d+)?", x.strip()) is not None)
    ok_map = {
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: isinstance(x, int) and not isinstance(x, bool)
        or (isinstance(x, str) and re.fullmatch(r"-?\d+", x.strip())),
        "number": is_num,
        "boolean": lambda x: isinstance(x, bool),
        "array": lambda x: isinstance(x, (list, tuple)),
        "object": lambda x: isinstance(x, dict),
        "null": lambda x: x is None,
    }
    return any(t in ok_map and ok_map[t](val) for t in types)


# ---------------------------------------------------------------------------
# 报告序列化 / 异常分组（store v8 与 API 的载荷形状单一真源）
# ---------------------------------------------------------------------------

def report_to_dict(rep: ValidationReport) -> dict:
    return {
        "entries": [{
            "path": e.path, "rule": e.rule, "category": e.category,
            "verdict": e.verdict, "reason": e.reason, "source": e.source,
            "suggestion": e.suggestion,
        } for e in rep.entries],
        "counts": dict(rep.counts),
        "blocking": list(rep.blocking),
        "validated": bool(rep.validated),
        "notes": list(rep.notes),
        "guard": rep.guard,
        "summary": rep.summary,
    }


def report_from_dict(d: dict) -> ValidationReport:
    rep = ValidationReport(
        counts=dict(d.get("counts") or {}),
        blocking=list(d.get("blocking") or []),
        validated=bool(d.get("validated")),
        notes=list(d.get("notes") or []),
        guard=str(d.get("guard") or LOCAL_GUARD),
        summary=str(d.get("summary") or ""),
    )
    rep.entries = [ValidationEntry(
        path=str(e.get("path") or ""), rule=str(e.get("rule") or ""),
        category=str(e.get("category") or ""),
        verdict=str(e.get("verdict") or ""),
        reason=str(e.get("reason") or ""),
        source=str(e.get("source") or ""),
        suggestion=str(e.get("suggestion") or ""),
    ) for e in (d.get("entries") or [])]
    return rep


def report_exceptions(rep: ValidationReport) -> list[dict]:
    """把报告的阻断条目按 (异常类型, 属性路径) 分组成复核队列行：
    [{"type","path","entries":[…],"summary"}]。引擎常驻立场：这只是
    「待人工复核的清单」，处置必须走人工 API。"""
    grouped: dict[tuple[str, str], dict] = {}
    for e in rep.entries:
        if not e.category:
            continue
        key = (e.category, e.path)
        g = grouped.get(key)
        if g is None:
            g = {"type": e.category, "path": e.path, "entries": [],
                 "summary": rep.summary}
            grouped[key] = g
        g["entries"].append({
            "path": e.path, "rule": e.rule, "verdict": e.verdict,
            "reason": e.reason, "source": e.source,
            "suggestion": e.suggestion,
        })
    return list(grouped.values())


# ---------------------------------------------------------------------------
# 校验主入口
# ---------------------------------------------------------------------------

def validate_product(
    analysis: SchemaAnalysis,
    rows: list[dict],
    *,
    variant_roles: list[dict] | None = None,
    assignment: dict | None = None,
    pt_verified: bool | None = None,
) -> ValidationReport:
    """跑完整适用校验。rows=S1 现行 active 行（全量；本函数按复核态
    分流：confirmed/edited 进载荷树，proposed/rejected 只影响缺失判定）。

    variant_roles：expand_skus 输出（sku/parent_sku/variant_role）。
    assignment：现行分类分配行（None=未分类）。pt_verified：官方 PTD
    存在性验证是否 passed（None=未跑）。
    """
    rep = ValidationReport()
    rows = [r for r in rows if r.get("lifecycle", "active") == "active"]
    payload_rows = [r for r in rows
                    if r.get("review_status") in ("confirmed", "edited")]
    tree = _ValueTree(payload_rows)
    by_path: dict[str, dict] = {}
    for r in rows:
        by_path.setdefault(_norm_key(str(r.get("attribute_path") or "")), r)
    missing_prefixes: list[str] = []       # 已开缺失票的父路径（见上归并）

    for p in analysis.properties:
        key = _norm_key(p.path)
        row = by_path.get(key)
        vals = tree.get(p.path)
        provided = bool(vals) and any(v is not None for v, _s in vals)

        # —— 必填缺失的父子归并：父对象整缺时子路径的缺失是同一根因，
        # 开一张工单就够（brand 缺 → brand.value 不另开票）——
        # 父有值而子缺时仍各自照常。missing_prefixes 按文档序累积。
        if not provided and any(
                key == mp or key.startswith(mp + ".")
                or key.startswith(mp + "[") for mp in missing_prefixes):
            continue

        # —— 条件必填：先判适用性（JSON Schema 语义的简单子集）——
        applicable = True
        cond_note = ""
        if p.requiredness == "conditional":
            applies, why = (True, "") if p.cond_if is None else \
                eval_cond_if(p.cond_if, tree)
            if applies is None:
                if provided:
                    cond_note = f"条件不可判定（{why}）——值已提供照常校验"
                else:
                    rep.add(ValidationEntry(
                        path=p.path, rule="conditional", verdict="unknown",
                        category=EXC_SCHEMA_UNCERTAIN,
                        reason=f"条件必填的适用性求值不了：{why}",
                        suggestion="人工判定该属性是否适用；适用则提供值，"
                                   "不适用请在复核中说明"))
                    continue
            elif not applies:
                rep.add(ValidationEntry(
                    path=p.path, rule="conditional", verdict="skipped",
                    category="", reason=f"条件未命中（{why}）——不适用",
                    suggestion=""))
                continue
            else:
                cond_note = f"条件命中（{why}）——按必填处理"

        if p.requiredness == "unknown":
            if provided:
                _check_value(rep, p, vals)
            else:
                rep.notes.append(
                    f"{p.path}：requiredness=unknown（组合约束未解析）且"
                    f"无值——无法要求，仅记录不阻断")
            continue

        need = p.requiredness == "required" or (
            p.requiredness == "conditional" and applicable)
        if not need:
            if provided:
                _check_value(rep, p, vals)
            elif row is not None and row.get("review_status") == "proposed":
                rep.notes.append(
                    f"{p.path}：可选属性有候选未确认（不进载荷，不阻断）")
            elif p.format and p.format not in FORMAT_CHECKERS:
                rep.notes.append(
                    f"{p.path}：可选未提供且官方 format 本引擎不校验"
                    f"（{p.format}）——提供值时将如实标 unknown")
            continue

        # —— 必填（含条件命中）——
        if not provided:
            missing_prefixes.append(key)
            st = str((row or {}).get("review_status") or "")
            if st == "proposed":
                grade = str(row.get("source_grade") or "")
                rep.add(ValidationEntry(
                    path=p.path, rule="required", verdict="pending",
                    category=EXC_INSUFFICIENT_EVIDENCE,
                    reason=f"必填属性已有{grade}级候选但未经人工确认"
                           f"（C 级绝不因置信度自动确认）",
                    source=_row_source(row),
                    suggestion="人工复核确认该值，或编辑为正确值"))
            elif st == "rejected":
                rep.add(ValidationEntry(
                    path=p.path, rule="required", verdict="failed",
                    category=EXC_MISSING_REQUIRED,
                    reason="必填属性的人工候选已被否决且无新值",
                    source=_row_source(row),
                    suggestion="提供新值（人工编辑或重新填充后确认）"))
            else:
                rep.add(ValidationEntry(
                    path=p.path, rule="required", verdict="failed",
                    category=EXC_MISSING_REQUIRED,
                    reason="必填属性缺失（无候选、无人工值）",
                    suggestion="补齐事实材料后重新填充，或人工录入"))
            continue
        _check_value(rep, p, vals, extra=cond_note)

    # —— 冲突行（gap.conflict 来源或 {"conflict": …} 值）——
    for r in rows:
        if r.get("source_type") == "gap.conflict" or _is_conflict_value(
                _parse_value(r)):
            rep.add(ValidationEntry(
                path=str(r.get("attribute_path") or ""), rule="conflict",
                verdict="failed", category=EXC_VALUE_CONFLICT,
                reason="多行材料互斥，留证未裁决（不择一）",
                source=_row_source(r),
                suggestion="人工裁决冲突值（编辑为正确值）"))

    # —— 兼容品牌禁入自身品牌位（第二道闸）——
    for r in payload_rows:
        path = _norm_key(str(r.get("attribute_path") or ""))
        if path.split(".")[0] in COMPAT_BRAND_TARGETS and re.search(
                r"compat|兼容|适用于|fits", _row_source(r), re.I):
            rep.add(ValidationEntry(
                path=path, rule="compat_guard", verdict="failed",
                category=EXC_VALUE_CONFLICT,
                reason="自身品牌位的值来自兼容/适配材料——禁止充数",
                source=_row_source(r),
                suggestion="改为商品自有品牌（人工录入）"))

    # —— 变体完整性（父子关系以实际 SKU 为单位）——
    if variant_roles:
        skus = {str(v.get("sku") or "") for v in variant_roles}
        for v in variant_roles:
            role = str(v.get("variant_role") or "standalone")
            parent = str(v.get("parent_sku") or "")
            if role == "child" and (not parent or parent not in skus):
                rep.add(ValidationEntry(
                    path=f"variant:{v.get('sku')}", rule="variant",
                    verdict="failed", category=EXC_VARIANT_INCOMPLETE,
                    reason=f"子 SKU {v.get('sku')} 的父 SKU"
                           f"（{parent or '空'}）不在本商品 SKU 集",
                    suggestion="补齐父行或修正父 SKU"))
        if any(v.get("variant_role") == "parent" for v in variant_roles) \
                and not any(v.get("variant_role") == "child"
                            for v in variant_roles):
            rep.add(ValidationEntry(
                path="variant", rule="variant", verdict="failed",
                category=EXC_VARIANT_INCOMPLETE,
                reason="存在父 SKU 但没有任何子 SKU（父子关系不完整）",
                suggestion="补齐子行或改为独立商品"))

    # —— 分类与官方验证 ——
    if not (assignment and str(assignment.get("pt_name") or "")):
        rep.add(ValidationEntry(
            path="classification", rule="assignment", verdict="pending",
            category=EXC_CLASSIFICATION_REVIEW,
            reason="无现行 Product Type 分配（未分类或待复核）",
            suggestion="先完成分类（或人工改判）再校验"))
    elif pt_verified is not True:
        why = "官方 PTD 验证未通过" if pt_verified is False \
            else "官方 PTD 验证未跑"
        rep.add(ValidationEntry(
            path="classification", rule="pt_verification", verdict="pending",
            category=EXC_CLASSIFICATION_REVIEW, reason=why,
            suggestion="重跑分类任务让官方验证链给出 passed 记录"))

    rep.validated = not rep.blocking
    if rep.validated:
        rep.summary = ("本地校验通过（validated·local）——全部适用规则"
                       "此刻实跑且全过")
    else:
        rep.summary = (f"本地校验阻断：{len(rep.blocking)} 类异常、"
                       f"{sum(rep.counts.values())} 项待处理"
                       f"（{'、'.join(rep.blocking)}）")
    return rep


def _check_value(rep: ValidationReport, p, vals: list[tuple[Any, str]],
                 extra: str = "") -> None:
    """对已提供的值跑全部规则（类型/枚举/pattern/format/范围/长度/项数）。
    任何 unknown（校验不了）= 不能证实 = 阻断。"""
    fails = 0
    for val, src in vals:
        verdict, why = _validate_fact(val, p)
        if verdict == "ok":
            continue
        fails += 1
        if fails <= _MAX_LIST_PREVIEW:
            cat = EXC_VALUE_CONFLICT if verdict == "mismatch" \
                else EXC_SCHEMA_UNCERTAIN
            rule = ("format" if "format" in why else
                    "pattern" if "pattern" in why else
                    "enum" if "枚举" in why else
                    "type" if "类型" in why else
                    "range" if "数值" in why or "步长" in why else
                    "length" if "长度" in why else
                    "items" if "项数" in why else "constraint")
            rep.add(ValidationEntry(
                path=p.path, rule=rule,
                verdict="failed" if verdict == "mismatch" else "unknown",
                category=cat,
                reason=(f"值与官方规则冲突：{why}" if verdict == "mismatch"
                        else f"官方规则本引擎校验不了：{why}"),
                source=src, suggestion="人工修正值，或改用符合官方约束的值"))
    if fails == 0:
        rep.add(ValidationEntry(
            path=p.path, rule="constraints", verdict="passed", category="",
            reason=("全部适用约束通过" + (f"（{extra}）" if extra else "")),
            suggestion=""))

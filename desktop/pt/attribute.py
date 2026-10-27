# -*- coding: utf-8 -*-
"""pt.attribute —— PTD 属性值模型与状态机（S1：属性值基础层）。

背景（2026-10-19 PTD 架构审计 R3）：分类引擎已能产出属性缺口报告
（schema.py attribute_gap_report），但属性值本身零持久化——无值存储、
无来源分级、无生命周期。本模块定义属性值的领域模型与校验；存储在
store.py v6（pt_attribute 表），HTTP 适配在 pt_api.py（/api/pt/
attributes/*）。S1 只建基础层，不接入分类任务管线（tasks.py 七阶段
顺序不变），也不接 UI。

== 证据分级 source_grade（事实证据强度）==

  A = 商品原始数据直接支持的事实（标题/五点/详情/型号/SKU/人工录入）
  B = 可靠规则转换的事实（单位换算/已验证 PT→feed_product_type/
      店铺声明的默认品牌与 generic 声明）
  C = AI 推断但缺少充分证据（必须带置信度与证据引用）
  D = 缺失、冲突或需要人工补充（占位/待补，不得当作已有值对外）

铁律：A/B/C/D 是「我们对自己的值知道多少」，绝不等同于亚马逊官方
校验状态——grade=A 也只是证据充分，不是「官方通过」，更不是可刊登。

== 复核状态 review_status 状态机 ==

    proposed → confirmed | edited | rejected
    edited   → confirmed | edited | rejected
    confirmed→ edited | rejected
    rejected → edited

  * confirmed 只能由人工操作写入（store.set_review_status 要求
    human=True 显式意图；引擎/任务代码没有任何路径自动确认——
    「不自动确认 AI 属性」）。
  * 人工编辑产生新修订行（review_status=edited），旧行进历史链
    （superseded_by），不删除。
  * rejected 是值的否决，不是删除：行保留，可经 edited 复活。

== 生命周期 lifecycle（绑定分类与 Schema 上下文）==

  active → stale（终态）
  * 触发：所属 assignment 被新分配顶替（分类变更），或所属 Schema
    缓存失效（损坏/官方版本更新使旧上下文不可信）。
  * stale 行不得再 confirmed/edited——必须在新上下文下重新产出。
  * 失效不删除：历史行全量保留（stale_reason 记录原因）。

== 值校验状态 validation_status ==

  pending | passed | failed —— 对照官方 Schema 的值级校验结论，由
  校验器写入（S1 只存储字段，不做校验实现）。failed 行必须带结构化
  validation_errors（[{code, message}]，上限 20 条）。

铁律：
  1. 本模块只做模型与校验（纯函数），不碰 SQLite/网络/文件，
     不 import main——与 pt.material 同一纪律。
  2. 校验失败抛 ValueError（store/api 层翻译为 400/409）。
  3. 兼容品牌/适配型号是适配关系，绝不能写成商品自身品牌——S2+
     的填充层负责强制；source_type 词表预留该约定（compatible_*
     只允许进 compatible_* 类属性）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ---- 合法枚举（写入前一律校验） ----
VALID_SOURCE_GRADE = frozenset({"A", "B", "C", "D"})
VALID_REVIEW_STATUS = frozenset({"proposed", "confirmed", "edited",
                                 "rejected"})
VALID_VALIDATION_STATUS = frozenset({"pending", "passed", "failed"})
VALID_LIFECYCLE = frozenset({"active", "stale"})

# 复核状态机：当前态 → 允许去向（confirmed 只经人工路径，见上）
REVIEW_TRANSITIONS: dict[str, frozenset] = {
    "proposed": frozenset({"confirmed", "edited", "rejected"}),
    "edited": frozenset({"confirmed", "edited", "rejected"}),
    "confirmed": frozenset({"edited", "rejected"}),
    "rejected": frozenset({"edited"}),
}

# ---- 输入边界（DoS/滥用防线；正常属性远小于这些值） ----
MAX_ATTRIBUTE_PATH_LEN = 200
MAX_VALUE_JSON_BYTES = 16_000        # 单属性值 JSON 上限
MAX_ATTRIBUTES_PER_CALL = 200        # 单次批量写入上限
MAX_SOURCE_TYPE_LEN = 64
MAX_SOURCE_REFERENCE_LEN = 500
MAX_VALIDATION_ERRORS = 20
MAX_STALE_REASON_LEN = 300
MAX_SKU_LEN = 80

# 属性路径：PTD 属性名/点路径/数组下标（flight_weight/$ref 键名等
# 官方 Schema 出现的字符集），不允许空白与中文（Amazon 属性名规范）
_PATH_RE = re.compile(r"^[A-Za-z0-9_$.\[\]-]+$")
# 来源类型：小写词/点径 + 可选 ":细节"（material.title / rule:unit_convert /
# ai:gpt / human / engine:gap_report——与 schema.py FACT_SOURCES 的
# material.title 命名惯例一致）
_SOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]*(?::[A-Za-z0-9_.\-]+)?$")


def check_attribute_path(path: Any) -> str:
    """属性路径校验：非空串、字符集、长度、无首尾点。"""
    p = str(path or "").strip()
    if not p:
        raise ValueError("attribute_path 不能为空")
    if len(p) > MAX_ATTRIBUTE_PATH_LEN:
        raise ValueError(f"attribute_path 超长（>{MAX_ATTRIBUTE_PATH_LEN}）")
    if not _PATH_RE.match(p):
        raise ValueError(f"attribute_path 含非法字符：{p[:60]!r}")
    if p.startswith(".") or p.endswith(".") or ".." in p:
        raise ValueError(f"attribute_path 点号位置非法：{p[:60]!r}")
    return p


def check_value_json(value: Any, *, grade: str) -> str:
    """值校验：可 JSON 化、大小上限；D 级允许空值（占位）。"""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"属性值不可 JSON 序列化：{e}") from e
    if len(text.encode("utf-8")) > MAX_VALUE_JSON_BYTES:
        raise ValueError(f"属性值超 {MAX_VALUE_JSON_BYTES} 字节")
    if grade == "D" and value in (None, "", [], {}):
        return text                      # D=缺失占位，空值合法
    if value is None or value == "":
        raise ValueError("A/B/C 级属性值不能为空（空值请用 D 级占位）")
    return text


def check_source(grade: Any, source_type: Any, source_reference: Any,
                 confidence: Any) -> tuple[str, str, str, float]:
    """来源三元组 + 置信度校验，返回规范化值。"""
    g = str(grade or "").strip().upper()
    if g not in VALID_SOURCE_GRADE:
        raise ValueError(f"非法 source_grade：{grade!r}"
                         f"（{sorted(VALID_SOURCE_GRADE)}）")
    st = str(source_type or "").strip()
    if not st or len(st) > MAX_SOURCE_TYPE_LEN:
        raise ValueError("source_type 不能为空且 ≤64 字符")
    if not _SOURCE_TYPE_RE.match(st):
        raise ValueError(f"source_type 格式非法：{st!r}"
                         "（小写词，可选 :细节）")
    sr = str(source_reference or "").strip()
    if len(sr) > MAX_SOURCE_REFERENCE_LEN:
        sr = sr[:MAX_SOURCE_REFERENCE_LEN]
    # C 级（AI 推断）与 A/B 级都必须说清证据从哪来；D 级可省
    if g in ("A", "B", "C") and not sr:
        raise ValueError(f"{g} 级属性必须带 source_reference（证据出处）")
    try:
        conf = float(0.0 if confidence is None else confidence)
    except (TypeError, ValueError) as e:
        raise ValueError(f"confidence 必须是数字：{confidence!r}") from e
    if not 0.0 <= conf <= 1.0:
        raise ValueError(f"confidence 越界 [0,1]：{conf}")
    if g == "C" and conf <= 0.0:
        raise ValueError("C 级（AI 推断）必须带 >0 的 confidence")
    return g, st, sr, conf


def check_validation(validation_status: Any, validation_errors: Any
                     ) -> tuple[str, str]:
    """值校验结论校验：failed 必带 errors（≤20 条），passed 不带。"""
    vs = str(validation_status or "pending").strip().lower()
    if vs not in VALID_VALIDATION_STATUS:
        raise ValueError(f"非法 validation_status：{vs!r}"
                         f"（{sorted(VALID_VALIDATION_STATUS)}）")
    errs = validation_errors or []
    if not isinstance(errs, list):
        raise ValueError("validation_errors 必须是列表")
    if len(errs) > MAX_VALIDATION_ERRORS:
        raise ValueError(f"validation_errors 超上限 {MAX_VALIDATION_ERRORS}")
    for e in errs:
        if not isinstance(e, dict) or not str(e.get("code") or ""):
            raise ValueError("validation_errors 元素须为 {code,message} 结构")
    if vs == "failed" and not errs:
        raise ValueError("validation_status=failed 必须带 errors")
    if vs == "passed" and errs:
        raise ValueError("validation_status=passed 不应带 errors")
    return vs, json.dumps(errs, ensure_ascii=False)


def check_review_transition(current: Any, new: Any) -> str:
    """复核状态机校验：非法跳转抛 ValueError。"""
    cur = str(current or "").strip()
    nxt = str(new or "").strip()
    if cur not in VALID_REVIEW_STATUS:
        raise ValueError(f"非法当前 review_status：{cur!r}")
    if nxt not in VALID_REVIEW_STATUS:
        raise ValueError(f"非法目标 review_status：{nxt!r}")
    if nxt not in REVIEW_TRANSITIONS[cur]:
        raise ValueError(f"复核状态不允许 {cur} → {nxt}"
                         f"（允许：{sorted(REVIEW_TRANSITIONS[cur])}）")
    return nxt


def check_sku(sku: Any) -> str:
    s = str(sku or "").strip()
    if len(s) > MAX_SKU_LEN:
        raise ValueError(f"sku 超长（>{MAX_SKU_LEN}）")
    return s


def check_stale_reason(reason: Any) -> str:
    r = str(reason or "").strip()
    if not r:
        raise ValueError("stale_reason 不能为空（失效必须留原因）")
    return r[:MAX_STALE_REASON_LEN]


def normalize_row(row: dict) -> dict:
    """store 行 → API 返回结构（value_json 反序列化、去掉内部列）。"""
    out = {k: row[k] for k in row if k != "validation_errors_json"}
    try:
        out["value"] = json.loads(row.get("value_json") or "null")
    except (TypeError, ValueError):
        out["value"] = None
    try:
        out["validation_errors"] = json.loads(
            row.get("validation_errors_json") or "[]")
    except (TypeError, ValueError):
        out["validation_errors"] = []
    out.pop("value_json", None)
    return out

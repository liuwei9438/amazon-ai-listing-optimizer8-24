# -*- coding: utf-8 -*-
"""pt.ai_gpt —— 模块 8：真实 GPT 决策器适配（独立 Adapter）。

把模块 4 的 ai_decide 契约接到真实 OpenAI 兼容模型（OpenAI/DeepSeek，
chat/completions）。工程铁律：

- 本模块不 import main、不读全局配置——key/model/base/网络函数全部
  构造期注入（main.py 装配期提供；测试注入假 chat 函数，绝不联网）。
- 不改 matcher 评分：输出仍是 {"chosen", "reasons", "self_confidence"}，
  候选集校验/门槛/置信度合成全部留给 pt.matcher（唯一裁判）。
- 结构化输出限候选集：prompt 明令「只能从 candidates 里选」；模型返回
  候选集之外的 PT 时**原样透传**给 matcher 拒绝（绝不在本层吞掉证据）。
- 异常分类（模块 8 批复四）：超时/连不上/HTTP 错/空响应/JSON 解析失败
  各自抛 AiDecisionError 子类——matcher 会把任何异常转「AI 决策失败
  （进人工复核）」，AI 失败绝不崩批量。
- 决策证据：每次调用把 模型/耗时/候选集版本/结论 追加到 evidence_log
  （JSONL，可注入路径）；**任何日志都不含 API Key**（key 只进请求头，
  异常文本里若带 Authorization 头会被 requests 之外层截断——本模块
  主动对错误文本做 key 探测剔除）。
- 无真实凭证不假装联调：没 key 时构造允许（便于 UI 状态展示），调用
  即抛 AiConfigError，由 matcher 兜底进人工复核。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable

__all__ = [
    "AiDecisionError", "AiConfigError", "AiTimeoutError", "AiUnreachableError",
    "AiBadStatusError", "AiEmptyResponseError", "AiInvalidJsonError",
    "GptDecision", "build_semantic_prompt", "parse_semantic_text",
]


class AiDecisionError(Exception):
    """AI 决策失败基类（matcher 兜底进人工复核，绝不崩批量）。"""

    kind = "error"


class AiConfigError(AiDecisionError):
    kind = "no_key"


class AiTimeoutError(AiDecisionError):
    kind = "timeout"


class AiUnreachableError(AiDecisionError):
    kind = "network"


class AiBadStatusError(AiDecisionError):
    kind = "bad_status"


class AiEmptyResponseError(AiDecisionError):
    kind = "empty"


class AiInvalidJsonError(AiDecisionError):
    kind = "bad_json"


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _strip_key(text: str, key: str) -> str:
    """错误文本里绝不携带 key 片段（模块 8 批复四·日志安全）。"""
    if not key or len(key) < 8:
        return text
    return str(text).replace(key, "***")


def parse_decision_text(text: str) -> dict:
    """把模型输出解析成决策对象（容忍代码围栏/前后废话）。

    合法形状：{"chosen": "PT"|null, "reasons": {"PT": "理由"},
    "self_confidence": 0..1}。解析不出 JSON 对象 → AiInvalidJsonError；
    解析出但形状不对 → AiInvalidJsonError（分类处理，不静默兜底）。
    """
    raw = str(text or "").strip()
    if not raw:
        raise AiEmptyResponseError("模型返回空内容")
    body = raw
    m = _JSON_FENCE.search(raw)
    if m:
        body = m.group(1).strip()
    else:
        # 容忍前后缀废话：截取首个 { 到末个 }
        i, j = body.find("{"), body.rfind("}")
        if i >= 0 and j > i:
            body = body[i:j + 1]
    try:
        obj = json.loads(body)
    except Exception as e:  # noqa: BLE001
        raise AiInvalidJsonError(
            f"模型输出不是合法 JSON：{type(e).__name__}") from e
    if not isinstance(obj, dict):
        raise AiInvalidJsonError("模型输出 JSON 不是对象")
    out: dict = {"chosen": obj.get("chosen"),
                 "reasons": obj.get("reasons") or {},
                 "self_confidence": obj.get("self_confidence")}
    if out["reasons"] is not None and not isinstance(out["reasons"], dict):
        raise AiInvalidJsonError("reasons 不是对象")
    return out


def build_prompt(payload: dict) -> str:
    """matcher.build_payload 的决策载荷 → 紧凑中文 prompt。

    只放身份/功能/用途/适配与候选证据（铁律 3），明令禁止编造。
    """
    p = payload.get("product") or {}
    cands = payload.get("candidates") or []
    lines = [
        "你是亚马逊商品分类助手。只能从候选 Product Type 里选一个最合适"
        "的；都不合适就返回 null。禁止编造候选之外的 Product Type。",
        "",
        "【商品】",
        f"- 标题：{p.get('title') or ''}",
        f"- 身份：{p.get('identity') or ''}"
        f"（来源 {p.get('identity_source') or '未知'}）",
        f"- 型号：{p.get('model') or ''}",
    ]
    if p.get("bullets"):
        lines.append("- 要点：" + "；".join(str(b) for b in p["bullets"][:5]))
    if p.get("fitment"):
        lines.append("- 适配设备：" + "、".join(str(x) for x in p["fitment"][:8]))
    if p.get("compatible_brands"):
        note = (f"（{p.get('compatible_brands_note')}）"
                if p.get("compatible_brands_note") else "")
        lines.append(f"- 兼容品牌：{'、'.join(p['compatible_brands'][:6])}"
                     + note)
    if p.get("usage_scenarios"):
        lines.append("- 用途：" + "、".join(
            str(x) for x in p["usage_scenarios"][:6]))
    lines += ["", "【候选 Product Type】"]
    for i, c in enumerate(cands, 1):
        ev = c.get("evidence") or {}
        evk = "; ".join(
            f"{k}:" + ",".join(str(x) for x in (v or {}).get("asins", [])
                               [:2] or (v or {}).get("keywords", [])[:3] or
                               (v or {}).get("item_name", [])[:1])
            for k, v in ev.items())[:200]
        lines.append(f"{i}. {c.get('pt_name')}"
                     f"（{c.get('display_name') or ''}）"
                     + (f" 证据 {evk}" if evk else ""))
    lines += [
        "",
        "只输出一个 JSON 对象（不要任何其他文字）：",
        '{"chosen": "选中的 pt_name 或 null", '
        '"reasons": {"选中或各候选的 pt_name": "一句话理由"}, '
        '"self_confidence": 0.0到1.0的数字}',
    ]
    return "\n".join(lines)


def build_semantic_prompt(payload: dict) -> str:
    """P2 受限语义裁决 prompt（matcher.build_semantic_payload → 文本）。

    与决策 prompt 的本质区别：只问「商品是否属于该类型」（match/
    confidence/理由），明令**不选择最终分类、不代替官方验证**——选择
    与门槛仍归 pt.matcher（唯一裁判），官方验证归模块 5。"""
    p = payload.get("product") or {}
    cands = payload.get("candidates") or []
    lines = [
        "你是亚马逊商品分类语义审查员。只判断商品与每个候选 Product "
        "Type 的语义归属：商品是不是这一类东西（整机配件挂整机类型、"
        "相似词不等于同类）。不选择最终分类，不代替任何官方验证。",
        "",
        "【商品】",
        f"- 标题：{p.get('title') or ''}",
        f"- 身份：{p.get('identity') or ''}",
        f"- 型号：{p.get('model') or ''}",
    ]
    if p.get("bullets"):
        lines.append("- 要点：" + "；".join(str(b) for b in p["bullets"][:5]))
    if p.get("fitment"):
        lines.append("- 适配设备（辅助线索，商品本体可能只是其配件）：" +
                     "、".join(str(x.get("value") or x) if isinstance(x, dict)
                               else str(x) for x in p["fitment"][:8]))
    lines += ["", "【候选 Product Type】"]
    for i, c in enumerate(cands, 1):
        lines.append(f"{i}. {c.get('pt_name')}"
                     f"（{c.get('display_name') or ''}）")
    lines += [
        "",
        "只输出一个 JSON 对象（不要任何其他文字）：",
        '{"semantic": {"候选 pt_name": {"match": true或false, '
        '"confidence": 0.0到1.0, "reason": "一句话理由"}}}',
    ]
    return "\n".join(lines)


def parse_semantic_text(text: str) -> dict:
    """裁决输出解析（容忍围栏/前后废话）。合法形状：
    {"semantic": {pt: {"match": bool, "confidence": 0..1, "reason": str}}}。
    形状不对 → AiInvalidJsonError（matcher 收到异常=维持门槛）。"""
    raw = str(text or "").strip()
    if not raw:
        raise AiEmptyResponseError("模型返回空内容")
    body = raw
    m = _JSON_FENCE.search(raw)
    if m:
        body = m.group(1).strip()
    else:
        i, j = body.find("{"), body.rfind("}")
        if i >= 0 and j > i:
            body = body[i:j + 1]
    try:
        obj = json.loads(body)
    except Exception as e:  # noqa: BLE001
        raise AiInvalidJsonError(
            f"模型输出不是合法 JSON：{type(e).__name__}") from e
    if not isinstance(obj, dict) or not isinstance(obj.get("semantic"),
                                                   dict):
        raise AiInvalidJsonError("semantic 裁决输出形状不对（缺 semantic 对象）")
    out: dict = {"semantic": {}}
    for k, v in obj["semantic"].items():
        if not isinstance(v, dict):
            continue
        out["semantic"][str(k)] = {
            "match": v.get("match") is True,
            "confidence": v.get("confidence"),
            "reason": str(v.get("reason") or "")[:200],
        }
    return out


class GptDecision:
    """ai_decide 适配器（线程安全性由 TaskEngine 的 AI 锁保证）。

    参数：
      key/model   模型凭证与名称（复用现有 AI 配置链路，装配期注入）
      base        API 根地址（OpenAI 兼容；空=按 provider 默认）
      chat_fn     注入式网络函数 (prompt, timeout) -> 文本。测试注入假
                  函数即可全程零联网（模块 8 批复四·Mock 注入）。
      evidence_log 决策证据 JSONL 路径（None=不落盘；绝不写 key）
      provider    openai | deepseek（只影响默认 base）
    """

    def __init__(self, *, key: str, model: str, base: str = "",
                 chat_fn: Callable[[str, float], str] | None = None,
                 evidence_log: str | Path | None = None,
                 provider: str = "openai", timeout: float = 45.0,
                 max_tokens: int = 500):
        self._key = str(key or "")
        self._model = str(model or "")
        self._base = str(base or "").rstrip("/") or (
            "https://api.deepseek.com" if provider == "deepseek"
            else "https://api.openai.com/v1")
        self._provider = str(provider or "openai").lower()
        self._timeout = float(timeout)
        self._max_tokens = int(max_tokens)
        self._chat_fn = chat_fn
        self._log_path = Path(evidence_log) if evidence_log else None
        self.calls = 0          # 诊断计数（无敏感信息）

    # ---- 对外状态（UI「AI 就绪」展示用；绝不含 key） ----

    def describe(self) -> dict:
        return {"ready": bool(self._key and self._model),
                "model": self._model or "",
                "provider": self._provider,
                "base": self._base if "mock" in self._base or
                "127.0.0.1" in self._base or "localhost" in self._base
                else "official",
                "timeout": self._timeout, "calls": self.calls}

    # ---- ai_decide 契约 ----

    def __call__(self, payload: dict) -> dict:
        pid = str((payload.get("product") or {}).get("pid") or "")
        cands = payload.get("candidates") or []
        cands_hash = hashlib.sha1(
            ",".join(sorted(str(c.get("pt_name") or "")
                            for c in cands)).encode("utf-8")
        ).hexdigest()[:10]
        started = time.time()
        err_kind = ""
        chosen = None
        try:
            if not self._key or not self._model:
                raise AiConfigError(
                    "还没配置 AI Key/模型（⚙️ 里配置，或让管理员在服务器"
                    "保存全局 Key）")
            prompt = build_prompt(payload)
            text = self._ask(prompt)
            out = parse_decision_text(text)
            chosen = out.get("chosen")
            return out
        except AiDecisionError as e:
            err_kind = e.kind
            raise
        finally:
            self._evidence(pid=pid, model=self._model,
                           latency_ms=round((time.time() - started) * 1000),
                           cands_n=len(cands), cands_hash=cands_hash,
                           chosen=chosen, err_kind=err_kind)

    # ---- P2 受限语义裁决契约（matcher.ai_semantic） ----

    def semantic_verdict(self, payload: dict) -> dict:
        """受限语义裁决：复用同一凭证/网络/证据管线，prompt 与解析
        独立（只判 match/confidence，不选分类）。异常分类同 __call__
        ——matcher 收到异常=维持门槛（fail-closed）。"""
        pid = str((payload.get("product") or {}).get("pid") or "")
        cands = payload.get("candidates") or []
        cands_hash = hashlib.sha1(
            ",".join(sorted(str(c.get("pt_name") or "")
                            for c in cands)).encode("utf-8")
        ).hexdigest()[:10]
        started = time.time()
        err_kind = ""
        matched: list[str] = []
        try:
            if not self._key or not self._model:
                raise AiConfigError("还没配置 AI Key/模型")
            text = self._ask(build_semantic_prompt(payload))
            out = parse_semantic_text(text)
            matched = sorted(k for k, v in (out.get("semantic") or {}).items()
                             if v.get("match"))
            return out
        except AiDecisionError as e:
            err_kind = e.kind
            raise
        finally:
            self._evidence(pid=pid, model=self._model, kind="semantic",
                           latency_ms=round((time.time() - started) * 1000),
                           cands_n=len(cands), cands_hash=cands_hash,
                           matched=matched, err_kind=err_kind)

    def _ask(self, prompt: str) -> str:
        if self._chat_fn is not None:
            return self._chat_fn(prompt, self._timeout)
        # 默认走 OpenAI 兼容 chat/completions（与 main._ai_chat_one 同构；
        # 这里独立实现，避免 import main）
        import requests  # 局部引入：注入 chat_fn 的测试零依赖

        try:
            r = requests.post(
                self._base + "/chat/completions",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + self._key},
                json={"model": self._model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0,
                      "max_tokens": self._max_tokens},
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001 —— 分类处理，绝不裸抛
            name = type(e).__name__
            if "timeout" in name.lower() or "timed out" in str(e).lower():
                raise AiTimeoutError(f"模型请求超时（{self._timeout}s）") \
                    from e
            raise AiUnreachableError(
                _strip_key(f"连不上模型服务：{name}: {e}", self._key)[:300]
            ) from e
        if r.status_code != 200:
            raise AiBadStatusError(_strip_key(
                f"模型服务返回 {r.status_code}：{r.text[:200]}",
                self._key)[:300])
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            raise AiInvalidJsonError("模型服务响应不是 JSON") from e
        content = str((((data.get("choices") or [{}])[0]
                        .get("message") or {}).get("content")) or "")
        if not content.strip():
            raise AiEmptyResponseError("模型返回空内容")
        return content

    def _evidence(self, **kw) -> None:
        """决策证据落盘（模型/耗时/候选集版本/结论；绝无 key）。"""
        if self._log_path is None:
            return
        try:
            self.calls += 1
            kw["ts"] = round(time.time(), 3)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(kw, ensure_ascii=False) + "\n")
        except OSError:
            pass            # 证据盘故障绝不影响决策主流程

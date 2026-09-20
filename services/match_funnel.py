# V2.11.5 三层匹配漏斗 + 成本预估。
#
# 借鉴 GM-助手 auto_ai_pricing 的漏斗哲学：便宜的证据先粉碎候选，
# 贵的 AI 视觉只终审剩下的前几名——
#   第一层  标题事实否决（数量冲突直接淘汰，免费）
#   第二层  颜色比对 + 规格冲突扣分（本地像素/正则，免费）
#   第三层  视觉模型终审（可选：GLM 等 OpenAI 兼容多模态，双图对照）
# 附带 estimate_cost()：按 1688 采购价 + 贴标费 + 头程分摊粗估单件成本。
#
# 设计约束：本模块绝不 import 同包其他模块（sourcing 会 import 这里的
# dominant_colors），所有函数失败时安静降级，绝不让批次收尾崩掉。
from __future__ import annotations

import io
import json
import re

import requests

# ---------------- 可调参数 ----------------

FREIGHT_CNY = 8.0        # 每单头程运费默认（元）
LABELING_CNY = 4.0       # 贴标费默认（元）
COLOR_MIN_SHARE = 0.12   # 占比≥12% 的颜色才算主色
VISION_TOPK = 3          # 视觉终审最多送几个候选
VISION_MIN_CONF = 60     # 置信度低于此值转人工
VISION_TIMEOUT = 60

VISION_BASE_DEFAULT = "https://open.bigmodel.cn/api/paas/v4"
VISION_MODEL_DEFAULT = "glm-4.5v"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}

# ---------------- 第一层：标题事实 ----------------

_EN_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
}

_ZH_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

_ZH_UNITS = r"(?:只|个|件|颗|支|条|片|双|对|套|包|盒|pcs|PCS|Pieces)"


def extract_qty_en(text: str):
    """英文标题里的销售数量（Pack of 2 / 2-Pack / 4 Pcs / x2 …）。

    拿不到返回 None（= 未声明，按 1 件理解）。
    """
    s = str(text or "").lower()
    if not s:
        return None
    found = []

    for m in re.finditer(
        r"(?:pack|set|pcs|pieces|piece|packs|sets)\s*(?:of)?\s*([0-9]{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        s,
    ):
        v = _EN_NUM.get(m.group(1))
        if v:
            found.append(v)

    for m in re.finditer(
        r"\b([0-9]{1,2})\s*[-\s]?\s*(?:pack|pcs|pieces|piece|set)\b",
        s,
    ):
        found.append(int(m.group(1)))

    for m in re.finditer(
        r"(?<![0-9])(?<![0-9]\s)[x×]\s*([0-9]{1,2})(?!\s*[0-9])",
        s,
    ):
        # 「Filter x2」算数量；「2 x 3 Inch」是尺寸——x 前面挨着数字的不算
        found.append(int(m.group(1)))

    # "Pack of Two"（数字词跟在 of 后）上面第一条已覆盖。
    return max(found) if found else None


def extract_qty_zh(text: str):
    """1688 中文标题里的销售数量（2只装 / 十只 / 一盒2个 …）。

    拿不到返回 None。多段数量取最大值（「一盒2个」按 2 个卖理解）。
    """
    s = str(text or "")
    if not s:
        return None
    found = []

    for m in re.finditer(rf"([0-9]{{1,3}})\s*{_ZH_UNITS}(?:装)?", s):
        found.append(int(m.group(1)))

    for m in re.finditer(rf"([一二两三四五六七八九十])\s*{_ZH_UNITS}(?:装)?", s):
        v = _ZH_NUM.get(m.group(1))
        if v:
            found.append(v)

    return max(found) if found else None


def quantity_conflict(src_qty, cand_qty) -> bool:
    """数量是否冲突（照 GM 规则，宁松勿严）。

    候选未写数量 / 明确单件 → 可按所需数量采购，不算冲突；
    只有候选明确多件包装且数量对不上才算冲突。
    """
    c = int(cand_qty) if cand_qty else 1
    s = int(src_qty) if src_qty else 1
    return c > 1 and c != s


# 规格单位：英文标题和 1688 标题里都会原样出现的写法
_SPEC_UNITS = [
    "mm", "cm", "inch", "in", "kg", "g", "ml", "mah", "w", "v", "a",
    "寸", "层", "格", "孔", "节",
]
_SPEC_RE = {
    u: re.compile(rf"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*{u}\b", re.I)
    for u in _SPEC_UNITS
}


def _spec_map(text: str) -> dict:
    s = str(text or "").lower()
    out = {}
    for unit, pattern in _SPEC_RE.items():
        values = pattern.findall(s)
        if values:
            out[unit] = {float(v) for v in values}
    return out


def spec_conflicts(title_src: str, title_cand: str) -> list:
    """同单位不同数值的规格冲突（如 30mm vs 35mm）。返回冲突描述列表。"""
    a = _spec_map(title_src)
    b = _spec_map(title_cand)
    hits = []

    for unit, av in a.items():
        bv = b.get(unit)
        if not bv:
            continue
        for x in av:
            for y in bv:
                big, small = max(x, y), min(x, y)
                if big > 0 and (big - small) / max(big, 1) > 0.08:
                    hits.append(f"{x:g}{unit}≠{y:g}{unit}")
    return hits


# ---------------- 第二层：主色比对 ----------------

# 色相区间 → 色名（对齐 GM 的常用电商色口径）
_HUE_BOUNDS = [
    (0, 15, "red"), (15, 45, "orange"), (45, 70, "yellow"),
    (70, 165, "green"), (165, 255, "blue"), (255, 290, "purple"),
    (290, 330, "pink"), (330, 360.1, "red"),
]


def dominant_colors(image) -> list:
    """主色列表 [(色名, 占比)]，占比≥COLOR_MIN_SHARE，最多 4 个。

    白底/浅灰背景像素（低饱和高亮）直接剔除，不计入分母——
    不然所有主图的主色都是「白」，颜色门就废了。
    """
    try:
        import numpy as np
        from PIL import Image as _PIL

        # 注意是对 PIL 模块取 Resampling（老版 Pillow 没有，退回模块本身）
        resample = getattr(getattr(_PIL, "Resampling", _PIL), "LANCZOS")
        small = image.convert("RGB").resize((48, 48), resample)
        arr = np.asarray(small, dtype=np.float32) / 255.0
        mx = arr.max(axis=2)
        mn = arr.min(axis=2)
        v = mx
        s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)

        keep = ~((s < 0.12) & (v > 0.80))  # 剔背景
        total = int(keep.sum())
        if total < 80:
            return []

        counts = {}

        def add(name: str, mask):
            n = int((mask & keep).sum())
            if n:
                counts[name] = counts.get(name, 0) + n

        add("black", (v < 0.16))
        add("white", (s < 0.12) & (v > 0.55))
        add("gray", (s < 0.14) & (v >= 0.16) & (v <= 0.55))

        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        hue = np.zeros_like(v)
        d = np.maximum(mx - mn, 1e-6)
        hh = np.where(
            mx == r, ((g - b) / d) % 6,
            np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4),
        ) * 60.0
        hue = np.clip(hh, 0, 360)

        colored = (s >= 0.14) & (v >= 0.16)
        for lo, hi, name in _HUE_BOUNDS:
            add(name, colored & (hue >= lo) & (hue < hi))

        out = [
            (name, n / total)
            for name, n in counts.items()
            if n / total >= COLOR_MIN_SHARE
        ]
        out.sort(key=lambda p: -p[1])
        return out[:4]

    except Exception:
        return []


def colors_compatible(colors_a, colors_b):
    """两边主色有没有交集。任一边拿不到（None/空）→ True（不否决）。"""
    if not colors_a or not colors_b:
        return True
    set_a = {name for name, _ in colors_a}
    set_b = {name for name, _ in colors_b}
    return bool(set_a & set_b)


# ---------------- 第三层：视觉终审（可选） ----------------

def vision_cfg():
    """从 session / secrets 收视觉模型配置；没有 Key 返回 None。"""
    try:
        import streamlit as st
    except Exception:
        return None

    key = ""
    model = ""
    base = VISION_BASE_DEFAULT
    try:
        key = str(st.session_state.get("pl_vision_key") or "").strip()
        model = str(st.session_state.get("pl_vision_model") or "").strip()
    except Exception:
        pass
    try:
        if not key:
            key = str(st.secrets.get("ZHIPU_API_KEY") or "").strip()
    except Exception:
        pass

    if not key:
        return None
    return {
        "key": key,
        "model": model or VISION_MODEL_DEFAULT,
        "base": base.rstrip("/"),
    }


_VISION_PROMPT = """你是跨境电商1688同款匹配审核员。第1张图是亚马逊在售商品的标准图，之后的图片是按 #1、#2… 编号的1688候选商品图。
请把每个候选单独与第1张标准图比较：商品品类、整体轮廓与结构、部件数量与位置、颜色组合、关键规格。严禁把别的候选的颜色或结构串到当前候选。
采购数量只认标题：候选标题未写数量或明确单件 = 可按所需数量采购（数量合格）；只有候选明确写了不同的多件包装数量才是数量冲突。
标题里明确的数量、规格、材质是硬证据，图片不得推翻；看不清或无法确认的项目不得给高分。
只输出严格JSON（不要Markdown、不要解释）：
{"selectedIndex": 1到N的整数或-1, "confidence": 0到100, "reason": "中文一句话理由"}
没有任何候选是同款时 selectedIndex 为 -1。

亚马逊商品标题：{title}
候选标题：
{cands}"""


def vision_review(src_title, src_img_url, cands: list, cfg: dict):
    """多模态终审：返回 {selectedIndex, confidence, reason} 或 None（任何失败）。

    cands: [{title, img}, …] 最多 VISION_TOPK 个。
    """
    if not cfg or not src_img_url or not cands:
        return None
    cands = cands[:VISION_TOPK]

    content = [
        {"type": "text", "text": _VISION_PROMPT.format(
            title=str(src_title or "")[:400],
            cands="\n".join(
                f"#{i + 1} {str(c.get('title') or '')[:120]}"
                for i, c in enumerate(cands)
            ),
        )},
        {"type": "image_url", "image_url": {"url": src_img_url}},
    ]
    for c in cands:
        if c.get("img"):
            content.append(
                {"type": "image_url", "image_url": {"url": c["img"]}}
            )

    try:
        resp = requests.post(
            f"{cfg['base']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": content}],
                "temperature": 0,
                "max_tokens": 300,
            },
            timeout=VISION_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        text = resp.json()["choices"][0]["message"]["content"]
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(text[start:end + 1])
        index = int(data.get("selectedIndex", -1))
        conf = int(data.get("confidence", 0))
        if 1 <= index <= len(cands):
            return {
                "pick": index - 1,
                "confidence": conf,
                "reason": str(data.get("reason") or "")[:80],
            }
        return {"pick": -1, "confidence": conf,
                "reason": str(data.get("reason") or "")[:80]}

    except Exception:
        return None


# ---------------- 成本预估 ----------------

def estimate_cost(price, moq) -> dict:
    """按 1688 采购价粗估单件成本（元）。

    单件成本 = 采购单价 + 贴标费 + 每单头程 ÷ 起订量（起订 2 件时运费摊一半）。
    价格区间（"1.85-2.1"）取低值。解析不了返回 None。
    """
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(price or ""))
    if not m:
        return None
    unit = float(m.group(1))
    if unit <= 0:
        return None

    m_moq = re.search(r"([0-9]+)", str(moq or ""))
    qty = max(1, int(m_moq.group(1)) if m_moq else 1)

    labeling = LABELING_CNY
    freight_share = FREIGHT_CNY / qty
    return {
        "unit": round(unit, 2),
        "labeling": labeling,
        "freight_share": round(freight_share, 2),
        "total": round(unit + labeling + freight_share, 2),
    }


# ---------------- 漏斗编排 ----------------

def funnel_select(item: dict, results: list, img_cache: dict, vc: dict):
    """对单个产品跑完整漏斗，选出最佳供应商。

    返回 {
        cand,          # 选中的候选（原 dict）
        score,         # 本地综合分
        verify,        # "视觉✓93" / "待人工" / "本地评分" / "待人工（数量冲突）"
        vetoed_n, spec_n, color_bad_n, vision_used,
    }；results 为空返回 None。
    """
    from .sourcing import image_colors, image_similarity, score_candidate

    src_qty = extract_qty_en(item.get("title_en") or item.get("title") or "")
    title_src = str(item.get("title_en") or item.get("title") or "")

    scored = []
    vetoed = 0
    color_bad = 0
    spec_total = 0

    src_colors = image_colors(item.get("image_url") or "", img_cache)

    for cand in results:
        cand_title = str(cand.get("title") or "")

        if quantity_conflict(
            src_qty,
            extract_qty_zh(cand_title) or extract_qty_en(cand_title),
        ):
            vetoed += 1
            continue

        specs = spec_conflicts(title_src, cand_title)
        spec_total += len(specs)

        cand_colors = image_colors(cand.get("img") or "", img_cache)
        compat = colors_compatible(src_colors, cand_colors)
        if not compat:
            color_bad += 1

        img_sim = image_similarity(
            item.get("image_url", ""),
            cand.get("img", ""),
            img_cache,
        )

        total, _parts = score_candidate(
            item,
            cand,
            img_sim,
            color_state=("ok" if compat else "bad") if (src_colors and cand_colors) else None,
            spec_pen=len(specs),
        )
        scored.append((total, cand))

    if not scored:
        if not results:
            return None
        # 全部被数量否决：留最高原始分给人工复核，绝不让产品空手而归
        best = max(
            results,
            key=lambda c: score_candidate(
                item, c,
                image_similarity(item.get("image_url", ""), c.get("img", ""), img_cache),
            )[0],
        )
        return {
            "cand": best, "score": 0, "verify": "待人工（数量冲突）",
            "vetoed_n": vetoed, "spec_n": spec_total,
            "color_bad_n": color_bad, "vision_used": False,
        }

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top = scored[0]

    verify = "本地评分"
    vision_used = False

    if vc and top_score >= 35:
        vr = vision_review(
            title_src,
            item.get("image_url") or "",
            [{"title": c.get("title"), "img": c.get("img")}
             for _, c in scored[:VISION_TOPK]],
            vc,
        )
        if vr is not None:
            vision_used = True
            if vr["pick"] >= 0 and vr["confidence"] >= VISION_MIN_CONF:
                top = scored[vr["pick"]][1]
                verify = f"视觉✓{vr['confidence']}"
            else:
                verify = "待人工"

    return {
        "cand": top,
        "score": top_score,
        "verify": verify,
        "vetoed_n": vetoed,
        "spec_n": spec_total,
        "color_bad_n": color_bad,
        "vision_used": vision_used,
    }

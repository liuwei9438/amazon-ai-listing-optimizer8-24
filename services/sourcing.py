from __future__ import annotations


# =====================================================
# V2.8 1688 找货（优化程序端）
#
# 流程：老板在「1688 找货」页勾选导入的产品 → AI 把英文标题
# 转成中文采购搜索词（顺带提取型号/品牌）→ 创建找货批次
# （Worker v11 /src_task_add）→ 老板 Chrome 里的采集插件 v5
# 自动认领任务，在 1688 做型号搜索 + 以图搜图 → 结果回传
# Worker → 这里轮询 /src_tasks_view 看进度 → 全部完成后
# 计算综合匹配分（型号 40 / 图片相似 35 / 品牌 10 / 品名 10 /
# 商家信号 5，图片相似度用 pHash 在服务器端对比）→
# 每个产品推荐一个 ✅ 供应商 → 导出 Excel（原表右侧补
# 采购链接/采购价/起订量/供应商/匹配分 五列）。
#
# 员工看不到这一页：入口只在管理员账号（app_admins）或
# 完全没配登录（本地开发）时出现。
# =====================================================


import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from PIL import Image

from services.ai_client import OpenAIResponsesClient
from services.json_storage import load_json, save_json


SRC_STORE = Path("tasks/sourcing/batches.json")

# 导出时补在原表右侧的找货列
SRC_EXPORT_COLUMNS = [
    "采购链接",
    "采购价",
    "起订量",
    "供应商",
    "匹配分",
]

# 综合匹配分各项上限（合计 100）
W_MODEL = 40
W_IMAGE = 35
W_BRAND = 10
W_NAME = 10
W_SELLER = 5

_FALLBACK_SERVER = "https://wz-auth.liuweide9438.workers.dev"


# =====================================================
# 权限 / 服务器 / 密钥
# =====================================================


def sourcing_allowed() -> bool:
    """找货入口只给管理员账号；完全没配登录（本地开发）时放行。"""
    try:
        from services.user_auth import (
            auth_enabled,
            is_admin_user,
        )

        if not auth_enabled():
            return True

        return is_admin_user()

    except Exception:

        return True


def _src_server() -> str:
    """Worker 地址：优先复用 Secrets 里的 auth_server（同一个 Worker）。"""
    url = ""

    try:
        url = str(
            st.secrets.get("auth_server", "") or ""
        ).strip()

    except Exception:
        url = ""

    return (url.rstrip("/") or _FALLBACK_SERVER)


def get_src_key() -> str:
    """找货密钥：管理后台 /manage 生成后填进 Secrets 的 SRC_KEY。"""
    try:
        return str(
            st.secrets.get("SRC_KEY", "") or ""
        ).strip()

    except Exception:
        return ""


def src_api(
    path: str,
    payload: dict,
    timeout: int = 25,
) -> dict:
    """调 Worker 找货接口。返回响应 JSON；网络/HTTP 错抛 RuntimeError。"""
    resp = requests.post(
        _src_server() + path,
        json=payload,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )

    try:
        data = resp.json()

    except Exception as exc:
        raise RuntimeError(
            f"Worker 响应不是 JSON（HTTP {resp.status_code}）"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Worker 响应格式异常")

    return data


# =====================================================
# 表格列 / 单元格工具
# =====================================================


def _find_col(
    dataframe: pd.DataFrame,
    *needles: str,
):
    """按精确名 → 包含子串 两种方式找列，找不到返回 None。"""
    columns = list(dataframe.columns)

    for needle in needles:
        if needle in columns:
            return needle

    for needle in needles:
        for col in columns:
            if needle in str(col):
                return col

    return None


def _cell_str(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def first_url(cell) -> str:
    """产品图单元格里可能有多张图（换行/空格分隔），取第一个 URL。"""
    text = _cell_str(cell)

    match = re.search(
        r"https?://[^\s,，;；\"']+",
        text,
    )

    return match.group(0) if match else ""


def fallback_model(title: str) -> str:
    """不用 AI 时从标题里猜型号：字母+数字组合优先，纯 6~8 位数字次之。"""
    text = _cell_str(title)

    if not text:
        return ""

    combo = re.findall(
        r"\b[A-Za-z]{1,5}[\s-]?\d{2,6}[A-Za-z]?\b",
        text,
    )

    if combo:
        return combo[0].upper()

    digits = re.findall(
        r"\b\d{6,8}\b",
        text,
    )

    if digits:
        return digits[0]

    dotver = re.findall(
        r"\b[A-Za-z]{1,5}\s?\d{1,2}\.\d\b",
        text,
    )

    return dotver[0].upper() if dotver else ""


# =====================================================
# AI 搜索词转换（英文标题 → 中文采购搜索词 + 型号 + 品牌）
# =====================================================


_CONVERT_SYSTEM = (
    "你是跨境电商采购专家，帮亚马逊卖家在 1688（中国批发平台）"
    "找同款货源。输入是亚马逊英文标题列表，对每一行输出：\n"
    "kw：中文采购搜索词，1~3 个词，必须是 1688 中国卖家实际会用的"
    "高频品类词（先想这类货在批发市场叫什么，如「遥控车轮胎」"
    "「珠锁轮毂」「金属尾翼」）。宁可泛一点也不要堆词：超过 3 个词"
    "或带尺寸/规格（如 90x37mm、1.9寸、2Pcs）在 1688 基本搜不到，"
    "长尾组合一律换成上位品类词。\n"
    "不要出现英文品牌名（如 Homag、Lectric 必须去掉，换成通用词）。\n"
    "model：标题里的具体型号/零件号（如 4014262、TST R002、XP 3.0），"
    "没有就填空字符串。\n"
    "brand：标题里的英文品牌名（原样），没有就填空字符串。"
)

_CONVERT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "kw": {"type": "string"},
                    "model": {"type": "string"},
                    "brand": {"type": "string"},
                },
                "required": ["i", "kw", "model", "brand"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _convert_chunk(
    titles: list[str],
    api_key: str,
    model: str,
) -> dict[int, dict]:
    """一次 AI 调用转换 ≤10 条标题，返回 {序号: {kw,model,brand}}。"""
    lines = "\n".join(
        f"{i}. {title}"
        for i, title in enumerate(titles)
    )

    client = OpenAIResponsesClient(
        api_key,
        model or "gpt-4.1-mini",
        stage="sourcing_kw",
    )

    result = client.create_json(
        _CONVERT_SYSTEM,
        "亚马逊标题列表：\n" + lines,
        _CONVERT_SCHEMA,
    )

    out: dict[int, dict] = {}

    items = result.get("items") if isinstance(result, dict) else None

    if isinstance(items, list):
        for entry in items:
            if not isinstance(entry, dict):
                continue

            try:
                index = int(entry.get("i"))

            except Exception:
                continue

            if 0 <= index < len(titles):
                out[index] = {
                    "kw": _cell_str(entry.get("kw"))[:60],
                    "model": _cell_str(entry.get("model"))[:60],
                    "brand": _cell_str(entry.get("brand"))[:30],
                }

    return out


def convert_search_terms(
    titles: list[str],
    api_key: str,
    model: str,
    progress=None,
) -> list[dict]:
    """批量转换。失败的单条回退：kw=英文原标题、型号用本地正则。"""
    converted: list[dict] = [
        {"kw": "", "model": "", "brand": ""}
        for _ in titles
    ]

    chunk_size = 10

    for start in range(0, len(titles), chunk_size):
        chunk = [
            _cell_str(t)[:200]
            for t in titles[start:start + chunk_size]
        ]

        try:
            mapping = _convert_chunk(chunk, api_key, model)

        except Exception:

            mapping = {}

        for offset, title in enumerate(chunk):
            entry = mapping.get(offset)

            if entry and entry.get("kw"):
                converted[start + offset] = entry

            else:
                converted[start + offset] = {
                    "kw": title[:60],
                    "model": fallback_model(title),
                    "brand": "",
                    "fallback": True,
                }

        if progress:
            progress(
                min(start + chunk_size, len(titles)) / max(len(titles), 1)
            )

    return converted


# =====================================================
# 图片相似度：pHash（DCT 32x32 → 8x8 低频 → 64 位）
# =====================================================


def _dct_matrix(size: int):
    import numpy as np

    k = np.arange(size).reshape(-1, 1)
    x = np.arange(size).reshape(1, -1)

    return np.cos(
        np.pi * (2 * x + 1) * k / (2 * size)
    )


_DCT_CACHE: dict[int, object] = {}


def phash_image(image: Image.Image) -> int:
    import numpy as np

    matrix = _DCT_CACHE.get(32)

    if matrix is None:
        matrix = _dct_matrix(32)
        _DCT_CACHE[32] = matrix

    resample = getattr(
        getattr(Image, "Resampling", Image),
        "LANCZOS",
    )

    small = image.convert("L").resize(
        (32, 32),
        resample,
    )

    pixels = np.asarray(small, dtype=np.float64)

    freq = matrix @ pixels @ matrix.T

    block = freq[:8, :8].flatten()

    median = float(np.median(block[1:]))

    bits = "".join(
        "1" if v >= median else "0"
        for v in block
    )

    return int(bits, 2)


def _hamming(a: int, b: int) -> int:
    try:
        return (a ^ b).bit_count()

    except AttributeError:
        return bin(a ^ b).count("1")


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}


def _fetch_phash(url: str):
    """下载一张图并算 (pHash, 主色列表)。任何失败返回 None，绝不上抛。

    V2.11.5：同一次下载顺手算主色（match_funnel 的颜色门用），
    不再多下一遍图。
    """
    try:
        resp = requests.get(
            url,
            headers=_HEADERS,
            timeout=12,
            stream=True,
        )

        if resp.status_code != 200:
            return None

        buffer = io.BytesIO()

        for chunk in resp.iter_content(65536):
            buffer.write(chunk)

            if buffer.tell() > 8 * 1024 * 1024:
                return None

        from .match_funnel import dominant_colors

        with Image.open(buffer) as image:
            image.load()

            return (phash_image(image), dominant_colors(image))

    except Exception:
        return None


def prefetch_phashes(
    urls: list[str],
    workers: int = 8,
) -> dict[str, object]:
    """并发算一批 URL 的 pHash，写进会话缓存（重复 URL 不重下）。"""
    cache = st.session_state.setdefault(
        "src_phash_cache",
        {},
    )

    todo = sorted(
        {
            url
            for url in urls
            if url and url not in cache
        }
    )

    if todo:
        with ThreadPoolExecutor(
            max_workers=min(workers, max(len(todo), 1)),
        ) as pool:
            for url, value in zip(
                todo,
                pool.map(_fetch_phash, todo),
            ):
                cache[url] = value

    return cache


def _cache_phash(value):
    """缓存值可能是 V2.11.5 之前的裸 pHash（int），兼容取值。"""
    return value[0] if isinstance(value, tuple) else value


def image_similarity(
    url_a: str,
    url_b: str,
    cache: dict,
):
    """两张图的相似度 0~1；任一图取不到返回 None。"""
    if not url_a or not url_b:
        return None

    hash_a = _cache_phash(cache.get(url_a))
    hash_b = _cache_phash(cache.get(url_b))

    if hash_a is None or hash_b is None:
        return None

    return 1.0 - _hamming(hash_a, hash_b) / 64.0


def image_colors(url: str, cache: dict):
    """V2.11.5：从缓存取主色列表 [(色名, 占比)]；取不到返回 None。"""
    if not url:
        return None

    value = cache.get(url)

    return value[1] if isinstance(value, tuple) else None


# =====================================================
# 综合匹配分
# =====================================================


def _compact(text: str) -> str:
    return re.sub(
        r"\s+",
        "",
        _cell_str(text).lower(),
    )


def _name_hits(
    title_en: str,
    cand_title: str,
    exclude=(),
) -> int:
    """英文标题里的实词（≥3 字符）在候选标题里命中几个。

    品牌词另算分（品牌 10），这里排除掉避免重复计分。
    """
    haystack = _compact(cand_title)

    drop = {
        _compact(word)
        for word in exclude
    }

    tokens = {
        token.lower()
        for token in re.findall(
            r"[A-Za-z0-9]{3,}",
            _cell_str(title_en),
        )
        if not token.isdigit()
    } - drop

    if not tokens:
        return 0

    return sum(
        1
        for token in tokens
        if token in haystack
    )


def score_candidate(
    item: dict,
    cand: dict,
    img_sim,
    color_state=None,
    spec_pen: int = 0,
) -> tuple[int, dict]:
    """一个候选供应商的综合分。返回 (总分, 分项)。

    V2.11.5 漏斗加成：color_state（"ok" 加 3 / "bad" 扣 15，
    来自 match_funnel 颜色门）；spec_pen（规格冲突条数，每条扣 8 封顶 20）。
    """
    cand_title = _cell_str(cand.get("title"))
    signals = _cell_str(cand.get("signals"))
    hay = _compact(cand_title + " " + signals)

    parts = {}

    # 型号精确命中（40）
    model = _compact(item.get("model"))

    parts["型号"] = (
        W_MODEL
        if model and model in hay
        else 0
    )

    # 图片相似（35，线性）
    parts["图片"] = (
        round(W_IMAGE * img_sim)
        if img_sim is not None
        else 0
    )

    # 品牌命中（10）
    brand = _compact(item.get("brand"))

    parts["品牌"] = (
        W_BRAND
        if brand and brand in _compact(cand_title)
        else 0
    )

    # 品名关键词命中（10）：命中 1 个 3 分封顶 10（品牌词不重复计）
    parts["品名"] = min(
        W_NAME,
        3 * _name_hits(
            item.get("title_en", ""),
            cand_title,
            exclude=[item.get("brand", "")],
        ),
    )

    # 商家信号（5）：图搜同款 3 + 超级工厂 1 + 实力商家 1
    seller = 0

    if "图搜同款" in signals:
        seller += 3

    if "超级工厂" in signals:
        seller += 1

    if "实力商家" in signals:
        seller += 1

    parts["商家"] = min(W_SELLER, seller)

    # V2.11.5：颜色门 / 规格冲突（match_funnel 提供）
    if color_state == "ok":
        parts["颜色"] = 3

    elif color_state == "bad":
        parts["颜色"] = -15

    if spec_pen:
        parts["规格"] = -min(20, 8 * spec_pen)

    return sum(parts.values()), parts


# =====================================================
# 本地批次登记（tasks/sourcing/batches.json）
#
# Worker 里存任务和结果（权威数据）；这里只存
# 「批次任务 ↔ Excel 行」的映射和选中结果，用于展示和导出。
# =====================================================


def load_local_batches() -> list:
    data = load_json(
        SRC_STORE,
        default=[],
    )

    return data if isinstance(data, list) else []


def save_local_batches(batches: list) -> None:
    try:
        SRC_STORE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        save_json(SRC_STORE, batches)

    except Exception:
        pass


def append_local_batch(record: dict) -> None:
    batches = load_local_batches()
    batches.insert(0, record)
    save_local_batches(batches[:30])


# =====================================================
# 导出：原表右侧补找货五列
# =====================================================


def export_sourcing(
    dataframe: pd.DataFrame,
    picks: dict[int, dict],
) -> io.BytesIO:
    """picks = {行位置: {采购链接/采购价/起订量/供应商/匹配分}}。"""
    if dataframe is None:
        raise ValueError("原始 Excel 不存在，请先在左侧上传")

    result = dataframe.copy()

    for col in SRC_EXPORT_COLUMNS:
        if col not in result.columns:
            result[col] = ""

    for pos, values in picks.items():
        try:
            idx = result.index[int(pos)]

        except Exception:
            continue

        for key, value in values.items():
            if key in result.columns:
                result.at[idx, key] = value

    output = io.BytesIO()

    result.to_excel(
        output,
        index=False,
        sheet_name="导入产品模板",
    )

    output.seek(0)

    return output


# =====================================================
# 页面
# =====================================================


def _current_user() -> str:
    try:
        return str(
            st.session_state.get("auth_user", "") or ""
        )

    except Exception:
        return ""


def _view_batches(src_key: str, with_results: bool = True) -> list:
    """调 /src_tasks_view 拿批次（新→旧）。失败返回 []。"""
    data = src_api(
        "/src_tasks_view",
        {"key": src_key, "with_results": with_results},
    )

    if not data.get("ok"):
        raise RuntimeError(
            str(data.get("error") or "读取批次失败")
        )

    batches = data.get("batches")

    return batches if isinstance(batches, list) else []


def _counts_line(counts: dict) -> str:
    counts = counts if isinstance(counts, dict) else {}

    return (
        f"待认领 {counts.get('pending', 0)}"
        f"｜执行中 {counts.get('running', 0)}"
        f"｜完成 {counts.get('done', 0)}"
        f"｜失败 {counts.get('fail', 0)}"
    )


def render_sourcing_page(
    envelope,
    uploaded_name: str,
    api_key: str,
    model: str,
) -> None:
    """1688 找货主页面（仅管理员可见，见 sourcing_allowed）。"""
    st.markdown(
        """
        <div class="hint-bar">
        <b>🔎 1688 找货：</b>勾选产品 → AI 转中文搜索词 → 创建任务 →
        老板 Chrome 的采集插件自动跑 1688 搜索 → 全部完成后计算匹配分 →
        导出带采购链接的 Excel。
        </div>
        """,
        unsafe_allow_html=True,
    )

    src_key = get_src_key()

    if not src_key:
        st.warning(
            "还没配置找货密钥：管理后台（Worker /manage）点「生成找货密钥」，"
            "再把密钥填进优化程序 Secrets 的 SRC_KEY。"
        )

        return

    if envelope is None or envelope.dataframe is None:
        st.info(
            "👈 先在左侧上传采集 Excel（或用「从采集插件粘贴」），"
            "这里就会出现可勾选的产品列表。"
        )

        return

    dataframe = envelope.dataframe

    title_col = _find_col(dataframe, "标题(必填)", "标题")
    sku_col = _find_col(dataframe, "父SKU(必填)", "SKU")
    image_col = _find_col(dataframe, "产品图")
    ref_col = _find_col(dataframe, "参考网址")

    if title_col is None:
        st.error("表里找不到标题列（标题/标题(必填)）")

        return

    st.markdown("#### 1️⃣ 勾选要找货的产品")

    pick_rows = []

    for pos, (_, row) in enumerate(dataframe.iterrows()):
        title = _cell_str(row.get(title_col))

        if not title:
            continue

        image_url = (
            first_url(row.get(image_col))
            if image_col
            else ""
        )

        pick_rows.append(
            {
                "选择": False,
                "行": pos + 2,  # Excel 实际行号（含表头）
                "SKU": _cell_str(row.get(sku_col)) if sku_col else "",
                "标题": title[:40],
                "型号(自动)": fallback_model(title),
                "有图": "✓" if image_url else "—",
            }
        )

    if not pick_rows:
        st.warning("表里没有带标题的产品行")

        return

    pick_frame = pd.DataFrame(pick_rows)

    edited = st.data_editor(
        pick_frame,
        key="src_pick_editor",
        num_rows="fixed",
        use_container_width=True,
        disabled=["行", "SKU", "标题", "型号(自动)", "有图"],
        hide_index=True,
        column_config={
            "标题": st.column_config.TextColumn(
                width="medium",
            ),
        },
    )

    chosen_positions = [
        int(row["行"]) - 2
        for _, row in edited.iterrows()
        if bool(row["选择"])
    ]

    selected_count = len(chosen_positions)

    st.caption(
        f"已勾选 {selected_count} 个产品"
        "（建议一次 ≤ 50 个：插件每个任务约 20~30 秒）"
    )

    if selected_count == 0:
        _render_batches_section(
            dataframe,
            src_key,
            uploaded_name,
        )

        return

    # --------------------------------------------
    # 2. AI 搜索词转换
    # --------------------------------------------

    st.markdown("#### 2️⃣ AI 转换搜索词（英文标题 → 中文采购词）")

    convert_key = "src_convert_cache"

    if st.button(
        f"🤖 AI 转换这 {selected_count} 个标题",
        type="primary",
        key="src_convert_btn",
        disabled=not api_key.strip(),
    ):
        if not api_key.strip():
            st.error("左侧 API Key 没配置，无法调用 AI")

        else:
            titles = [
                _cell_str(
                    dataframe.iloc[pos].get(title_col),
                )
                for pos in chosen_positions
            ]

            progress_bar = st.progress(
                0.0,
                text="AI 转换中…",
            )

            try:
                converted = convert_search_terms(
                    titles,
                    api_key,
                    model,
                    progress=lambda p: progress_bar.progress(
                        min(p, 1.0),
                        text="AI 转换中…",
                    ),
                )

            except Exception as exc:
                st.error(f"AI 转换失败：{exc}")

                converted = None

            finally:
                progress_bar.empty()

            if converted is not None:
                mapping = {}

                for pos, entry in zip(chosen_positions, converted):
                    mapping[str(pos)] = {
                        "title_en": _cell_str(
                            dataframe.iloc[pos].get(title_col),
                        )[:200],
                        **entry,
                    }

                st.session_state[convert_key] = mapping

    if not api_key.strip():
        st.caption(
            "💡 没配 AI Key 也能建任务：直接点下面「用原标题建任务」，"
            "搜索词 = 英文原标题（效果差一些，型号自动用本地提取）。"
        )

    convert_map = st.session_state.get(convert_key) or {}

    ready_positions = [
        pos
        for pos in chosen_positions
        if str(pos) in convert_map
    ]

    if ready_positions:
        edit_rows = []

        for pos in ready_positions:
            entry = convert_map[str(pos)]

            edit_rows.append(
                {
                    "行": pos + 2,
                    "标题": entry["title_en"][:36],
                    "搜索词": entry.get("kw", ""),
                    "型号": entry.get("model", ""),
                    "品牌": entry.get("brand", ""),
                }
            )

        st.caption("搜索词可以在这里直接改，改完再创建任务：")

        kw_editor = st.data_editor(
            pd.DataFrame(edit_rows),
            key="src_kw_editor",
            num_rows="fixed",
            use_container_width=True,
            disabled=["行", "标题"],
            hide_index=True,
        )

        fallback_rows = [
            entry
            for entry in convert_map.values()
            if entry.get("fallback")
        ]

        if fallback_rows:
            st.caption(
                f"⚠️ {len(fallback_rows)} 条 AI 转换失败，已用英文原标题兜底，"
                "建议手动改成中文搜索词。"
            )

    # --------------------------------------------
    # 3. 创建找货任务
    # --------------------------------------------

    st.markdown("#### 3️⃣ 创建找货任务")

    can_create = bool(ready_positions)

    create_col, plain_col = st.columns(2)

    create_clicked = create_col.button(
        f"📨 创建任务（{selected_count} 个，AI 搜索词）",
        type="primary",
        key="src_create_btn",
        disabled=not can_create,
        use_container_width=True,
    )

    plain_clicked = plain_col.button(
        "⚡ 跳过 AI，用原标题建任务",
        key="src_create_plain_btn",
        disabled=can_create,
        use_container_width=True,
        help="搜索词 = 英文标题前 60 字符 + 本地提取型号",
    )

    if create_clicked or plain_clicked:
        tasks = []
        items_meta = []

        positions = (
            ready_positions
            if create_clicked
            else chosen_positions
        )

        for pos in positions:
            row = dataframe.iloc[pos]

            title_en = _cell_str(row.get(title_col))

            image_url = (
                first_url(row.get(image_col))
                if image_col
                else ""
            )

            entry = (
                convert_map.get(str(pos))
                if create_clicked
                else None
            )

            if entry:
                kw = entry.get("kw", "")
                part_model = entry.get("model", "")
                brand = entry.get("brand", "")

            else:
                kw = title_en[:60]
                part_model = fallback_model(title_en)
                brand = ""

            if not kw and not image_url:
                continue

            tasks.append(
                {
                    "kw": kw[:60],
                    "title": title_en[:200],
                    "model": part_model[:60],
                    "image_url": image_url[:400],
                }
            )

            items_meta.append(
                {
                    "tid": "",
                    "pos": pos,
                    "sku": _cell_str(row.get(sku_col)) if sku_col else "",
                    "title_en": title_en[:200],
                    "kw": kw[:60],
                    "model": part_model[:60],
                    "brand": brand[:30],
                    "image_url": image_url,
                    "ref_url": (
                        first_url(row.get(ref_col))
                        if ref_col
                        else ""
                    ),
                }
            )

        if not tasks:
            st.error("没有可创建的任务（选中行既没标题也没产品图）")

        else:
            try:
                data = src_api(
                    "/src_task_add",
                    {
                        "key": src_key,
                        "by": _current_user()[:32] or "optimizer",
                        "note": f"优化端 {len(tasks)} 个",
                        "tasks": tasks,
                    },
                )

            except Exception as exc:
                data = {"ok": False, "error": f"连不上 Worker：{exc}"}

            if not data.get("ok"):
                st.error(
                    f"创建失败：{data.get('error') or '未知错误'}"
                )

            else:
                batch_id = str(data.get("batch_id"))

                for index, meta in enumerate(items_meta):
                    meta["tid"] = f"t{index + 1}"

                append_local_batch(
                    {
                        "batch_id": batch_id,
                        "created_at": datetime.now()
                        .astimezone()
                        .isoformat(timespec="seconds"),
                        "count": len(items_meta),
                        "excel_name": str(uploaded_name or ""),
                        "items": items_meta,
                    }
                )

                st.session_state["src_active_batch"] = batch_id
                st.session_state.pop(convert_key, None)
                st.session_state.pop("src_scores", None)

                try:
                    from services.user_auth import (
                        log_user_event,
                    )

                    log_user_event(
                        "src_batch",
                        rows=len(items_meta),
                        batch_id=batch_id,
                    )

                except Exception:
                    pass

                st.success(
                    f"✅ 批次已创建：{batch_id}"
                    f"（{len(items_meta)} 个任务）。"
                    "打开老板 Chrome 的采集插件点「开始找货」，"
                    "插件会自动认领执行；这里每 20 秒自动刷新进度。"
                )

    # --------------------------------------------
    # 4. 批次进度 + 结果 + 导出
    # --------------------------------------------

    _render_batches_section(
        dataframe,
        src_key,
        uploaded_name,
    )


def _render_batches_section(
    dataframe: pd.DataFrame,
    src_key: str,
    uploaded_name: str,
) -> None:
    """批次列表、进度轮询、匹配分与导出。"""
    local_batches = load_local_batches()

    if not local_batches:
        st.markdown("#### 4️⃣ 找货批次")

        st.caption("还没有批次。上面勾选产品创建第一个找货任务。")

        return

    options = [
        f"{record['batch_id']}（{record.get('count', '?')} 个 · "
        f"{str(record.get('created_at', ''))[:16]}）"
        for record in local_batches[:8]
    ]

    default_index = 0

    active = st.session_state.get("src_active_batch")

    if active:
        for index, record in enumerate(local_batches[:8]):
            if record.get("batch_id") == active:
                default_index = index

                break

    st.markdown("#### 4️⃣ 找货批次与结果")

    choice = st.selectbox(
        "批次",
        options,
        index=default_index,
        key="src_batch_select",
    )

    record = local_batches[:8][options.index(choice)]

    batch_id = record.get("batch_id", "")

    refresh_col, _ = st.columns([1, 3])

    with refresh_col:
        if st.button(
            "🔄 刷新进度",
            key="src_refresh_btn",
            use_container_width=True,
        ):
            st.session_state.pop(f"src_view_{batch_id}", None)

    try:
        data = src_api(
            "/src_tasks_view",
            {"key": src_key, "batch_id": batch_id, "with_results": True},
        )

    except Exception as exc:
        st.error(f"读取批次失败：{exc}")

        return

    if not data.get("ok"):
        st.error(
            f"读取批次失败：{data.get('error') or '未知错误'}"
        )

        return

    batches = data.get("batches") or []

    if not batches:
        st.warning(
            "Worker 上查不到这个批次（可能已被后台删除）。"
        )

        return

    batch = batches[0]

    counts = batch.get("counts") or {}

    st.markdown(
        f"**{_counts_line(counts)}**"
    )

    tasks_by_tid = {
        str(task.get("tid")): task
        for task in batch.get("tasks", [])
    }

    items = record.get("items", [])

    pending = (
        counts.get("pending", 0)
        + counts.get("running", 0)
    )

    # ---- 自动刷新（20 秒，只刷这一小块）----
    if pending > 0:
        _render_live_status(src_key, batch_id, counts)

        st.info(
            "插件还没跑完（上面每 20 秒自动更新）。"
            "确认老板 Chrome 的插件已点「开始找货」。"
        )

    done_items = [
        item
        for item in items
        if str(
            tasks_by_tid.get(
                str(item.get("tid")),
                {},
            ).get("status")
        )
        in {"done", "fail"}
    ]

    if not done_items:
        return

    st.markdown(
        f"#### 5️⃣ 匹配结果（已回传 {len(done_items)}"
        f"/{len(items)} 个产品）"
    )

    score_key = "src_scores"

    scored_map = st.session_state.setdefault(score_key, {})

    need_score = [
        item
        for item in done_items
        if f"{batch_id}/{item['tid']}" not in scored_map
        and tasks_by_tid.get(item["tid"], {}).get("status") == "done"
        and tasks_by_tid.get(item["tid"], {}).get("n_results")
    ]

    if st.button(
        "🎯 计算匹配分（含图片对比）",
        type="primary",
        key="src_score_btn",
        disabled=not need_score,
    ):
        urls = []

        for item in need_score:
            if item.get("image_url"):
                urls.append(item["image_url"])

            for cand in tasks_by_tid.get(item["tid"], {}).get("results", []):
                if cand.get("img"):
                    urls.append(cand["img"])

        with st.spinner(
            f"下载对比图片（约 {len(set(urls))} 张）…",
        ):
            cache = prefetch_phashes(urls)

        for item in need_score:
            task = tasks_by_tid.get(item["tid"], {})

            scored = []

            for cand in task.get("results", []):
                img_sim = image_similarity(
                    item.get("image_url", ""),
                    cand.get("img", ""),
                    cache,
                )

                total, parts = score_candidate(
                    item,
                    cand,
                    img_sim,
                )

                scored.append(
                    {
                        "cand": cand,
                        "score": total,
                        "parts": parts,
                        "img_sim": img_sim,
                    }
                )

            scored.sort(
                key=lambda entry: entry["score"],
                reverse=True,
            )

            scored_map[f"{batch_id}/{item['tid']}"] = scored

    _render_results(
        dataframe,
        batch_id,
        items,
        tasks_by_tid,
        scored_map,
        uploaded_name,
    )


def _render_live_status(
    src_key: str,
    batch_id: str,
    counts: dict,
) -> None:
    """pending 时每 20 秒自动拉一次进度的独立小块。"""
    fragment = getattr(st, "fragment", None) or getattr(
        st,
        "experimental_fragment",
        None,
    )

    if fragment is None:
        st.caption(
            "插件执行中…点「🔄 刷新进度」查看最新状态。"
        )

        return

    @fragment(run_every=20)
    def _live():
        try:
            data = src_api(
                "/src_tasks_view",
                {
                    "key": src_key,
                    "batch_id": batch_id,
                    "with_results": False,
                },
            )

            live_counts = (
                (data.get("batches") or [{}])[0].get("counts")
                if data.get("ok")
                else None
            )

        except Exception:
            live_counts = None

        if live_counts is None:
            st.caption("⏳ 插件执行中…（自动刷新暂时连不上 Worker）")

            return

        left = (
            live_counts.get("pending", 0)
            + live_counts.get("running", 0)
        )

        if left > 0:
            st.caption(
                f"⏳ 插件执行中… {_counts_line(live_counts)}（每 20 秒自动更新）"
            )

        else:
            st.caption(
                f"✅ 插件已跑完！{_counts_line(live_counts)} —"
                "点「🔄 刷新进度」查看结果。"
            )

    _live()


def _render_results(
    dataframe: pd.DataFrame,
    batch_id: str,
    items: list,
    tasks_by_tid: dict,
    scored_map: dict,
    uploaded_name: str,
) -> None:
    choices = st.session_state.setdefault("src_choice", {})

    picks: dict[int, dict] = {}

    pick_labels = []

    for item in items:
        tid = str(item.get("tid"))

        task = tasks_by_tid.get(tid, {})

        status = task.get("status")

        pos = int(item.get("pos", -1))

        title = item.get("title_en") or task.get("title") or ""

        header = (
            f"行{pos + 2}｜{str(title)[:40] or tid}"
        )

        with st.expander(header):
            st.markdown(
                f"搜索词：**{item.get('kw', '')}**　"
                f"型号：**{item.get('model') or '—'}**　"
                f"品牌：**{item.get('brand') or '—'}**"
            )

            if item.get("ref_url"):
                st.markdown(
                    f"采集链接：{item['ref_url']}"
                )

            if status == "fail":
                st.error(
                    f"插件回报失败：{task.get('error') or '未返回结果'}"
                )

                continue

            results = task.get("results") or []

            if not results:
                st.warning(
                    "这个任务还没有候选结果（可能插件未回传）。"
                )

                continue

            scored = scored_map.get(f"{batch_id}/{tid}")

            if not scored:
                st.caption(
                    f"收到 {len(results)} 个候选。"
                    "点上方「🎯 计算匹配分」出推荐。"
                )

                _render_candidates_table(results)

                continue

            choice_key = f"{batch_id}/{tid}"

            chosen_index = choices.get(choice_key, 0)

            chosen_index = min(chosen_index, len(scored) - 1)

            # 先出「换候选」下拉框，再用当前选择渲染推荐 ——
            # 这样换选后同一次刷新就能看到新分数，不用多点一下。
            if len(scored) > 1:
                labels = [
                    f"{index + 1}. {entry['score']}分 · "
                    f"{str(entry['cand'].get('title', ''))[:24]}"
                    for index, entry in enumerate(scored[:12])
                ]

                new_label = st.selectbox(
                    "换一个候选",
                    labels,
                    index=chosen_index,
                    key=f"src_cand_{batch_id}_{tid}",
                )

                try:
                    chosen_index = (
                        int(str(new_label).split(".")[0]) - 1
                    )

                except Exception:
                    chosen_index = 0

                choices[choice_key] = chosen_index

            top = scored[chosen_index]

            # ✅推荐 = 得分最高的第 1 个
            st.markdown(
                f"{'✅ 推荐' if chosen_index == 0 else '⭐ 已手选'}"
                f"　匹配分 **{top['score']}**/100"
            )

            st.markdown(f"采购链接：{_buy_url(top['cand'])}")

            part_text = "　".join(
                f"{name} {value}"
                for name, value in top["parts"].items()
                if value
            )

            if part_text:
                st.caption(f"分项：{part_text}")

            c1, c2 = st.columns(2)

            with c1:
                if item.get("image_url"):
                    st.image(
                        item["image_url"],
                        width=180,
                        caption="亚马逊产品图",
                    )

            with c2:
                if top["cand"].get("img"):
                    st.image(
                        top["cand"]["img"],
                        width=180,
                        caption="1688 候选图",
                    )

            _render_candidates_table(
                [entry["cand"] for entry in scored],
                scores=[entry["score"] for entry in scored],
            )

            cand = top["cand"]

            picks[pos] = {
                "采购链接": _buy_url(cand),
                "采购价": cand.get("price", ""),
                "起订量": cand.get("moq", ""),
                "供应商": cand.get("company", ""),
                "匹配分": str(top["score"]),
            }

            pick_labels.append(
                f"行{pos + 2} → {cand.get('company', '')}"
                f"（{top['score']}分）"
            )

    if picks:
        st.markdown("#### 6️⃣ 导出")

        st.caption(
            "已选出 "
            + str(len(picks))
            + " 个："
            + "；".join(pick_labels[:6])
            + ("…" if len(pick_labels) > 6 else "")
        )

        try:
            output = export_sourcing(dataframe, picks)

            safe_stem = re.sub(
                r"\.xlsx$",
                "",
                str(uploaded_name or "找货"),
                flags=re.I,
            )

            st.download_button(
                "⬇️ 导出找货结果（Excel，原表+采购五列）",
                data=output.getvalue(),
                file_name=f"{safe_stem}_V2.8_找货结果.xlsx",
                mime=(
                    "application/vnd.openxmlformats-"
                    "officedocument.spreadsheetml.sheet"
                ),
                type="primary",
                key="src_export_btn",
                use_container_width=True,
            )

        except Exception as exc:
            st.error(f"生成找货文件失败：{exc}")


def _buy_url(cand: dict) -> str:
    """候选的 1688 采购链接；没带 url 时用 offerId 拼。"""
    url = _cell_str(cand.get("url"))

    if url:
        return url

    offer_id = _cell_str(cand.get("offer_id"))

    if offer_id:
        return f"https://detail.1688.com/offer/{offer_id}.html"

    return "—"


def _render_candidates_table(
    results: list,
    scores=None,
) -> None:
    rows = []

    for index, cand in enumerate(results[:12]):
        row = {
            "匹配分": (
                str(scores[index]) if scores else "-"
            ),
            "价格": cand.get("price", ""),
            "起订量": cand.get("moq", ""),
            "供应商": cand.get("company", ""),
            "标题": str(cand.get("title", ""))[:36],
            "来源": cand.get("from", ""),
            "信号": cand.get("signals", ""),
        }

        rows.append(row)

    if rows:
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )

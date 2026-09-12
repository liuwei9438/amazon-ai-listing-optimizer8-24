from __future__ import annotations

# =====================================================
# 产品资料库 V1.0
#
# 用途：把优化程序导出的 Excel（20 列「导入产品模板」+
# 短标题 / 商品亮点 等附加列）导入进来，卡片墙浏览产品
# 图片和文案，点开单个产品查看 / 编辑，最后导出回同样
# 格式的 Excel 继续走后续流程。
#
# 特点：只读 Excel + 账号登录，不调用 AI、不需要 API Key。
# V1 不存数据库：导入 → 查看/编辑 → 导出，一次会话内完成。
# =====================================================

import hashlib
import inspect
import io
import math
import re
from html import escape as html_escape

import pandas as pd
import streamlit as st


# =====================================================
# 纯函数区（不依赖 Streamlit 运行时，可单独测试）
# =====================================================

# 采集插件 / 优化导出统一的 20 列模板（与 listing_exporter 一致）
TEMPLATE_COLUMNS = [
    "父SKU(必填)", "SKU", "库存", "币种", "成本价(必填)", "运费",
    "材料", "包装材料", "语言", "标题(必填)", "颜色",
    "要点1", "要点2", "要点3", "要点4", "要点5",
    "简介", "产品图", "简介图", "参考网址",
]

# 优化结果附加列
KNOWN_EXTRA_COLUMNS = ("短标题", "商品亮点")

# 多行编辑的字段
TEXTAREA_COLUMNS = {"简介", "商品亮点", "产品图", "简介图", "参考网址"}

# 数值列：编辑后尽量存回数字（Excel 消费方更友好）
NUMERIC_COLUMNS = ("库存", "成本价(必填)", "运费")

# 卡片价格行的货币符号
CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "CNY": "¥", "CAD": "C$", "MXN": "MX$", "AUD": "A$",
}

# 每行卡片数
CARDS_PER_ROW = 5


def text_value(value) -> str:
    """NaN 安全的字符串化（空单元格 → ""）。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def split_images(value) -> list:
    """产品图单元格 → 图片链接列表（空格/换行/逗号/分号/竖线分隔）。"""
    text = text_value(value)
    if not text:
        return []
    return [part for part in re.split(r"[\s,;|]+", text) if part]


def display_images(value) -> list:
    """可直接渲染的图片链接：// 开头补全 https:，非 http(s) 杂项忽略。

    速卖通等页面抓下来的地址常是 //ae01.alicdn.com/...（不带协议），
    直接交给 st.image 会被当成"本地文件"打开而报错，这里统一修好。
    """
    result = []
    for token in split_images(value):
        if token.startswith("//"):
            token = "https:" + token
        if token.lower().startswith(("http://", "https://")):
            result.append(token)
    return result


def normalize_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """列排序：模板列在前（模板顺序）→ 已知附加列 → 其余列殿后（数据不丢）。"""
    result = frame.copy().dropna(how="all")
    ordered = [c for c in TEMPLATE_COLUMNS if c in result.columns]
    ordered += [c for c in KNOWN_EXTRA_COLUMNS if c in result.columns]
    ordered += [c for c in result.columns if c not in ordered]
    return result[ordered]


def looks_like_product_table(frame: pd.DataFrame) -> bool:
    """至少认识 2 个模板列才当作产品表，防误传无关文件。"""
    known = TEMPLATE_COLUMNS + list(KNOWN_EXTRA_COLUMNS)
    hits = sum(1 for c in known if c in frame.columns)
    return hits >= 2


def price_display(row) -> str:
    """卡片价格行 HTML：币种+成本价大字，运费小字附加。"""
    currency = text_value(row.get("币种")).upper()
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} " if currency else "")
    price = text_value(row.get("成本价(必填)"))
    shipping = text_value(row.get("运费"))
    if price:
        html = f"<b>{html_escape(symbol + price)}</b>"
    else:
        html = "<b>-</b>"
    if shipping:
        html += f' <span class="p-ship">+ {html_escape(shipping)}</span>'
    return html


def restore_numeric(column: str, value):
    """库存/成本价/运费 编辑后尽量存回数字；转不动就保留原文。"""
    if column not in NUMERIC_COLUMNS:
        return value
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text
    if number == int(number):
        return int(number)
    return number


def to_xlsx_bytes(frame: pd.DataFrame) -> bytes:
    """当前表格 → xlsx 字节（列顺序不变，顺手设一下列宽）。"""
    from openpyxl.utils import get_column_letter

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="导入产品模板")
        sheet = writer.sheets["导入产品模板"]
        for position, column in enumerate(frame.columns, start=1):
            if column == "标题(必填)":
                width = 60
            elif column in TEXTAREA_COLUMNS:
                width = 40
            elif column in ("父SKU(必填)", "SKU"):
                width = 20
            else:
                width = 14
            sheet.column_dimensions[get_column_letter(position)].width = width
    return buffer.getvalue()


def merge_frames(old, new):
    """多文件累积导入：新表出现过的 SKU，旧表同名行让位（新覆盖旧）。

    新表索引整体错开，行号永不撞车 —— 已编辑标记、翻页状态都不乱。
    """
    if old is None or old.empty:
        return new
    new_skus = set()
    if "SKU" in new.columns:
        new_skus = {text_value(v) for v in new["SKU"].tolist() if text_value(v)}
    if new_skus and "SKU" in old.columns:
        keep = [
            i for i, v in old["SKU"].items()
            if not text_value(v) or text_value(v) not in new_skus
        ]
        old = old.loc[keep]
    offset = (int(old.index.max()) + 1) if len(old) else 0
    shifted = new.copy()
    shifted.index = range(offset, offset + len(shifted))
    return normalize_dataframe(pd.concat([old, shifted]))


def missing_flags(row) -> list:
    """缺什么一眼看到：无图 / 缺标题 / 缺要点 / 缺简介 / 缺价格。"""
    flags = []
    if not display_images(row.get("产品图")):
        flags.append("无图")
    if not text_value(row.get("标题(必填)")):
        flags.append("缺标题")
    for label, column in (("缺要点", "要点1"), ("缺简介", "简介")):
        if column in row.index and not text_value(row.get(column)):
            flags.append(label)
    if not text_value(row.get("成本价(必填)")):
        flags.append("缺价格")
    return flags


# 标题体检词表：能借优化程序的品牌词库就借，借不到用同内容兜底
try:
    from compliance.brand_protection import DEFAULT_BRANDS, PROHIBITED_TERMS
except BaseException:  # noqa: BLE001  词库文件不在也不影响主流程
    DEFAULT_BRANDS = {
        "LG", "Dyson", "Epson", "Samsung", "Bosch", "Philips", "Whirlpool",
    }
    PROHIBITED_TERMS = {
        "original", "genuine", "official", "oem",
        "authentic", "best seller", "#1", "premium quality",
    }


def _terms_regex(terms):
    """词表 → 正则：前后都不能是字母数字（#1 这种带符号的也判得准）。"""
    parts = [
        r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        for term in sorted(terms, key=len, reverse=True)
    ]
    if not parts:
        return re.compile(r"(?!x)x")  # 空词表：永不匹配
    return re.compile("|".join(parts), re.IGNORECASE)


_BRAND_RE = _terms_regex(DEFAULT_BRANDS)
_PROHIBITED_RE = _terms_regex(PROHIBITED_TERMS)


def title_issues(title) -> list:
    """标题体检：超长 / 几乎全大写 / 品牌词 / 违禁词。纯规则，不调 AI。"""
    text = text_value(title)
    if not text:
        return []
    issues = []
    if len(text) > 200:
        issues.append(f"超长：{len(text)}/200 字符")
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:
        issues.append("几乎全大写")
    if _BRAND_RE.search(text):
        issues.append("含品牌词，建议改成 Compatible with XX")
    if _PROHIBITED_RE.search(text):
        issues.append("含违禁词（best seller / #1 / OEM 等）")
    return issues


def arrange_indices(frame, indices, filter_mode, sort_mode, edited) -> list:
    """搜索命中后，再按 筛选/排序 整理显示顺序（只动顺序，不动数据）。"""
    picked = list(indices)
    if filter_mode == "只看无图":
        picked = [
            i for i in picked
            if not display_images(
                frame.at[i, "产品图"] if "产品图" in frame.columns else ""
            )
        ]
    elif filter_mode == "只看缺字段":
        picked = [i for i in picked if missing_flags(frame.loc[i])]
    elif filter_mode == "只看已编辑":
        picked = [i for i in picked if i in edited]

    if sort_mode in ("价格从低到高", "价格从高到低"):
        prices = {}
        for i in picked:
            try:
                prices[i] = float(frame.at[i, "成本价(必填)"])
            except (KeyError, TypeError, ValueError):
                prices[i] = float("nan")
        reverse = sort_mode == "价格从高到低"

        def _key(i):
            value = prices[i]
            if math.isnan(value):
                return (1, 0.0)  # 没价格的一律垫底
            return (0, -value if reverse else value)

        picked.sort(key=_key)
    elif sort_mode == "缺字段在前":
        picked.sort(key=lambda i: not missing_flags(frame.loc[i]))
    elif sort_mode == "最近编辑在前":
        picked.sort(key=lambda i: i not in edited)
    return picked


# =====================================================
# 应用主体
# =====================================================

try:
    from services.user_auth import (
        auth_enabled,
        is_admin_user,
        log_user_event,
        render_sidebar_badge,
        require_login,
        sync_session_cookie,
    )
except BaseException as auth_error:
    try:
        st.set_page_config(page_title="产品资料库")
    except Exception:
        pass
    st.error(f"登录模块加载失败：{auth_error}")
    st.stop()


VERSION = "V1.1"

st.set_page_config(
    page_title="产品资料库",
    layout="wide",
)

CUSTOM_CSS = """
<style>
.stApp { background: #f6f7f9; }

/* ---- 顶部横幅 ---- */
.app-hero {
    background: linear-gradient(120deg, #232F3E 0%, #37475A 78%);
    color: #ffffff;
    padding: 18px 28px 14px 28px;
    border-radius: 14px;
    margin-bottom: 14px;
}
.hero-title { font-size: 24px; font-weight: 800; }
.hero-sub { color: #d5dbd1; margin-top: 4px; font-size: 13px; }
.version-pill {
    display: inline-block;
    background: #FF9900;
    color: #232F3E;
    font-weight: 700;
    border-radius: 999px;
    padding: 2px 12px;
    font-size: 12px;
    margin-top: 8px;
}

/* ---- 产品卡片 ---- */
[data-testid="stImageContainer"] img {
    border-radius: 10px 10px 0 0;
    border: 1px solid #e6e8eb;
    border-bottom: none;
    background: #fff;
}
.p-ph {
    aspect-ratio: 1/1;
    display: flex; flex-direction: column; gap: 4px;
    align-items: center; justify-content: center;
    background: #fafbfc;
    border: 1px solid #e6e8eb;
    border-bottom: none;
    border-radius: 10px 10px 0 0;
    color: #aab4c0;
    font-size: 13px;
}
.p-meta {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 0 0 10px 10px;
    padding: 8px 10px;
    min-height: 96px;
}
.p-price { color: #B12704; font-size: 17px; font-weight: 800; }
.p-ship { color: #8a94a0; font-size: 12px; font-weight: 400; }
.p-title {
    font-size: 12.5px; color: #232F3E;
    margin-top: 4px; line-height: 1.45;
    display: -webkit-box; -webkit-line-clamp: 3;
    -webkit-box-orient: vertical; overflow: hidden;
}
.p-sku { font-size: 11.5px; color: #5b6b7a; margin-top: 6px; }
.p-badge {
    background: #FF9900; color: #232F3E;
    border-radius: 999px; padding: 0 6px;
    font-size: 10.5px; font-weight: 700;
}
.p-flags { margin-top: 5px; line-height: 1.9; }
.p-flag {
    background: #fdecea; color: #B12704;
    border-radius: 4px; padding: 1px 5px;
    font-size: 10px; font-weight: 600;
    display: inline-block; margin-right: 4px;
}
.page-info { text-align: center; padding-top: 8px; color: #5b6b7a; font-size: 13px; }
.detail-pos { padding-top: 8px; color: #37475A; font-weight: 600; font-size: 14px; text-align: center; }

/* ---- 新手引导 ---- */
.guide-card {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 14px;
    padding: 20px;
    height: 100%;
}
.guide-num {
    width: 34px; height: 34px;
    border-radius: 50%;
    background: #232F3E;
    color: #FF9900;
    font-weight: 800;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px;
    margin-bottom: 10px;
}
.guide-title { font-size: 16px; font-weight: 700; color: #232F3E; margin-bottom: 6px; }
.guide-text { font-size: 13px; color: #5b6b7a; line-height: 1.7; }

/* ---- 侧边栏 ---- */
.side-brand { font-size: 17px; font-weight: 800; color: #232F3E; padding: 6px 2px 2px 2px; }
.side-brand span { color: #FF9900; }
.side-step { font-weight: 700; font-size: 14px; color: #232F3E; margin: 16px 0 8px 0; }

#MainMenu, footer { visibility: hidden; }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =====================================================
# 账号门（和优化程序同一套账号）
# =====================================================

require_login()
sync_session_cookie()


# =====================================================
# 内测门：新工具先只给管理员账号测试用。
#
# - 员工就算拿到网址，登录后也只看到「内测中」提示；
# - 对全员开放时不用改代码：在 Secrets 里加一行
#       lib_open = "true"
#   然后重启应用即可（不加 = 一直只限管理员）。
# =====================================================

def _lib_open_flag() -> bool:
    try:
        return str(
            st.secrets.get("lib_open", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


if (
    auth_enabled()
    and not _lib_open_flag()
    and not is_admin_user()
):
    st.markdown(
        f"""
        <div class="app-hero">
            <div class="hero-title">📚 产品资料库</div>
            <div class="hero-sub">内测中，暂未开放 · {VERSION}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.info("📚 产品资料库还在内测，暂未开放，请先用优化程序页面。")
    st.stop()


# =====================================================
# 侧边栏：导入 / 导出
# =====================================================

lib_error = ""

with st.sidebar:
    st.markdown(
        '<div class="side-brand">📚 产品<span>资料库</span></div>',
        unsafe_allow_html=True,
    )
    render_sidebar_badge()

    st.markdown(
        '<div class="side-step">1️⃣ 导入 Excel</div>',
        unsafe_allow_html=True,
    )
    uploaded = st.file_uploader(
        "上传优化后的 Excel",
        type=["xlsx"],
        key="lib_uploader",
    )

    if uploaded is not None:
        data = uploaded.getvalue()
        fingerprint = hashlib.sha1(data).hexdigest()
        seen = st.session_state.get("lib_fps") or set()
        if fingerprint not in seen:
            # 没见过的新文件：解析后并进资料库（同名 SKU 新的覆盖旧的）。
            try:
                frame = normalize_dataframe(
                    pd.read_excel(io.BytesIO(data))
                )
            except Exception as exc:
                lib_error = f"读取文件失败：{exc}"
            else:
                if frame.empty or not looks_like_product_table(frame):
                    lib_error = (
                        "这不是优化程序导出的表格"
                        "（没找到模板列，如 标题(必填) / 产品图 / SKU）。"
                    )
                else:
                    st.session_state["lib_df"] = merge_frames(
                        st.session_state.get("lib_df"), frame
                    )
                    st.session_state["lib_edited"] = (
                        st.session_state.get("lib_edited") or set()
                    )
                    seen.add(fingerprint)
                    st.session_state["lib_fps"] = seen
                    files = st.session_state.get("lib_files") or []
                    files.append(str(uploaded.name))
                    st.session_state["lib_files"] = files
                    st.session_state["lib_page"] = 1
                    st.session_state.pop("view_idx", None)
                    log_user_event(
                        "lib_import",
                        rows=len(frame),
                        filename=str(uploaded.name),
                    )

    df = st.session_state.get("lib_df")

    if df is not None:
        files = st.session_state.get("lib_files") or []
        note = f"（累计 {len(files)} 个文件）" if len(files) > 1 else ""
        st.success(f"已导入：{len(df)} 个产品{note}")

        st.markdown(
            '<div class="side-step">2️⃣ 导出 Excel</div>',
            unsafe_allow_html=True,
        )
        stem = re.sub(
            r"\.xlsx$",
            "",
            str(st.session_state.get("lib_name") or "产品表"),
            flags=re.I,
        )
        edited_count = len(st.session_state.get("lib_edited") or ())
        st.download_button(
            "⬇️ 导出当前表格（含修改）",
            data=to_xlsx_bytes(df),
            file_name=f"{stem}_资料库已编辑.xlsx",
            mime=(
                "application/vnd.openxmlformats-"
                "officedocument.spreadsheetml.sheet"
            ),
            type="primary",
            use_container_width=True,
            on_click=lambda: log_user_event(
                "export", rows=len(st.session_state.get("lib_df") or [])
            ),
        )
        if edited_count:
            st.caption(f"✏️ 本次已编辑 {edited_count} 个产品")

        if st.button("🗑 清空资料库（重新开始）"):
            for key in (
                "lib_fp", "lib_fps", "lib_df", "lib_edited", "lib_files",
                "lib_page", "lib_last_query", "view_idx",
            ):
                st.session_state.pop(key, None)
            st.rerun()

    st.caption(
        "V1 说明：可连续导入多个表格自动合并（按 SKU 去重，新的覆盖旧的）；"
        "数据只在本次会话里，改完记得导出。"
    )


# =====================================================
# 页面主体
# =====================================================

st.markdown(
    f"""
    <div class="app-hero">
        <div class="hero-title">📚 产品资料库</div>
        <div class="hero-sub">导入优化后的 Excel · 卡片浏览图片与文案 · 点击编辑 · 导出继续后续流程</div>
        <span class="version-pill">{VERSION}</span>
    </div>
    """,
    unsafe_allow_html=True,
)

if lib_error:
    st.error(lib_error)

if df is None:
    g1, g2, g3 = st.columns(3)
    with g1:
        st.markdown(
            """
            <div class="guide-card">
                <div class="guide-num">1</div>
                <div class="guide-title">📥 导入 Excel</div>
                <div class="guide-text">
                在左侧上传优化程序导出的 Excel，
                自动按 20 列模板识别产品。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with g2:
        st.markdown(
            """
            <div class="guide-card">
                <div class="guide-num">2</div>
                <div class="guide-title">🔍 查看 / 编辑</div>
                <div class="guide-text">
                卡片墙浏览图片、价格、标题；
                点「查看 / 编辑」进入详情页，
                所有字段直接修改保存。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with g3:
        st.markdown(
            """
            <div class="guide-card">
                <div class="guide-num">3</div>
                <div class="guide-title">📤 导出 Excel</div>
                <div class="guide-text">
                改完在左侧「导出当前表格」下载，
                格式不变，继续走后续流程。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.stop()


# ---- 搜索 / 每页数量 ----

search_col, size_col = st.columns([4, 1])
with search_col:
    query = st.text_input(
        "搜索",
        placeholder="搜索：SKU / 父SKU / 标题 / 短标题 关键词",
        key="lib_query",
    )
with size_col:
    page_size = st.selectbox(
        "每页显示",
        [20, 50, 100],
        index=1,
        key="lib_page_size",
    )

# ---- 筛选 / 排序 ----

filter_col, sort_col = st.columns(2)
with filter_col:
    filter_mode = st.selectbox(
        "筛选",
        ["全部", "只看无图", "只看缺字段", "只看已编辑"],
        key="lib_filter",
    )
with sort_col:
    sort_mode = st.selectbox(
        "排序",
        ["默认顺序", "价格从低到高", "价格从高到低", "缺字段在前", "最近编辑在前"],
        key="lib_sort",
    )

# 搜索词变了 → 回到第 1 页。
if query != st.session_state.get("lib_last_query", ""):
    st.session_state["lib_last_query"] = query
    st.session_state["lib_page"] = 1

indices = list(df.index)
if query.strip():
    needle = query.strip().lower()

    def _hit(idx) -> bool:
        row = df.loc[idx]
        haystack = " ".join(
            text_value(row.get(col))
            for col in ("SKU", "父SKU(必填)", "标题(必填)", "短标题")
        ).lower()
        return needle in haystack

    indices = [idx for idx in indices if _hit(idx)]

edited_rows = st.session_state.get("lib_edited") or set()
indices = arrange_indices(df, indices, filter_mode, sort_mode, edited_rows)
st.markdown(
    f"**共 {len(df)} 个产品** · 当前显示 {len(indices)} 个 · "
    f"✏️ 已编辑 {len(edited_rows)} 个"
)

# ---- 详情视图状态 ----

view_idx = st.session_state.get("view_idx")
if view_idx is not None and view_idx not in df.index:
    st.session_state.pop("view_idx", None)
    view_idx = None


# =====================================================
# 详情 / 编辑页
# =====================================================

TEXT_FIELD_ORDER = (
    ["标题(必填)", "短标题"]
    + [f"要点{i}" for i in range(1, 6)]
    + ["简介", "商品亮点"]
)
BASIC_FIELD_ORDER = [
    "父SKU(必填)", "SKU", "库存", "币种", "成本价(必填)", "运费",
    "材料", "包装材料", "语言", "颜色", "参考网址",
]


def _image_width_kwargs() -> dict:
    """st.image 自适应宽度参数：新版叫 use_container_width，
    云端 1.39 旧版叫 use_column_width —— 传错名字会直接 TypeError，
    当前版本能认哪个就传哪个（都认不出就不传）。"""
    try:
        params = inspect.signature(st.image).parameters
    except (TypeError, ValueError):
        return {}
    if "use_container_width" in params:
        return {"use_container_width": True}
    if "use_column_width" in params:
        return {"use_column_width": True}
    return {}


_IMAGE_WIDTH_KWARGS = _image_width_kwargs()


def _safe_image(url: str):
    """渲染图片，异常时降级成提示（单张坏图不拖垮整个页面）。"""
    try:
        st.image(str(url), **_IMAGE_WIDTH_KWARGS)
    except Exception:
        st.caption("🖼️ 该图片链接无法显示")


def _gallery(frame, idx, column, images):
    """图廊 + 单图删除：删完立刻写回单元格（导出的 Excel 同步少图）。"""
    gallery = st.columns(min(len(images), CARDS_PER_ROW))
    for i, url in enumerate(images):
        with gallery[i % len(gallery)]:
            _safe_image(url)
            st.markdown(f"[↗ 原图{i + 1}]({html_escape(str(url))})")
            if st.button("🗑 删掉此图", key=f"del_{column}_{idx}_{i}"):
                frame.at[idx, column] = " | ".join(
                    u for j, u in enumerate(images) if j != i
                )
                edited = st.session_state.get("lib_edited") or set()
                edited.add(idx)
                st.session_state["lib_edited"] = edited
                log_user_event(
                    "lib_img_del",
                    sku=text_value(frame.at[idx, "SKU"]) if "SKU" in frame.columns else "",
                )
                st.rerun()


def _form_field(idx, row, column: str):
    """渲染一个编辑控件（图片列一行一链接，其余按单行/多行）。"""
    key = f"fld_{idx}_{column}"
    if column in ("产品图", "简介图"):
        default = "\n".join(split_images(row.get(column)))
        st.text_area(
            column,
            value=default,
            height=90,
            key=key,
            help="每行一个图片链接",
        )
    elif column in TEXTAREA_COLUMNS:
        st.text_area(
            column,
            value=text_value(row.get(column)),
            height=150,
            key=key,
        )
    else:
        st.text_input(
            column,
            value=text_value(row.get(column)),
            key=key,
        )


def render_detail(frame: pd.DataFrame, idx, visible_indices):
    row = frame.loc[idx]
    position = (
        visible_indices.index(idx) if idx in visible_indices else -1
    )

    b_back, b_prev, b_pos, b_next = st.columns([1.2, 1, 2.2, 1])
    with b_back:
        if st.button("⬅️ 返回列表", use_container_width=True):
            st.session_state.pop("view_idx", None)
            st.rerun()
    with b_prev:
        if st.button(
            "⬆️ 上一个",
            disabled=position <= 0,
            use_container_width=True,
        ):
            st.session_state["view_idx"] = visible_indices[position - 1]
            st.rerun()
    with b_next:
        if st.button(
            "⬇️ 下一个",
            disabled=position < 0 or position >= len(visible_indices) - 1,
            use_container_width=True,
        ):
            st.session_state["view_idx"] = visible_indices[position + 1]
            st.rerun()
    with b_pos:
        sku_text = text_value(row.get("SKU")) or "（无SKU）"
        edited_mark = " · ✏️ 已编辑" if idx in (
            st.session_state.get("lib_edited") or set()
        ) else ""
        st.markdown(
            f'<div class="detail-pos">{position + 1} / {len(visible_indices)}'
            f" · SKU：{html_escape(sku_text)}{edited_mark}</div>",
            unsafe_allow_html=True,
        )

    # ---- 图片（图廊里可单张删除，删完立刻写回） ----
    st.markdown("#### 🖼️ 产品图")
    images = display_images(row.get("产品图"))
    if images:
        _gallery(frame, idx, "产品图", images)
    else:
        st.caption("（无图片链接）")

    intro_images = display_images(row.get("简介图"))
    if intro_images:
        st.markdown("#### 📷 简介图")
        _gallery(frame, idx, "简介图", intro_images)

    reference = text_value(row.get("参考网址"))
    if reference.startswith("http"):
        st.markdown(f"🔗 [打开参考网页]({html_escape(reference)})")

    # ---- 体检：缺字段 + 标题风险（纯规则，不调 AI） ----
    checkups = title_issues(row.get("标题(必填)")) + missing_flags(row)
    if checkups:
        st.warning("⚠️ 体检提醒：" + "；".join(checkups))

    # ---- 编辑表单 ----
    st.markdown("#### ✏️ 编辑信息（改完点底部「保存修改」）")

    text_columns = [c for c in TEXT_FIELD_ORDER if c in frame.columns]
    basic_columns = [c for c in BASIC_FIELD_ORDER if c in frame.columns]
    rendered = set(text_columns + basic_columns) | {"产品图", "简介图"}
    extra_columns = [c for c in frame.columns if c not in rendered]

    with st.form(key=f"form_{idx}"):
        left, right = st.columns([3, 2])

        with left:
            st.markdown("**文案**")
            for column in text_columns:
                _form_field(idx, row, column)

        with right:
            st.markdown("**图片与基础信息**")
            for column in ["产品图", "简介图"]:
                if column in frame.columns:
                    _form_field(idx, row, column)
            for column in basic_columns:
                _form_field(idx, row, column)
            if extra_columns:
                st.markdown("**其他列**")
                for column in extra_columns:
                    st.text_area(
                        column,
                        value=text_value(row.get(column)),
                        height=80,
                        key=f"fld_{idx}_{column}",
                    )

        submitted = st.form_submit_button(
            "💾 保存修改",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        saved_columns = text_columns + basic_columns + extra_columns + [
            "产品图", "简介图",
        ]
        changed = 0
        for column in saved_columns:
            if column not in frame.columns:
                continue
            key = f"fld_{idx}_{column}"
            if key not in st.session_state:
                continue
            new_value = restore_numeric(
                column, st.session_state[key]
            )
            frame.at[idx, column] = new_value
            changed += 1
        edited_set = st.session_state.get("lib_edited") or set()
        edited_set.add(idx)
        st.session_state["lib_edited"] = edited_set
        log_user_event("lib_edit", sku=text_value(row.get("SKU")))
        st.success(f"✅ 已保存（{changed} 个字段）→ 左侧可导出 Excel。")


# =====================================================
# 卡片墙 + 分页
# =====================================================

def render_grid(frame: pd.DataFrame, visible_indices):
    if not visible_indices:
        st.info("没有符合搜索条件的产品。")
        return

    total_pages = max(1, math.ceil(len(visible_indices) / int(page_size)))
    page = min(int(st.session_state.get("lib_page", 1)), total_pages)
    st.session_state["lib_page"] = page

    c_prev, c_info, c_next = st.columns([1, 2, 1])
    with c_prev:
        if st.button(
            "⬅️ 上一页",
            disabled=page <= 1,
            use_container_width=True,
        ):
            st.session_state["lib_page"] = page - 1
            st.rerun()
    with c_next:
        if st.button(
            "下一页 ➡️",
            disabled=page >= total_pages,
            use_container_width=True,
        ):
            st.session_state["lib_page"] = page + 1
            st.rerun()
    with c_info:
        st.markdown(
            f'<div class="page-info">第 {page} / {total_pages} 页</div>',
            unsafe_allow_html=True,
        )

    start = (page - 1) * int(page_size)
    page_indices = visible_indices[start:start + int(page_size)]
    edited = st.session_state.get("lib_edited") or set()

    columns = st.columns(CARDS_PER_ROW)
    for pos, idx in enumerate(page_indices):
        row = frame.loc[idx]
        with columns[pos % CARDS_PER_ROW]:
            images = display_images(row.get("产品图"))
            if images:
                _safe_image(images[0])
            else:
                st.markdown(
                    '<div class="p-ph">🖼️<br>无图片</div>',
                    unsafe_allow_html=True,
                )
            badge = (
                ' <span class="p-badge">已编辑</span>'
                if idx in edited else ""
            )
            flags_html = "".join(
                f'<span class="p-flag">{html_escape(flag)}</span>'
                for flag in missing_flags(row)[:3]
            )
            title = text_value(row.get("标题(必填)")) or "（无标题）"
            st.markdown(
                f"""
                <div class="p-meta">
                    <div class="p-price">{price_display(row)}</div>
                    <div class="p-title">{html_escape(title)}</div>
                    <div class="p-sku">SKU：{html_escape(text_value(row.get("SKU"))) or "-"}{badge}</div>
                    <div class="p-flags">{flags_html}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button(
                "查看 / 编辑",
                key=f"open_{idx}",
                use_container_width=True,
            ):
                st.session_state["view_idx"] = idx
                st.rerun()


if view_idx is not None:
    render_detail(df, view_idx, indices)
else:
    render_grid(df, indices)

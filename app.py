from __future__ import annotations


import hashlib
import json
import os
import re

import pandas as pd
import streamlit as st


from core import (
    read_workbook,
    export_unchanged,
    integrity_report,
)


from services.config import get_openai_api_key


from services.task_manager import (
    create_task,
    get_task_dir,
    load_status,
)

from services.task_control import save_control

from services.json_storage import load_json, save_json

from services.result_storage import (
    load_profiles,
    load_failed_items,
)


from services.current_task import (
    save_current_task,
    load_current_task,
    clear_current_task,
)


from services.task_worker import (
    start_worker,
)


from services.listing_exporter import (
    ListingExporter,
)

from image.image_storage import cloudinary_ready

from analyzer.title_strategy_generator import (
    TitleStrategyGenerator,
)

VERSION = "V2.6.0-EMP-1"


TASK_RUNNING_STATUS = [
    "created",
    "running",
    "processing",
]



DEBUG_MODE = False


# =====================================================
# V2.6 员工模式 / 管理员模式
#
# 这个工具主要给员工日常使用，页面默认只保留
# 上传 → 开始 → 进度 → 下载 四步。
# 完整功能（五个标签页、诊断 JSON、模块勾选）只在
# Secrets 里配置 ADMIN_MODE = "true" 后显示。
#
# API Key 同理：Secrets 里配了 OPENAI_API_KEY /
# DEEPSEEK_API_KEY 后，员工全程不需要接触密钥。
# =====================================================


def _read_secrets_flag(name: str) -> bool:

    try:

        return str(
            st.secrets.get(name, "") or ""
        ).strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    except Exception:

        return False


ADMIN_MODE = _read_secrets_flag(
    "ADMIN_MODE"
)


def get_saved_provider_key(
    provider_name: str,
) -> str:
    """按服务商读 Secrets 里的密钥（员工模式不再手动填 Key）。"""

    names = (
        ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]
        if provider_name == "DeepSeek"
        else ["OPENAI_API_KEY"]
    )

    try:

        for name in names:

            value = str(
                st.secrets.get(name, "") or ""
            ).strip()

            if value:

                return value

    except Exception:

        pass

    return ""


# =====================================================
# 页面配置
# =====================================================

st.set_page_config(
    page_title="Amazon AI Listing Optimizer",
    layout="wide",
)


# =====================================================
# 视觉样式（V2.5.0 UI）
# =====================================================

CUSTOM_CSS = """
<style>
.stApp { background: #f6f7f9; }

/* ---- 顶部横幅 ---- */
.app-hero {
    background: linear-gradient(120deg, #232F3E 0%, #37475A 78%);
    color: #ffffff;
    padding: 26px 32px 22px 32px;
    border-radius: 16px;
    margin-bottom: 14px;
}
.hero-title { font-size: 30px; font-weight: 800; letter-spacing: 0.5px; }
.hero-sub { color: #d5dbd1; margin-top: 6px; font-size: 14px; }
.version-pill {
    display: inline-block;
    background: #FF9900;
    color: #232F3E;
    font-weight: 700;
    border-radius: 999px;
    padding: 3px 14px;
    font-size: 13px;
    margin-top: 12px;
}

/* ---- 流程提示条 ---- */
.hint-bar {
    background: #FFF7E6;
    border: 1px solid #FFD591;
    border-left: 5px solid #FF9900;
    border-radius: 10px;
    padding: 12px 16px;
    margin: 2px 0 18px 0;
    font-size: 15px;
    color: #3b2f00;
}

/* ---- 新手引导卡片 ---- */
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

/* ---- 指标卡 ---- */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #e6e8eb;
    border-radius: 12px;
    padding: 12px 18px;
    box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06);
}
[data-testid="stMetricLabel"] { font-size: 13px; color: #5b6b7a; }
[data-testid="stMetricValue"] { font-weight: 800; }

/* ---- 侧边栏 ---- */
[data-testid="stSidebar"] { border-right: 1px solid #e6e8eb; }
.side-brand {
    font-size: 17px; font-weight: 800; color: #232F3E;
    padding: 6px 2px 2px 2px;
}
.side-brand span { color: #FF9900; }
.side-step {
    display: flex; align-items: center; gap: 8px;
    font-weight: 700; font-size: 14px; color: #232F3E;
    margin: 18px 0 8px 0;
}
.step-badge {
    background: #FF9900; color: #232F3E;
    border-radius: 50%;
    min-width: 22px; height: 22px;
    display: inline-flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 12px;
}

/* ---- 按钮 / 标签页 / 折叠面板 ---- */
.stButton > button { border-radius: 9px; font-weight: 600; }
.stTabs [data-baseweb="tab"] { font-weight: 600; padding: 8px 16px; }
.stTabs [data-baseweb="tab-highlight"] { background: #FF9900; }
[data-testid="stExpander"] {
    border: 1px solid #e6e8eb;
    border-radius: 12px;
    overflow: hidden;
}
[data-testid="stFileUploaderDropzone"] { border-radius: 10px; }

#MainMenu, footer { visibility: hidden; }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# =====================================================
# 任务恢复
# =====================================================

current_task = st.session_state.get(
    "current_task"
) or load_current_task()


# Validate the persisted task pointer.  Failed/completed tasks remain visible
# until the user explicitly closes them so diagnostics and exports are not lost.
if current_task:

    old_status = load_status(
        current_task
    )


    if not old_status:
        clear_current_task()
        st.session_state.pop("current_task", None)
        st.session_state["task_started"] = False
        current_task = ""


# =====================================================
# Highlight展示
# =====================================================


def display_highlights(
    highlight_result
):


    if not highlight_result:

        return



    if isinstance(
        highlight_result,
        list,
    ):

        for item in highlight_result:

            if isinstance(
                item,
                str,
            ):

                st.write(
                    "• " + item
                )



            elif isinstance(
                item,
                dict,
            ):

                text = (
                    item.get("content")
                    or
                    item.get("text")
                    or
                    ""
                )


                if text:

                    st.write(
                        "• " + text
                    )



# =====================================================
# 内容展示（V2.5.0：含短标题 / 商品亮点 / 首图缩略图）
# =====================================================


def display_generated_content(
    profile
):

    if not isinstance(
        profile,
        dict,
    ):
        return

    title = profile.get(
        "generated_title",
        {}
    )

    short_title_result = (
        profile.get("short_title_result", {})
        if isinstance(profile.get("short_title_result", {}), dict)
        else {}
    )

    short_title = (
        short_title_result.get("short_title")
        or short_title_result.get("title")
        or ""
    )


    if title.get(
        "title"
    ):

        st.markdown("#### 🏷️ AI 标题")

        st.write(
            title["title"]
        )

        if short_title:
            st.markdown(
                f"**短标题：** {str(short_title)}"
            )




    bullet = profile.get(
        "bullet_result",
        {}
    )


    bullets = bullet.get(
        "bullets",
        []
    )


    if bullets:

        st.markdown("#### 📋 AI 五点")


        for item in bullets:

            st.write(
                "• "
                +
                str(item)
            )



    highlight_result = profile.get("highlight_result", {})

    if highlight_result:
        st.markdown("#### ✨ 商品亮点")
        display_highlights(highlight_result)



    description = profile.get(
        "description_result",
        {}
    )


    if description.get(
        "description"
    ):

        st.markdown("#### 📄 AI 详情")


        st.write(
            description["description"]
        )



    image_result = (
        profile.get("image_result", {})
        if isinstance(profile.get("image_result", {}), dict)
        else {}
    )

    if (
        image_result.get("status") == "success"
        and image_result.get("main_image_optimized")
    ):

        st.markdown("#### 🖼️ 优化首图")

        try:
            st.image(
                str(image_result["main_image_optimized"]),
                width=280,
            )
        except Exception:
            st.caption(
                str(image_result["main_image_optimized"])
            )


# =====================================================
# 页面：侧边栏（全部操作，按步骤编号）
# =====================================================

MODULE_TEXT_KEYS = [
    "enable_title",
    "enable_short_title",
    "enable_highlight",
    "enable_bullet",
    "enable_description",
    "enable_seo",
]

MODULE_ALL_KEYS = MODULE_TEXT_KEYS + ["enable_images"]

uploaded = None
envelope = None
api_key = ""
model = "gpt-4.1-mini"
provider = "OpenAI"

# 优化模块开关的默认值。员工模式下侧边栏不显示模块勾选，
# 一直用这组默认值（全文字模块开、图片关）；
# 管理员模式会被侧边栏的勾选覆盖。
enable_title = True
enable_short_title = True
enable_highlight = True
enable_bullet = True
enable_description = True
enable_seo = True
enable_images = False

with st.sidebar:

    st.markdown(
        '<div class="side-brand">🛒 Amazon <span>AI</span> Optimizer</div>',
        unsafe_allow_html=True,
    )

    # --------------------------------------------
    # 第 1 步：上传 Excel
    # --------------------------------------------

    st.markdown(
        '<div class="side-step"><span class="step-badge">1</span> 上传 Excel</div>',
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader(
        "上传 Excel",
        type=["xlsx"],
        key="main_excel_uploader"
    )

    if uploaded is None:
        # 文件被移除后主动释放解析缓存，避免 Session 长期保留整份工作簿对象。
        st.session_state.pop("excel_fingerprint", None)
        st.session_state.pop("excel_envelope", None)
        st.session_state.pop("excel_bytes", None)
        st.session_state.pop("excel_name", None)


    if uploaded is not None:

        excel_bytes = uploaded.getvalue()
        file_fingerprint = hashlib.sha1(excel_bytes).hexdigest()

        st.session_state["excel_name"] = uploaded.name
        st.session_state["excel_bytes"] = excel_bytes

        cached_fingerprint = st.session_state.get("excel_fingerprint")
        cached_envelope = st.session_state.get("excel_envelope")

        try:
            if cached_fingerprint == file_fingerprint and cached_envelope is not None:
                envelope = cached_envelope
            else:
                envelope = read_workbook(
                    uploaded.name,
                    excel_bytes,
                )
                st.session_state["excel_fingerprint"] = file_fingerprint
                st.session_state["excel_envelope"] = envelope

        except Exception as exc:
            st.session_state.pop("excel_fingerprint", None)
            st.session_state.pop("excel_envelope", None)
            st.error(
                f"读取文件失败：{exc}"
            )
            envelope = None

        if envelope is not None:

            st.success(
                f"读取成功："
                f"{len(envelope.records)} 个产品"
            )

            # ----------------------------------------
            # 第 2 步：API 配置
            # ----------------------------------------

            st.markdown(
                '<div class="side-step"><span class="step-badge">2</span> API 配置</div>',
                unsafe_allow_html=True,
            )

            # ----------------------------------------
            # AI 服务商选择：OpenAI 官方 / DeepSeek
            #
            # 选择结果通过环境变量注入给后台任务线程；
            # 任务运行中锁定选择，避免中途换服务商导致请求发错地方。
            # ----------------------------------------

            task_running_now = False

            if current_task:
                _running_status = load_status(current_task)
                task_running_now = bool(
                    _running_status
                    and _running_status.get("status") in TASK_RUNNING_STATUS
                )

            provider = st.radio(
                "AI 服务商",
                ["OpenAI", "DeepSeek"],
                index=0,
                horizontal=True,
                key="ai_provider",
                disabled=task_running_now,
                help="DeepSeek 更便宜、国内直连；OpenAI 为原默认配置。"
                "任务运行中不可切换。",
            )

            if provider == "DeepSeek":
                os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com"
            else:
                os.environ["OPENAI_BASE_URL"] = "https://api.openai.com/v1"

            key_label = (
                "DeepSeek API Key"
                if provider == "DeepSeek"
                else "OpenAI API Key"
            )

            key_hint = (
                "到 platform.deepseek.com 充值并创建 Key"
                if provider == "DeepSeek"
                else "到 platform.openai.com 充值并创建 Key"
            )

            # 按服务商读 Secrets：DeepSeek 用 DEEPSEEK_API_KEY，
            # OpenAI 用 OPENAI_API_KEY（找不到时回退旧配置）。
            saved_api_key = (
                get_saved_provider_key(provider)
                or
                get_openai_api_key()
            )

            if ADMIN_MODE:

                manual_api_key = st.text_input(
                    key_label,
                    type="password",
                )

                api_key = (
                    manual_api_key.strip()
                    or
                    saved_api_key
                )

            else:

                # 员工模式：密钥由管理员配置在 Secrets 里，
                # 页面上不出现输入框，员工全程接触不到密钥。
                api_key = saved_api_key

                if api_key:

                    st.success(
                        "✅ API 已配置"
                    )

                else:

                    # Secrets 里没配密钥时的兜底：仍然显示输入框，
                    # 否则员工模式下会没有地方填 Key，任务无法开始。
                    api_key = st.text_input(
                        key_label,
                        type="password",
                        key="emp_api_key",
                    ).strip()

            if not api_key.strip():
                st.caption(f"💡 {key_hint}")

            # 模型名跟随服务商自动切换（用户自定义过的模型名不会被动）。
            default_model = (
                "deepseek-chat"
                if provider == "DeepSeek"
                else "gpt-4.1-mini"
            )

            if st.session_state.get("model_input") in (
                None,
                "",
                "gpt-4.1-mini",
                "deepseek-chat",
            ):
                st.session_state["model_input"] = default_model

            if ADMIN_MODE:

                model = st.text_input(
                    "模型",
                    key="model_input",
                )

            else:

                # 员工模式不显示模型输入框，跟随服务商默认值。
                model = st.session_state["model_input"]

            # ----------------------------------------
            # 第 3 步：优化模块（仅管理员模式显示）
            #
            # 员工模式用顶部定义的默认值：全文字模块开、
            # 图片关，不显示任何勾选项。
            # ----------------------------------------

            if ADMIN_MODE:

                st.markdown(
                    '<div class="side-step"><span class="step-badge">3</span> 优化模块</div>',
                    unsafe_allow_html=True,
                )

                quick_a, quick_b, quick_c = st.columns(3)

                if quick_a.button("全选", key="quick_select_all", use_container_width=True):
                    for key in MODULE_ALL_KEYS:
                        st.session_state[key] = True
                    st.rerun()

                if quick_b.button("仅文字", key="quick_text_only", use_container_width=True):
                    for key in MODULE_TEXT_KEYS:
                        st.session_state[key] = True
                    st.session_state["enable_images"] = False
                    st.rerun()

                if quick_c.button("清空", key="quick_clear_all", use_container_width=True):
                    for key in MODULE_ALL_KEYS:
                        st.session_state[key] = False
                    st.rerun()

                enable_title = st.checkbox(
                    "优化标题",
                    True,
                    key="enable_title",
                )

                enable_short_title = st.checkbox(
                    "优化短标题",
                    True,
                    key="enable_short_title",
                )


                enable_highlight = st.checkbox(
                    "优化商品亮点",
                    True,
                    key="enable_highlight",
                )


                enable_bullet = st.checkbox(
                    "优化五点描述",
                    True,
                    key="enable_bullet",
                )


                enable_description = st.checkbox(
                    "优化详情描述",
                    True,
                    key="enable_description",
                )


                enable_seo = st.checkbox(
                    "优化SEO关键词",
                    True,
                    key="enable_seo",
                )


                enable_images = st.checkbox(
                    "优化首图（V1.3.2 稳定基线）",
                    False,
                    key="enable_images",
                    help="仅优化第一张主图；其他图片保留。图片失败不会影响文字优化结果。"
                    "需要配置 Cloudinary Secrets（CLOUDINARY_CLOUD_NAME / API_KEY / API_SECRET），"
                    "否则图片上传会失败。",
                )

            else:

                enable_title = True
                enable_short_title = True
                enable_highlight = True
                enable_bullet = True
                enable_description = True
                enable_seo = True

                # 员工模式默认不优化图片（避免未配置 Cloudinary 时
                # 全部图片失败的困扰）。
                enable_images = False

            # ----------------------------------------
            # 第 4 步：开始任务
            # ----------------------------------------

            st.markdown(
                '<div class="side-step"><span class="step-badge">4</span> 开始任务</div>',
                unsafe_allow_html=True,
            )

            current_status = None


            if current_task:


                current_status = load_status(
                    current_task
                )



            button_disabled = False



            if st.session_state.get(
                "task_started",
                False
            ):

                if current_status:

                    if current_status.get("status") in [
                        "processing",
                        "running",
                        "created"
                    ]:

                        button_disabled = True



            if current_status:

                if current_status.get(
                    "status"
                ) in TASK_RUNNING_STATUS:

                    button_disabled = True


            if st.button(
                "🚀 开始 AI 商品理解",
                type="primary",
                disabled=button_disabled,
                use_container_width=True,
            ):


                if not api_key:

                    st.error(
                        "请输入 OpenAI API Key"
                    )

                    st.stop()



                task_id = create_task(

                    total_products=len(
                        envelope.records
                    ),

                    filename=uploaded.name,

                )



                save_current_task(
                    task_id
                )



                options = {

                    "title":
                        enable_title,

                    "short_title":
                        enable_short_title,

                    "highlight":
                        enable_highlight,

                    "bullet":
                        enable_bullet,

                    "description":
                        enable_description,

                    "seo":
                        enable_seo,

                    "optimize_images":
                        enable_images,

                    # Internal safe default for product-level concurrency.
                    "max_workers":
                        4,

                }

                # 首图优化依赖 Cloudinary 上传。缺配置时提前告知，
                # 否则图片会全部静默失败，看起来像"没有起作用"。
                if options.get("optimize_images") and not cloudinary_ready():

                    st.warning(
                        "已开启首图优化，但未配置 Cloudinary Secrets"
                        "（CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / "
                        "CLOUDINARY_API_SECRET）。"
                        "所有图片上传都会失败，文字优化不受影响。"
                        "请先在环境变量或 Streamlit Secrets 中配置。"
                    )


                start_worker(

                    envelope.records,

                    task_id,

                    api_key,

                    model,

                    options,

                )



                st.session_state[
                    "task_started"
                ] = True



                save_current_task(
                    task_id
                )


                st.session_state["current_task"] = task_id

                st.success(
                    f"任务已启动：{task_id}"
                )

                st.info(
                    "AI 正在后台运行，可以刷新页面查看状态。"
                )

                st.rerun()

    else:

        st.caption(
            "上传后，API 配置和优化模块会出现在这里。"
        )


# =====================================================
# 页面主体：横幅 + 流程提示
# =====================================================

st.markdown(
    f"""
    <div class="app-hero">
        <div class="hero-title">🛒 Amazon AI Listing Optimizer</div>
        <div class="hero-sub">AI 生成标题 · 短标题 · 五点 · 详情 · 商品亮点 · 首图优化</div>
        <span class="version-pill">V2.6.0{" · 管理模式" if ADMIN_MODE else " · 员工版"}</span>
    </div>
    """,
    unsafe_allow_html=True,
)


# =====================================================
# 任务数据加载
# =====================================================

profiles = []
base_profiles = []
status = None


if current_task:

    status = load_status(
        current_task
    )

    profiles = load_profiles(
        current_task
    )

    # 重试任务继承：base_profiles.json 保存的是重试前已成功的产品。
    # 当前任务的 profiles 与之合并后，导出/预览即可同时包含新旧结果。
    base_path = get_task_dir(current_task) / "base_profiles.json"
    if base_path.exists():
        base_profiles = load_json(
            base_path,
            default=[],
        )
        if not isinstance(base_profiles, list):
            base_profiles = []

    if base_profiles:
        merged = {}
        for item in base_profiles + profiles:
            if not isinstance(item, dict):
                continue
            identity = item.get("source_identity")
            row_index = (
                identity.get("source_row_index")
                if isinstance(identity, dict)
                else None
            )
            merged[row_index if row_index is not None else id(item)] = item
        profiles = list(merged.values())

    if DEBUG_MODE:
        st.caption(
            f"DEBUG: task={current_task}, success={len(profiles)}, base={len(base_profiles)}"
        )


# =====================================================
# V2.4.4 Failure Observability
#
# A failed product must be visible as a terminal result.
# Do not hide failure diagnostics inside profiles-only JSON.
# =====================================================

failed_items = load_failed_items(
    current_task
) if current_task else []

if DEBUG_MODE:
    st.caption(
        f"DEBUG: success={len(profiles)} / failed={len(failed_items)}"
    )

terminal_success = len(profiles)
terminal_failed = len(failed_items)
terminal_completed = terminal_success + terminal_failed

if current_task:
    expected_total = (
        (
            status.get("total")
            or
            status.get("total_products")
            or
            0
        )
        + len(base_profiles)
    )
else:
    expected_total = 0


# =====================================================
# 流程提示条（告诉用户当前该做什么）
# =====================================================

if uploaded is None and not current_task:

    hint_html = "👈 <b>第 1 步：</b>在左侧上传 Excel 文件开始"

elif uploaded is not None and not api_key.strip():

    hint_html = (
        f"👈 <b>第 2 步：</b>在左侧填写 API Key"
        f"（当前服务商：{provider}）"
    )

elif status and status.get("status") in TASK_RUNNING_STATUS:

    completed_now = status.get("completed", 0)
    total_now = (
        status.get("total")
        or status.get("total_products")
        or 0
    )
    hint_html = (
        f"⏳ AI 处理中：{completed_now} / {total_now}，"
        "可以离开页面，稍后回来点「刷新任务状态」。"
    )

elif status and status.get("status") in {"completed", "cancelled", "failed"} and failed_items:

    hint_html = (
        f"🔁 有 {len(failed_items)} 个失败产品 → "
        + (
            "切到「🚨 失败诊断」标签页点「重新优化」"
            if ADMIN_MODE
            else "点下方「重新优化失败产品」按钮"
        )
    )

elif status and status.get("status") in {"completed", "cancelled", "failed"} and profiles:

    hint_html = (
        "✅ 任务已完成 → "
        + (
            "切到「⬇️ 导出」标签页下载优化结果"
            if ADMIN_MODE
            else "点下方「下载优化结果」按钮"
        )
    )

elif uploaded is None and current_task:

    hint_html = "📎 任务里有结果，但当前没有上传原 Excel — 请重新上传原文件才能导出"

elif uploaded is not None:

    hint_html = "👈 <b>第 3 步：</b>选好左侧优化模块，点「🚀 开始 AI 商品理解」"

else:

    hint_html = "👈 在左侧上传 Excel 文件开始"


st.markdown(
    f'<div class="hint-bar">{hint_html}</div>',
    unsafe_allow_html=True,
)


# =====================================================
# 新手引导（没有任务时显示）
# =====================================================

if not current_task and not profiles:

    g1, g2, g3 = st.columns(3)

    with g1:
        st.markdown(
            """
            <div class="guide-card">
                <div class="guide-num">1</div>
                <div class="guide-title">📤 上传 Excel</div>
                <div class="guide-text">
                在左侧边栏上传采集插件导出的 xlsx 文件，
                系统会自动识别产品数量。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with g2:
        card2_text = (
            "填写 API Key，勾选要优化的模块"
            "（标题 / 短标题 / 五点 / 亮点 / 详情 / 首图），"
            "点「开始 AI 商品理解」。"
            if ADMIN_MODE
            else "选好 AI 服务商（不确定就用默认），"
            "点「开始 AI 商品理解」。"
            "API 已由管理员配置，无需填写密钥。"
        )
        st.markdown(
            f"""
            <div class="guide-card">
                <div class="guide-num">2</div>
                <div class="guide-title">🔑 配置并开始</div>
                <div class="guide-text">{card2_text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with g3:
        card3_text = (
            "任务完成后切到「导出」标签页，"
            "下载优化后的 Excel（含短标题、商品亮点、"
            "优化首图）或诊断 JSON。"
            if ADMIN_MODE
            else "任务完成后点页面中的"
            "「下载优化结果」按钮，"
            "得到优化后的 Excel。"
        )
        st.markdown(
            f"""
            <div class="guide-card">
                <div class="guide-num">3</div>
                <div class="guide-title">⬇️ 导出结果</div>
                <div class="guide-text">{card3_text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# =====================================================
# 工作区：指标 + 标签页
# =====================================================

if current_task and status:

    # --------------------------------------------
    # 指标卡行
    # --------------------------------------------

    status_value = status.get(
        "status",
        ""
    )

    message = status.get(
        "message",
        ""
    )

    completed = (
        status.get(
            "completed",
            0
        )
        + len(base_profiles)
    )

    total = (
        (
            status.get("total")
            or
            status.get("total_products")
            or
            0
        )
        + len(base_profiles)
    )

    # 成功数直接用合并后的 profiles（含重试任务继承的旧成果）。
    success_count = len(profiles)
    failed_count = status.get("failed", 0)

    m1, m2, m3, m4, m5 = st.columns(5)

    with m1:
        st.metric("产品总数", total)

    with m2:
        st.metric("已完成", completed)

    with m3:
        st.metric("成功", success_count)

    with m4:
        st.metric("失败", failed_count)

    with m5:
        st.metric("状态", status_value or "-")

    if total:
        st.progress(
            min(completed / total, 1.0),
            text=f"进度 {completed} / {total}",
        )

    # --------------------------------------------
    # V2.6 重新优化失败产品（员工面板 / 失败诊断共用）
    #
    # 只重跑失败的产品：已成功的不重复调用 AI。
    # 新任务通过 base_profiles.json 继承旧的成功结果，
    # 导出时自动合并，不需要手动拼文件。
    # --------------------------------------------

    def render_retry_failed_button(key_prefix: str):

        task_is_idle = bool(
            status
            and status.get("status") not in TASK_RUNNING_STATUS
        )

        if uploaded is None or envelope is None:

            st.info(
                "💡 想重新优化失败产品：请在左侧重新上传原 Excel 文件后，"
                "这里会出现「重新优化」按钮。"
            )

        elif not task_is_idle:

            st.caption(
                "任务运行中，等任务结束后才能重新优化失败产品。"
            )

        else:

            if st.button(
                f"🔄 重新优化这 {len(failed_items)} 个失败产品",
                type="primary",
                key=f"{key_prefix}_retry_failed_products",
            ):

                if not api_key.strip():

                    st.error(
                        "请先在左侧填写 API Key"
                    )

                else:

                    failed_rows = {
                        item.get("source_row_index")
                        for item in failed_items
                        if isinstance(item, dict)
                        and item.get("source_row_index") is not None
                    }

                    failed_skus = {
                        str(item.get("sku") or "").strip()
                        for item in failed_items
                        if isinstance(item, dict)
                        and str(item.get("sku") or "").strip()
                    }

                    retry_records = [
                        record
                        for record in envelope.records
                        if (
                            getattr(record, "row_number", None) in failed_rows
                        )
                        or (
                            bool(str(getattr(record, "sku") or "").strip())
                            and str(getattr(record, "sku") or "").strip() in failed_skus
                        )
                    ]

                    if not retry_records:

                        st.error(
                            "无法在当前 Excel 里定位这些失败产品"
                            "（行号/SKU 对不上）。请确认上传的是原任务用的同一个文件。"
                        )

                    else:

                        retry_task_id = create_task(
                            total_products=len(retry_records),
                            filename=uploaded.name,
                        )

                        # 继承全部已成功结果（含更早重试继承来的）。
                        save_json(
                            get_task_dir(retry_task_id) / "base_profiles.json",
                            profiles,
                        )

                        retry_options = {

                            "title":
                                enable_title,

                            "short_title":
                                enable_short_title,

                            "highlight":
                                enable_highlight,

                            "bullet":
                                enable_bullet,

                            "description":
                                enable_description,

                            "seo":
                                enable_seo,

                            "optimize_images":
                                enable_images,

                            "max_workers":
                                4,

                        }

                        start_worker(

                            retry_records,

                            retry_task_id,

                            api_key,

                            model,

                            retry_options,

                        )

                        save_current_task(
                            retry_task_id
                        )

                        st.session_state["task_started"] = True
                        st.session_state["current_task"] = retry_task_id

                        st.success(
                            f"重试任务已启动：{retry_task_id}"
                            f"（本次只重跑 {len(retry_records)} 个失败产品）"
                        )

                        st.rerun()

        st.caption(
            "说明：重试只调用 AI 处理失败的产品，已成功的产品不会重跑、不重复消耗 token；"
            "导出的 Excel 会自动合并新旧结果。"
        )


    # --------------------------------------------
    # V2.6 员工模式：极简任务面板
    #
    # 员工只看到：刷新 / 开始新任务 / 下载 / 失败重试。
    # 五个标签页和诊断工具只在管理员模式显示。
    # --------------------------------------------

    if not ADMIN_MODE:

        col_refresh, col_new = st.columns(2)

        with col_refresh:

            if st.button(
                "🔄 刷新进度",
                use_container_width=True,
                key="emp_refresh",
            ):
                st.rerun()

        with col_new:

            if status_value in {"completed", "cancelled", "failed"}:

                if st.button(
                    "🧹 开始新任务",
                    use_container_width=True,
                    key="emp_new_task",
                ):
                    clear_current_task()
                    st.session_state.pop("current_task", None)
                    st.session_state["task_started"] = False
                    st.rerun()

        if status_value in TASK_RUNNING_STATUS:

            st.info(
                "⏳ AI 正在处理，不用一直开着页面；"
                "稍后回来点「刷新进度」即可。"
            )

        elif status_value in {"completed", "cancelled", "failed"} and not profiles:

            st.warning(
                "本次任务没有成功的产品，请联系管理员查看原因。"
            )

        # 下载优化结果（员工唯一的导出入口）
        if profiles and uploaded is not None and envelope is not None:

            try:

                optimized_export = ListingExporter.export_unified(
                    envelope.dataframe,
                    profiles,
                )

                optimized_data = (
                    optimized_export.getvalue()
                    if hasattr(optimized_export, "getvalue")
                    else optimized_export
                )

                safe_stem = re.sub(
                    r"\.xlsx$",
                    "",
                    uploaded.name,
                    flags=re.I,
                )

                st.download_button(
                    "⬇️ 下载优化结果（Excel）",
                    data=optimized_data,
                    file_name=
                    f"{safe_stem}_{VERSION}_AI优化结果.xlsx",
                    mime=
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                    key="emp_download_excel",
                    use_container_width=True,
                )

                st.caption(
                    f"✅ 已优化 {len(profiles)} 个产品，"
                    "点上面按钮下载 Excel。"
                )

            except Exception as exc:

                st.error(
                    f"生成优化文件失败：{exc}"
                )

        elif uploaded is None and profiles:

            st.warning(
                "请重新上传原 Excel 文件后才能下载结果。"
            )

        # 失败产品一键重试
        if failed_items and status_value not in TASK_RUNNING_STATUS:

            render_retry_failed_button("emp")

        # 员工模式到此为止，下面的标签页只有管理员模式渲染。
        st.stop()

    # --------------------------------------------
    # 标签页（仅管理员模式）
    # --------------------------------------------

    tab_status, tab_preview, tab_images, tab_export, tab_diag = st.tabs(
        [
            "📊 任务状态",
            "📝 结果预览",
            "🖼️ 图片优化",
            "⬇️ 导出",
            "🚨 失败诊断",
        ]
    )

    # =============================================
    # 标签页 1：任务状态
    # =============================================

    with tab_status:

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            if st.button("🔄 刷新任务状态", use_container_width=True):
                st.session_state.pop(
                    "current_task",
                    None
                )
                st.rerun()

        with col2:
            if st.button(
                "⏸️ 暂停任务",
                use_container_width=True,
            ):
                save_control(
                    current_task,
                    "pause"
                )
                st.warning(
                    "暂停请求已发送"
                )

        with col3:
            if st.button(
                "▶️ 继续任务",
                use_container_width=True,
            ):
                save_control(
                    current_task,
                    "running"
                )
                st.success(
                    "继续请求已发送"
                )

        with col4:
            if st.button(
                "⛔ 取消任务",
                use_container_width=True,
            ):
                save_control(
                    current_task,
                    "cancel"
                )
                st.error(
                    "取消请求已发送"
                )

        st.info(
            f"""
    状态：{status_value}

    消息：{message}

    进度：{completed} / {total}

    成功：{success_count}    失败：{failed_count}
    """
        )

        api_calls = status.get("api_calls", 0)
        api_attempts = status.get("api_attempts", 0)
        api_retries = status.get("api_retries", 0)
        active_workers = status.get("in_flight", 0)
        max_workers = status.get("max_workers", 0)
        if api_calls or max_workers:
            st.caption(
                f"AI调用：{api_calls}｜实际请求尝试：{api_attempts}｜"
                f"重试：{api_retries}｜并发：{active_workers}/{max_workers}"
            )

        if status_value in {"completed", "cancelled", "failed"}:
            st.session_state["task_started"] = False
            if st.button("关闭当前任务", key="close_current_task"):
                clear_current_task()
                st.session_state.pop("current_task", None)
                st.session_state["task_started"] = False
                st.rerun()

        if expected_total:
            if terminal_completed == expected_total:
                st.caption(
                    f"结果闭环：{terminal_success} 成功 + "
                    f"{terminal_failed} 失败 = {expected_total} 总数"
                )
            else:
                st.error(
                    f"结果未闭环：成功 {terminal_success} + 失败 {terminal_failed} "
                    f"= {terminal_completed}，但任务总数为 {expected_total}。"
                )

        if status.get(
            "traceback"
        ):

            st.error(
                "任务运行错误"
            )

            st.code(
                status.get(
                    "traceback"
                )
            )

    # =============================================
    # 标签页 2：结果预览
    # =============================================

    with tab_preview:

        if not profiles:

            st.info(
                "暂无可预览的产品，请先完成至少 1 个产品的 AI 商品理解。"
            )

        else:

            st.success(
                f"已完成 {len(profiles)} 个产品优化（默认展示前 3 个）"
            )

            for index, profile in enumerate(
                profiles[:3]
            ):

                with st.expander(
                    f"产品 {index + 1}"
                ):

                    display_generated_content(
                        profile
                    )

            # ----------------------------------------
            # Title Strategy 测试
            # 临时验证 AI 标题策略能力
            # ----------------------------------------

            with st.expander(
                "🧪 Title Strategy 测试"
            ):

                # 必须先确认已经有优化结果
                if len(profiles) >= 3:

                    test_index = 2

                else:

                    test_index = len(profiles) - 1


                test_profile = profiles[
                    test_index
                ]

                st.caption(
                    f"当前测试产品：产品 {test_index + 1}"
                )


                if st.button(
                    "生成 Title Strategy",
                    key="title_strategy_test",
                ):

                    try:

                        strategy_api_key = get_openai_api_key()


                        if not strategy_api_key:

                            st.error(
                                "未找到 OpenAI API Key"
                            )

                        else:

                            strategy_result = (
                                TitleStrategyGenerator.generate(
                                    test_profile,
                                    strategy_api_key,
                                )
                            )


                            st.subheader(
                                "Title Strategy 输出"
                            )

                            st.json(
                                strategy_result
                            )


                    except Exception as exc:

                        st.error(
                            f"Title Strategy 测试失败：{exc}"
                        )

    # =============================================
    # 标签页 3：图片优化
    # =============================================

    with tab_images:

        image_results = [
            (index, profile.get("image_result"))
            for index, profile in enumerate(
                profiles,
                1,
            )
            if isinstance(
                profile,
                dict,
            )
            and isinstance(
                profile.get("image_result"),
                dict,
            )
        ]

        if not image_results:

            st.info(
                "本次任务没有图片处理记录：可能未开启「优化首图」，"
                "或任务尚未处理到图片。开启前需配置 Cloudinary Secrets。"
            )

        else:

            failed_images = [
                (index, result)
                for index, result in image_results
                if result.get("status") != "success"
            ]

            st.markdown(
                f"**图片优化：成功 {len(image_results) - len(failed_images)}"
                f" / 失败 {len(failed_images)}**"
            )

            for index, result in image_results:

                img_status = result.get(
                    "status",
                    "",
                )

                sku = result.get(
                    "sku",
                    "",
                )

                if img_status == "success":

                    st.markdown(
                        f"✅ 产品 {index}｜{sku or '-'}｜"
                        f"{result.get('transform', '')}"
                    )

                    try:
                        st.image(
                            str(result.get("main_image_optimized", "")),
                            width=260,
                        )
                    except Exception:
                        st.caption(
                            result.get(
                                "main_image_optimized",
                                "",
                            )
                        )

                else:

                    st.markdown(
                        f"❌ 产品 {index}｜{sku or '-'}｜"
                        f"图片优化未生效"
                    )

                    st.caption(
                        "原因："
                        + str(
                            result.get("error")
                            or img_status
                            or "-"
                        )
                    )

    # =============================================
    # 标签页 4：导出
    # =============================================

    with tab_export:

        # ----------------------------------------
        # AI 优化结果 Excel
        # ----------------------------------------

        if profiles and uploaded is not None and envelope is not None:

            try:

                optimized_export = ListingExporter.export_unified(
                    envelope.dataframe,
                    profiles,
                )

                if hasattr(
                    optimized_export,
                    "getvalue"
                ):

                    optimized_data = (
                        optimized_export.getvalue()
                    )

                else:

                    optimized_data = optimized_export

                safe_stem = re.sub(
                    r"\.xlsx$",
                    "",
                    uploaded.name,
                    flags=re.I,
                )

                st.download_button(
                    "⬇️ 导出 AI 优化结果（Excel）",
                    data=optimized_data,
                    file_name=
                    f"{safe_stem}_{VERSION}_AI优化结果.xlsx",
                    mime=
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                )

                st.caption(
                    "包含：AI 标题 / 短标题 / 五点 / 详情 / 商品亮点 / 优化首图链接。"
                    "模板缺少的列会自动补在表格最右侧。"
                )

            except Exception as exc:

                st.error(
                    f"生成优化文件失败：{exc}"
                )

        elif uploaded is None:

            st.warning(
                "需要重新上传原始 Excel 文件才能导出（导出基于原表格写入）。"
            )

        else:

            st.info(
                "暂无优化结果可导出，请先运行任务。"
            )

        st.divider()

        # ----------------------------------------
        # JSON 导出
        # ----------------------------------------

        if profiles:

            st.markdown("**📄 JSON 导出**")

            # Profiles-only JSON is kept for backward compatibility.
            st.download_button(

                "下载 Product Profile JSON",

                data=json.dumps(
                    profiles,
                    ensure_ascii=False,
                    indent=2,
                ).encode(
                    "utf-8"
                ),

                file_name=
                "product_profiles_v2.4.4.json",

                mime=
                "application/json",
            )

            # Complete task diagnostic export:
            # success + failed + task status.  This is the file to use when
            # investigating any missing/failed product because profiles-only JSON
            # intentionally contains successful profiles only.
            complete_task_report = {
                "task_id": current_task,
                "status": status,
                "summary": {
                    "success": len(profiles),
                    "failed": len(failed_items),
                    "completed": len(profiles) + len(failed_items),
                    "expected_total": expected_total,
                    "closed": (
                        len(profiles) + len(failed_items) == expected_total
                        if expected_total
                        else None
                    ),
                },
                "profiles": profiles,
                "failed_items": failed_items,
            }

            st.download_button(
                "下载完整任务诊断 JSON",
                data=json.dumps(
                    complete_task_report,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                file_name="product_task_diagnostic_v2.4.4.json",
                mime="application/json",
                key="download_complete_task_diagnostic_json",
            )

        # ----------------------------------------
        # 原文件完整性测试
        # ----------------------------------------

        if uploaded is not None and envelope is not None:

            st.divider()

            st.markdown("**🧾 原文件完整性导出**")

            try:

                unchanged_export = export_unchanged(
                    envelope
                )

                integrity = integrity_report(
                    envelope,
                    unchanged_export,
                )

                if integrity["byte_identical"]:

                    st.success(
                        "验证通过：原文件完整性保持一致"
                    )

                    safe_stem = re.sub(
                        r"\.xlsx$",
                        "",
                        uploaded.name,
                        flags=re.I,
                    )

                    st.download_button(
                        "导出原文件完整性测试文件",
                        data=unchanged_export,
                        file_name=
                        f"{safe_stem}_{VERSION}_原样导出.xlsx",
                        mime=
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )

                else:

                    st.error(
                        "原文件完整性验证失败"
                    )

            except Exception as exc:

                st.error(
                    f"完整性测试失败：{exc}"
                )

    # =============================================
    # 标签页 5：失败诊断
    # =============================================

    with tab_diag:

        if not failed_items:

            st.success(
                "🎉 本次任务没有失败产品。"
            )

        else:

            st.markdown(
                f"**失败产品（{len(failed_items)}）— 真实错误如下**"
            )

            # ----------------------------------------
            # 重新优化失败产品（V2.6 抽成公共函数，
            # 与员工模式主页面的按钮逻辑完全一致）
            # ----------------------------------------

            render_retry_failed_button("diag")

            for failed_index, item in enumerate(failed_items, 1):
                if not isinstance(item, dict):
                    st.error(f"失败记录 {failed_index}: {item}")
                    continue

                row_index = item.get("source_row_index", "")
                sku = item.get("sku", "")
                title = item.get("title", "")
                error_type = item.get("error_type", "")
                error = item.get("error", "")
                attempt = item.get("attempt", item.get("attempts", ""))
                max_attempts = item.get("max_attempts", "")

                st.markdown(
                    f"**失败 {failed_index}｜Excel 行：{row_index or '-'}｜"
                    f"SKU：{sku or '-'}**"
                )
                if title:
                    st.caption(title)
                st.code(
                    "\n".join(
                        [
                            f"error_type: {error_type or '-'}",
                            f"error: {error or '-'}",
                            (
                                f"attempt: {attempt}/{max_attempts}"
                                if max_attempts
                                else f"attempts: {attempt or '-'}"
                            ),
                        ]
                    ),
                    language="text",
                )

            failure_report = {
                "task_id": current_task,
                "status": status,
                "summary": {
                    "success": terminal_success,
                    "failed": terminal_failed,
                    "completed": terminal_completed,
                    "expected_total": expected_total,
                    "closed": (
                        terminal_completed == expected_total
                        if expected_total
                        else None
                    ),
                },
                "failed_items": failed_items,
            }

            st.download_button(
                "下载失败诊断 JSON",
                data=json.dumps(
                    failure_report,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                file_name="failed_items_diagnostic.json",
                mime="application/json",
                key="download_failed_diagnostic_json",
            )

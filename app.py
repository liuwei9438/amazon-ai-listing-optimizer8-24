from __future__ import annotations


import os

import streamlit as st


from services.task_manager import (
    load_status,
)


from services.current_task import (
    clear_current_task,
    load_current_task,
)


from services.user_auth import (
    current_dept,
    get_dept_key_info,
    get_opt_config,
    is_admin_user,
    render_sidebar_badge,
    require_login,
    sync_session_cookie,
)


from image.image_storage import cloudinary_ready


VERSION = "V2.12.0"

TASK_RUNNING_STATUS = [
    "created",
    "running",
    "processing",
]


# =====================================================
# V2.12.0 一体工作台：优化 + 产品库合并成唯一页面「我的产品」
#
# 旧结构（侧栏上传 Excel → 四步流程 + 独立产品库页）整体下线：
#   - 导入（插件粘贴 / Excel）在工作台页面里，导入的产品带完整
#     资料存进产品库；
#   - 勾选产品直接 🚀 AI 优化（复用原任务引擎），结果写回产品；
#   - 导出从产品库勾选导出，不再依赖「当前上传的 Excel」。
# 侧栏只保留 AI 配置（服务商 / Key / 模型）和优化模块（管理员）。
# =====================================================


DEBUG_MODE = False


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
) or is_admin_user()


# AI 服务商清单和各家在 Secrets 里的密钥名。
# V2.7.11：智谱GLM 已移除——免费额度按分钟限流太严（429 要求控制
# 请求频率），串行+节流+长等待重试后单个产品仍要磨很久，先只留两家。
AI_PROVIDERS = ["OpenAI", "DeepSeek"]

PROVIDER_SECRET_NAMES = {
    "OpenAI": "OPENAI_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
}


def get_saved_provider_key(
    provider_name: str,
) -> str:
    """严格按服务商读 Secrets 里的密钥。

    不做跨服务商回退：OpenAI 的 Key 发给 DeepSeek 接口只会得到
    401 密钥错误，导致整个任务全部失败。配错了宁可显示输入框。
    """

    name = PROVIDER_SECRET_NAMES.get(
        provider_name, "OPENAI_API_KEY"
    )

    try:

        return str(
            st.secrets.get(name, "") or ""
        ).strip()

    except Exception:

        return ""


def get_other_provider_key(
    provider_name: str,
) -> str:
    """别家服务商的密钥——只用来判断"是不是配错了家"，不拿来用。"""

    for other in AI_PROVIDERS:
        if other != provider_name and get_saved_provider_key(other):
            return get_saved_provider_key(other)

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

/* ---- 按钮 / 折叠面板 ---- */
.stButton > button { border-radius: 9px; font-weight: 600; }
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
# 账号门
# =====================================================

# V2.11.1：登录页固定版本角标——未登录页面原本看不到版本号，
# 云端部署有没有更新没法一眼确认；角标只在未登录时显示。
# V2.11.2：挪到左下角——右下角会被 Streamlit 自己的「Manage app」
# 按钮盖住，总账号永远看不到角标，白白误判成「没部署」。
if not st.session_state.get("auth_user"):
    st.markdown(
        f'<div style="position:fixed;left:14px;bottom:12px;z-index:9999;'
        f'background:#232F3E;color:#FF9900;font-size:12px;font-weight:700;'
        f'border-radius:999px;padding:3px 12px;">{VERSION}</div>',
        unsafe_allow_html=True,
    )

require_login()

# V2.7.3：登录态写 Cookie（新标签页/采集插件打开的页面自动恢复登录）
sync_session_cookie()


# =====================================================
# 任务指针校验（任务目录被清时清掉指针，别卡住页面）
# =====================================================

current_task = st.session_state.get(
    "current_task"
) or load_current_task()

if current_task:

    if not load_status(current_task):
        clear_current_task()
        st.session_state.pop("current_task", None)
        st.session_state["task_started"] = False
        current_task = ""


# =====================================================
# 侧边栏：AI 配置 + 优化模块（管理员）
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

    render_sidebar_badge()

    # V2.7.2 修复“点了没反应”：页面与服务器之间的长连接
    # 闲置久了会悄悄断开。这里加一个隐形心跳：每 30 秒自动轻跳
    # 一次保持连接活跃，断了也会立刻触发自动重连。
    _wz_fragment = getattr(st, "fragment", None) or getattr(
        st, "experimental_fragment", None
    )
    if _wz_fragment is not None:
        try:
            @_wz_fragment(run_every=30)
            def _wz_heartbeat():
                st.empty()
            _wz_heartbeat()
        except Exception:
            pass

    # --------------------------------------------
    # 第 1 步：AI 配置（V2.12.0 起常驻——不再依赖上传 Excel）
    # --------------------------------------------

    st.markdown(
        '<div class="side-step"><span class="step-badge">1</span> AI 配置</div>',
        unsafe_allow_html=True,
    )

    # 总后台设置（Worker /opt_config，带登录令牌）——
    # desc_to_ai：详情描述是否参与 AI 优化（全局开关）；
    # modules：本小组可优化的模块（/manage 小组权限，worker v10）。
    try:
        opt_cfg = get_opt_config() or {}
    except Exception:
        opt_cfg = {}

    st.session_state["desc_to_ai"] = opt_cfg.get(
        "desc_to_ai", True
    )
    st.session_state["perm_modules"] = opt_cfg.get(
        "modules", {}
    )

    # AI 服务商选择：OpenAI 官方 / DeepSeek。
    # 选择结果通过环境变量注入给后台任务线程；
    # 任务运行中锁定选择，避免中途换服务商导致请求发错地方。
    task_running_now = False

    if current_task:
        _running_status = load_status(current_task)
        task_running_now = bool(
            _running_status
            and (
                _running_status.get("status") in TASK_RUNNING_STATUS
                or _running_status.get("status") == "paused"
            )
        )

    # 小组 Key 优先：组长在看板上选了 Key 属于哪家，
    # 组员侧自动锁定同一家服务商（防 Key 发错家全部 401）。
    emp_dept = "" if ADMIN_MODE else current_dept()
    dept_key, dept_provider = (
        get_dept_key_info(emp_dept)
        if emp_dept
        else ("", "")
    )

    if dept_key:
        provider = (
            "DeepSeek"
            if dept_provider == "deepseek"
            else "OpenAI"
        )
        st.caption(
            f"🔒 AI 服务商：{provider}"
            "（本组统一，由组长设置）"
        )
    else:
        provider = st.radio(
            "AI 服务商",
            AI_PROVIDERS,
            index=0,
            horizontal=True,
            key="ai_provider",
            disabled=task_running_now,
            help="DeepSeek 更便宜、国内直连；"
            "OpenAI 为原默认配置。任务运行中不可切换。",
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

    # 严格按服务商读对应的 Secrets 密钥（见函数注释：
    # 跨服务商回退会让错误家的 Key 发给接口，全部 401 失败）。
    saved_api_key = get_saved_provider_key(provider)

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

        # 员工模式密钥来源：
        # 本组 Key（组长看板自助填写）优先；没设组 Key 时
        # 用 Secrets 全局 Key，再没有就显示输入框手动填。
        if dept_key:
            api_key = dept_key
            api_source = "dept"
        else:
            api_key = saved_api_key
            api_source = "global"

        if api_source == "dept":

            st.success(
                f"✅ API 已配置（{emp_dept} 小组 Key）"
            )

        elif api_key:

            st.success(
                "✅ API 已配置"
            )

        else:

            # Secrets 里没配当前服务商的密钥：显示输入框兜底。
            api_key = st.text_input(
                key_label,
                type="password",
                key="emp_api_key",
            ).strip()

            # 只配了另一家服务商的 Key 时明确提示，
            # 避免以为已配置、实际全任务 401 失败。
            if get_other_provider_key(provider):

                needed = PROVIDER_SECRET_NAMES.get(
                    provider, "OPENAI_API_KEY"
                )

                st.caption(
                    f"⚠️ 当前选择 {provider}，但 Secrets 里"
                    f"只配置了另一家的密钥。用 {provider} 需要"
                    f"在 Secrets 配置 {needed}；"
                    "也可以直接在上方输入框里粘贴"
                    f"{provider} 的 Key。"
                )

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

    # --------------------------------------------
    # 第 2 步：优化模块（仅管理员模式显示）
    #
    # 员工模式用默认值：全文字模块开、图片关。
    # --------------------------------------------

    if ADMIN_MODE:

        st.markdown(
            '<div class="side-step"><span class="step-badge">2</span> 优化模块</div>',
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

    # v2.7.6：本小组的优化模块权限（总后台 /manage 设置，
    # worker v10）。只锁员工模式；没设置过的小组=全开。
    if not ADMIN_MODE:
        perm = st.session_state.get(
            "perm_modules", {}
        )
        if perm and not any(perm.values()):
            # 防御：六个模块全关时按全开处理，
            # 别让员工跑了任务却什么都没生成。
            perm = {}
        if perm and any(
            v is False for v in perm.values()
        ):
            enable_title = perm.get("title", True)
            enable_short_title = perm.get(
                "short_title", True
            )
            enable_highlight = perm.get(
                "highlight", True
            )
            enable_bullet = perm.get("bullet", True)
            enable_description = perm.get(
                "description", True
            )
            enable_seo = perm.get("seo", True)
            _names = []
            if enable_title:
                _names.append("标题")
            if enable_short_title:
                _names.append("短标题")
            if enable_highlight:
                _names.append("商品亮点")
            if enable_bullet:
                _names.append("五点")
            if enable_description:
                _names.append("详情")
            if enable_seo:
                _names.append("SEO关键词")
            st.caption(
                "🔒 本组可优化："
                + "、".join(_names)
                + "（总后台设置）"
            )

    # v2.7.4：总后台关了「详情描述参与AI优化」时，
    # 管理员勾选/员工默认值都强制不再让 AI 重写简介。
    if not st.session_state.get("desc_to_ai", True):
        enable_description = False
        st.caption(
            "🔒 详情描述不参与 AI 优化（总后台设置）："
            "简介 = 本地过滤后的原文，不消耗 token。"
        )

    # 首图优化依赖 Cloudinary 上传。缺配置时提前告知。
    if enable_images and not cloudinary_ready():
        st.caption(
            "⚠️ 已开启首图优化，但未配置 Cloudinary Secrets"
            "（CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / "
            "CLOUDINARY_API_SECRET）。图片上传都会失败，"
            "文字优化不受影响。"
        )

# =====================================================
# 页面主体：横幅 + 一体工作台
# =====================================================

st.markdown(
    f"""
    <div class="app-hero">
        <div class="hero-title">📦 我的产品 · AI Listing 工作台</div>
        <div class="hero-sub">导入产品库 · 勾选 AI 优化（标题 / 短标题 / 五点 / 详情 / 亮点 / SEO）· 结果挂产品 · 一键导出</div>
        <span class="version-pill">{VERSION}{" · 管理模式" if ADMIN_MODE else " · 基础版"}</span>
    </div>
    """,
    unsafe_allow_html=True,
)


# =====================================================
# V2.12.0 一体工作台（唯一页面）
# =====================================================

options = {
    "title": enable_title,
    "short_title": enable_short_title,
    "highlight": enable_highlight,
    "bullet": enable_bullet,
    "description": enable_description,
    "seo": enable_seo,
    "optimize_images": enable_images,
    # Internal safe default for product-level concurrency.
    "max_workers": 4,
}

try:
    from services.workbench import render_workbench

    render_workbench(
        api_key=api_key,
        model=model,
        options=options,
    )

except Exception as _wb_err:
    st.error(f"工作台加载失败：{_wb_err}")

    if ADMIN_MODE:
        import traceback

        st.code(traceback.format_exc())

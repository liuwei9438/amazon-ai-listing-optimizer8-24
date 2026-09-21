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


# V2.13.3 容错导入：09-21 云端曾因这条 import 整页崩溃（云端的
# services/user_auth.py 没同步到新版、缺 get_worker_ai_key /
# set_worker_ai_key 时，ImportError 直接白屏打不开）。改为：
# 老函数正常导入；两个新函数单独容错（缺了退化为「未配置」）；
# 整链异常时进安全模式并把真实错误亮在页面上，绝不再白屏。
try:
    from services.user_auth import (
        current_dept,
        get_dept_key_info,
        get_opt_config,
        is_admin_user,
        render_sidebar_badge,
        require_login,
        sync_session_cookie,
    )
    _AUTH_IMPORT_ERROR = ""
except Exception as _auth_err:

    _AUTH_IMPORT_ERROR = f"{type(_auth_err).__name__}: {_auth_err}"

    def current_dept():
        return ""

    def get_dept_key_info(dept):
        return "", ""

    def get_opt_config():
        return {}

    def is_admin_user():
        return False

    def render_sidebar_badge():
        return None

    def require_login():
        st.error(
            "登录模块加载失败（安全模式）。请把下面这段错误"
            "截图发给技术，然后到 share.streamlit.io 重启应用："
        )
        st.code(_AUTH_IMPORT_ERROR)
        st.stop()

    def sync_session_cookie():
        return None

try:
    from services.user_auth import get_worker_ai_key, set_worker_ai_key
except Exception:
    # 云端 user_auth.py 还是旧版（缺这两个函数）时不崩溃：
    # AI 设置面板照常打开，保存/读取提示未同步；重启应用
    # 重新拉取代码后自动恢复。

    def get_worker_ai_key():
        return "", ""

    def set_worker_ai_key(provider, key):
        return False, "服务器模块未同步（到 share.streamlit.io 重启应用即可恢复）"


VERSION = "V2.13.5"

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

/* ---- 页头（V2.13.0 一行极简）---- */
.page-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 2px 2px 12px 2px;
}
.page-title { font-size: 22px; font-weight: 800; color: #232F3E; }
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
    # AI 配置（V2.12.1 侧栏极简：员工不显示任何
    # Key / 模型 / 服务商控件，只留一行状态；
    # 管理员收进折叠面板，外面只留一行状态）
    # --------------------------------------------

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

    if ADMIN_MODE:

        # ---- 管理员：所有配置收进默认收起的设置面板 ----
        # V2.13.2：Key 保存进服务器（worker v13.6 cfg:ai）——填
        # 一次全站生效，管理员/组长/员工都不再手填；小组自己
        # 填了 Key 的组仍按组优先。
        try:
            wk_key, wk_provider = get_worker_ai_key()
        except Exception:
            wk_key, wk_provider = "", ""

        with st.expander("⚙️ AI 设置", expanded=False):
            provider = st.radio(
                "AI 服务商",
                AI_PROVIDERS,
                index=(1 if wk_provider == "deepseek" else 0),
                horizontal=True,
                key="ai_provider",
                disabled=task_running_now,
                help="DeepSeek 更便宜、国内直连；"
                "OpenAI 为原默认配置。任务运行中不可切换。",
            )

            if provider == "DeepSeek":
                os.environ["OPENAI_BASE_URL"] = (
                    "https://api.deepseek.com"
                )
            else:
                os.environ["OPENAI_BASE_URL"] = (
                    "https://api.openai.com/v1"
                )

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

            manual_api_key = st.text_input(
                key_label,
                type="password",
                help="留空=直接用已保存的 Key；填了仅本次登录"
                "有效，点「保存到服务器」后全站永久生效。",
            )

            # Key 来源优先级：本次手填 > 服务器全局 Key（服务商
            # 必须一致，错家的 Key 发过去只会 401）> Secrets。
            api_key = manual_api_key.strip()
            if not api_key and wk_key and (
                (provider == "DeepSeek")
                == (wk_provider == "deepseek")
            ):
                api_key = wk_key
            if not api_key:
                # 严格按服务商读对应的 Secrets 密钥（不做跨服务商
                # 回退：错家的 Key 发给接口只会 401 全军覆没）。
                api_key = get_saved_provider_key(provider)

            if not api_key.strip():
                st.caption(f"💡 {key_hint}")

            if wk_key:
                st.caption(
                    "🔒 服务器已保存："
                    + (
                        "DeepSeek"
                        if wk_provider == "deepseek"
                        else "OpenAI"
                    )
                    + f" Key（{len(wk_key)} 位）——全站登录自动"
                    "使用；小组填了 Key 的组按组优先。"
                )
            else:
                st.caption(
                    "🔒 服务器尚未保存 Key：保存后全站登录自动带，"
                    "谁都不用再手填。"
                )

            col_save, col_clear = st.columns(2)
            with col_save:
                if st.button(
                    "💾 保存到服务器（全站生效）",
                    use_container_width=True,
                    disabled=(
                        task_running_now
                        or not manual_api_key.strip()
                    ),
                ):
                    ok, err = set_worker_ai_key(
                        "deepseek"
                        if provider == "DeepSeek"
                        else "openai",
                        manual_api_key.strip(),
                    )
                    if ok:
                        st.session_state[
                            "_worker_ai_key_cache"
                        ] = None
                        st.success(
                            "已保存 ✅ 全站生效（下次刷新页面后"
                            "状态更新）"
                        )
                    else:
                        st.error(f"保存失败：{err}")
            with col_clear:
                if st.button(
                    "🗑️ 清空服务器 Key",
                    use_container_width=True,
                    disabled=task_running_now or not wk_key,
                ):
                    ok, err = set_worker_ai_key("", "")
                    if ok:
                        st.session_state[
                            "_worker_ai_key_cache"
                        ] = None
                        st.success("已清空 ✅")
                    else:
                        st.error(f"清空失败：{err}")

            # 模型名跟随服务商自动切换（自定义过的不会被动）。
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

            model = st.text_input(
                "模型",
                key="model_input",
            )

    else:

        # ---- 员工：侧栏不出现任何配置控件 ----
        # 密钥来源：本组 Key（组长在小组看板填）优先；
        # 没有就用 Secrets 全局 Key；都没有只提示找组长。
        emp_dept = current_dept()

        try:
            dept_key, dept_provider = (
                get_dept_key_info(emp_dept)
                if emp_dept
                else ("", "")
            )
        except Exception:
            dept_key, dept_provider = "", ""

        if dept_key:
            provider = (
                "DeepSeek"
                if dept_provider == "deepseek"
                else "OpenAI"
            )
            api_key = dept_key
        else:
            # V2.13.2：管理员保存在服务器的全局 Key 排在
            # Secrets 前面（那是「⚙️ AI 设置」真保存的那把）。
            try:
                wk_key, wk_provider = get_worker_ai_key()
            except Exception:
                wk_key, wk_provider = "", ""

            if wk_key:
                provider = (
                    "DeepSeek"
                    if wk_provider == "deepseek"
                    else "OpenAI"
                )
                api_key = wk_key
            else:
                saved_open = get_saved_provider_key("OpenAI")
                saved_deep = get_saved_provider_key("DeepSeek")

                if saved_deep and not saved_open:
                    provider, api_key = (
                        "DeepSeek",
                        saved_deep,
                    )
                else:
                    provider, api_key = "OpenAI", saved_open

        if provider == "DeepSeek":
            os.environ["OPENAI_BASE_URL"] = (
                "https://api.deepseek.com"
            )
        else:
            os.environ["OPENAI_BASE_URL"] = (
                "https://api.openai.com/v1"
            )

        # 员工不显示模型输入框，跟随服务商默认值。
        model = (
            "deepseek-chat"
            if provider == "DeepSeek"
            else "gpt-4.1-mini"
        )
        st.session_state["model_input"] = model

        # 没配置时不在侧栏显示任何提示——点「🚀 AI 优化」时的
        # 报错会指路找组长（_start_optimize），侧栏保持零内容。

    # --------------------------------------------
    # 优化模块（V2.13.0：收进管理员折叠面板）
    #
    # 员工模式用默认值：全文字模块开、图片关。
    # --------------------------------------------

    if ADMIN_MODE:

        with st.expander("🧩 优化模块", expanded=False):

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

        # 员工模式默认：全文字模块开、图片关（避免未配置
        # Cloudinary 时全部图片失败的困扰）。
        enable_title = True
        enable_short_title = True
        enable_highlight = True
        enable_bullet = True
        enable_description = True
        enable_seo = True
        enable_images = False

        # 本小组的优化模块权限（总后台 /manage 设置）静默生效；
        # 没设置过的小组=全开。六个全关时按全开处理（防御：
        # 别让员工跑了任务却什么都没生成）。
        perm = st.session_state.get("perm_modules", {})

        if perm and not any(perm.values()):
            perm = {}

        if perm and any(v is False for v in perm.values()):
            enable_title = perm.get("title", True)
            enable_short_title = perm.get("short_title", True)
            enable_highlight = perm.get("highlight", True)
            enable_bullet = perm.get("bullet", True)
            enable_description = perm.get("description", True)
            enable_seo = perm.get("seo", True)

    # 总后台关了「详情描述参与AI优化」时，管理员/员工都不再让
    # AI 重写简介（简介=本地过滤后的原文，省 token；V2.13.0 起
    # 静默生效，不在侧栏显示提示）。
    if not st.session_state.get("desc_to_ai", True):
        enable_description = False

# =====================================================
# 页面主体：横幅 + 一体工作台
# =====================================================

st.markdown(
    f"""
    <div class="page-head">
        <span class="page-title">📦 我的产品</span>
        <span class="version-pill">{VERSION}</span>
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

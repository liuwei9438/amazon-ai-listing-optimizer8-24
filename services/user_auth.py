from __future__ import annotations


import hashlib
import json
import secrets as _pysecrets
import time
from datetime import datetime
from pathlib import Path

import streamlit as st


# =====================================================
# V2.6.2 账号门（防外传白嫖）
#
# Secrets 不配置 app_users 时完全不启用，行为与旧版一致。
# 配置后：打开页面必须登录，未登录只显示登录页。
#
# Secrets 写法（密码哈希用 tools/make_password_hash.py
# 本地生成，明文密码永远不进聊天、不进代码）：
#
#   [app_users]
#   zhangsan = "pbkdf2$260000$<盐>$<哈希>"
#   lisi = "pbkdf2$260000$<盐>$<哈希>"
#
#   app_admins = "zhangsan"          # 管理员账号，逗号分隔
#
# 管理员登录后自动显示完整功能（等同 ADMIN_MODE=true）。
#
# 按账号记账：登录、启动任务会追加写入 tasks/usage_log.jsonl，
# 谁在什么时间跑了多少行一目了然。
# =====================================================


USAGE_LOG_PATH = Path("tasks") / "usage_log.jsonl"

HASH_SCHEME = "pbkdf2"
HASH_ITERATIONS = 260_000

# 连续失败 5 次后锁定 60 秒（同一浏览器会话内）。
MAX_FAILS_BEFORE_LOCK = 5
LOCK_SECONDS = 60


def hash_password_for_storage(password: str) -> str:
    """生成可写进 Secrets 的密码哈希串（随机盐）。"""
    salt_hex = _pysecrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        HASH_ITERATIONS,
    ).hex()
    return f"{HASH_SCHEME}${HASH_ITERATIONS}${salt_hex}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码是否匹配 Secrets 里的哈希串。"""
    try:
        scheme, iterations, salt_hex, digest_hex = str(
            stored
        ).strip().split("$")
        if scheme != HASH_SCHEME:
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        ).hex()
        return _pysecrets.compare_digest(calc, digest_hex)
    except Exception:
        return False


def _load_users() -> dict[str, str]:
    """读 Secrets 里的账号表；没配置返回空（登录门不启用）。"""
    try:
        raw = st.secrets.get("app_users")
    except Exception:
        return {}
    if not raw:
        return {}
    users: dict[str, str] = {}
    try:
        for name, value in raw.items():
            users[str(name).strip().lower()] = str(value).strip()
    except Exception:
        return {}
    return users


def _load_admins() -> set[str]:
    """管理员账号集合；支持逗号分隔字符串或 TOML 表。"""
    try:
        raw = st.secrets.get("app_admins", "")
    except Exception:
        return set()
    if not raw:
        return set()
    if isinstance(raw, dict):
        names = list(raw.keys())
    else:
        names = str(raw).split(",")
    return {
        str(n).strip().lower()
        for n in names
        if str(n).strip()
    }


def auth_enabled() -> bool:
    return bool(_load_users())


def current_user() -> str:
    return str(
        st.session_state.get("auth_user", "") or ""
    )


def is_admin_user() -> bool:
    user = current_user()
    return bool(user) and user.lower() in _load_admins()


def log_user_event(event: str, **fields) -> None:
    """按账号记账（JSONL 追加）。记账失败绝不阻断业务。"""
    user = current_user()
    if not user:
        return
    try:
        USAGE_LOG_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        entry = {
            "time": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "user": user,
            "event": event,
        }
        entry.update(fields)
        with open(
            USAGE_LOG_PATH,
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write(
                json.dumps(
                    entry,
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _locked_out() -> bool:
    fails = int(
        st.session_state.get("auth_fails", 0) or 0
    )
    if fails < MAX_FAILS_BEFORE_LOCK:
        return False
    locked_at = float(
        st.session_state.get("auth_locked_at", 0) or 0
    )
    return (time.time() - locked_at) < LOCK_SECONDS


def _render_login_page(users: dict[str, str]) -> None:
    st.markdown(
        """
        <div class="app-hero" style="text-align:center;">
            <div class="hero-title">Amazon AI Listing Optimizer</div>
            <div class="hero-sub">请登录后使用（内部工具，禁止外传）</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if _locked_out():
        st.error(
            "错误次数过多，请约 1 分钟后重试。"
        )
        return

    with st.form("login_form"):
        username = st.text_input("账号")
        password = st.text_input(
            "密码",
            type="password",
        )
        submitted = st.form_submit_button(
            "登录",
            use_container_width=True,
        )

    if not submitted:
        return

    key = username.strip().lower()
    stored = users.get(key, "")

    if key and stored and verify_password(password, stored):
        st.session_state["auth_user"] = key
        st.session_state["auth_fails"] = 0
        st.session_state.pop("auth_locked_at", None)
        log_user_event("login")
        st.rerun()
        return

    st.session_state["auth_fails"] = (
        int(st.session_state.get("auth_fails", 0) or 0)
        + 1
    )
    if st.session_state["auth_fails"] >= (
        MAX_FAILS_BEFORE_LOCK
    ):
        st.session_state["auth_locked_at"] = time.time()
    time.sleep(0.8)
    st.error("账号或密码错误")


def require_login() -> None:
    """登录门：未登录则只显示登录页并停掉后续全部界面。

    Secrets 没配 app_users 时直接放行（旧版行为）。
    """
    users = _load_users()
    if not users:
        return

    if current_user() in users:
        return

    _render_login_page(users)
    st.stop()


def render_sidebar_badge() -> None:
    """侧边栏显示当前账号 + 切换账号按钮。"""
    if not auth_enabled():
        return

    user = current_user()
    if not user:
        return

    admin_mark = " · 管理员" if is_admin_user() else ""
    st.markdown(
        f"""
        <div style="
            background:#ffffff;
            border:1px solid #e6e8eb;
            border-radius:10px;
            padding:8px 12px;
            font-size:13px;
            color:#232F3E;
        ">
            <b>账号</b>：{user}{admin_mark}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("切换账号", use_container_width=True):
        st.session_state.pop("auth_user", None)
        st.rerun()

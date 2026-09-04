from __future__ import annotations


import hashlib
import json
import secrets as _pysecrets
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import streamlit as st


# =====================================================
# V2.7.0 账号门 + 部门（防外传白嫖 / 一套账号管两个软件）
#
# 两种验证后端，Secrets 里配哪种就用哪种：
#
# 1) 统一授权服务器（推荐，和采集插件共用账号库）：
#      auth_server = "https://wz-auth.你的子域.workers.dev"
#      auth_admin_key = "你的 ADMIN_KEY"   # 读部门API Key用，可不配
#    登录时向 Worker /login（kind=web，不占插件设备位）验证；
#    Worker 连不上时自动回退本地 app_users（应急通道）。
#
# 2) 本地账号（V2.6.2 行为，独立于插件）：
#      [app_users]
#      zhangsan = "pbkdf2$260000$<盐>$<哈希>"
#      app_admins = "zhangsan"
#
# 两种都不配 = 不启用登录门（开发/内网行为）。
#
# 部门：Worker 侧账号带 dept / head 字段；本部门 API Key
# 由部门总号在 看板(Worker地址/dashboard) 自助填写，
# get_dept_api_key() 供 app.py 在员工模式下取 Key。
#
# 按账号记账：本地 tasks/usage_log.jsonl + 上报 Worker 部门动态。
# =====================================================


USAGE_LOG_PATH = Path("tasks") / "usage_log.jsonl"

HASH_SCHEME = "pbkdf2"
HASH_ITERATIONS = 260_000

# 连续失败 5 次后锁定 60 秒（同一浏览器会话内，本地计数）。
MAX_FAILS_BEFORE_LOCK = 5
LOCK_SECONDS = 60

# 优化程序事件 → Worker 部门动态里的事件名
WORKER_EVENT_MAP = {
    "login": "login",
    "task_start": "optimize",
    "task_retry": "optimize",
    "export": "export",
}


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
    """读 Secrets 里的本地账号表；没配置返回空。"""
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


def _secrets_str(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:
        return ""


def _worker_server() -> str:
    """统一授权服务器地址（Secrets: auth_server）。"""
    server = _secrets_str("auth_server")
    if server and not server.startswith("http"):
        return ""
    return server.rstrip("/")


def worker_auth_enabled() -> bool:
    return bool(_worker_server())


def _worker_post(
    server: str,
    path: str,
    payload: dict,
    timeout: float = 8.0,
) -> dict:
    """向授权服务器 POST JSON；任何异常都返回带标记的 dict，不抛出。"""
    try:
        req = urllib.request.Request(
            server + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # 带浏览器 UA，避免被 Cloudflare 当机器人拦截（403）
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "_unreachable": True,
        }


def _worker_login(
    server: str,
    user: str,
    password: str,
):
    """返回 (状态, 响应)：ok / denied / unreachable。"""
    data = _worker_post(
        server,
        "/login",
        {"user": user, "pass": password, "kind": "web"},
    )
    if data.get("_unreachable"):
        return "unreachable", data
    if data.get("ok"):
        return "ok", data
    return "denied", data


def get_dept_api_key(dept: str) -> str:
    """取本部门的 API Key（总号在看板上自助填写的那把）。

    需要 Secrets 同时配 auth_server + auth_admin_key。
    取不到（没配/没部门/网络问题）返回空串，调用方回退全局 Key。
    每个 Streamlit 会话只取一次。
    """
    dept = str(dept or "").strip().lower()
    if not dept:
        return ""

    cache = st.session_state.get("_dept_key_cache")
    if cache and cache[0] == dept:
        return cache[1]

    server = _worker_server()
    admin_key = _secrets_str("auth_admin_key")
    if not server or not admin_key:
        return ""

    data = _worker_post(
        server,
        "/admin",
        {
            "admin_key": admin_key,
            "action": "get_dept_key",
            "dept": dept,
        },
        timeout=6,
    )
    key = ""
    if isinstance(data, dict) and data.get("ok"):
        key = str(data.get("key") or "").strip()

    st.session_state["_dept_key_cache"] = (dept, key)
    return key


def auth_enabled() -> bool:
    """登录门是否启用（本地账号或授权服务器任一配置即启用）。"""
    return bool(_load_users()) or worker_auth_enabled()


def current_user() -> str:
    return str(
        st.session_state.get("auth_user", "") or ""
    )


def current_dept() -> str:
    return str(
        st.session_state.get("auth_dept", "") or ""
    )


def is_dept_head() -> bool:
    return bool(
        st.session_state.get("auth_head", False)
    )


def is_admin_user() -> bool:
    user = current_user()
    return bool(user) and user.lower() in _load_admins()


def _report_event_to_worker(event: str, fields: dict) -> None:
    """把优化动态上报到部门看板（尽力而为，绝不阻断）。"""
    server = _worker_server()
    token = str(
        st.session_state.get("auth_token", "") or ""
    )
    kind = WORKER_EVENT_MAP.get(event)
    if not server or not token or not kind:
        return
    try:
        rows = int(fields.get("rows") or 0)
    except Exception:
        rows = 0
    _worker_post(
        server,
        "/report",
        {
            "token": token,
            "k": kind,
            "rows": rows,
            "site": "optimizer",
        },
        timeout=4,
    )


def log_user_event(event: str, **fields) -> None:
    """按账号记账（JSONL 追加 + 上报部门看板）。
    记账失败绝不阻断业务。"""
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
            "dept": current_dept(),
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
    try:
        _report_event_to_worker(event, fields)
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


def _mark_success(user: str, data: dict) -> None:
    st.session_state["auth_user"] = user
    st.session_state["auth_dept"] = str(
        data.get("dept") or ""
    )
    st.session_state["auth_head"] = bool(
        data.get("head")
    )
    st.session_state["auth_token"] = str(
        data.get("token") or ""
    )
    st.session_state["auth_fails"] = 0
    st.session_state.pop("auth_locked_at", None)


def _mark_fail() -> None:
    st.session_state["auth_fails"] = (
        int(st.session_state.get("auth_fails", 0) or 0)
        + 1
    )
    if st.session_state["auth_fails"] >= (
        MAX_FAILS_BEFORE_LOCK
    ):
        st.session_state["auth_locked_at"] = time.time()


def _render_login_page(users: dict[str, str], server: str) -> None:
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
    if not key or not password:
        st.error("请输入账号和密码")
        return

    worker_err = ""

    if server:
        status, data = _worker_login(
            server,
            key,
            password,
        )
        if status == "ok":
            _mark_success(str(data.get("user") or key), data)
            log_user_event("login")
            st.rerun()
            return
        if status == "denied":
            _mark_fail()
            time.sleep(0.8)
            st.error(
                str(data.get("error") or "账号或密码错误")
            )
            return
        worker_err = str(
            data.get("error") or "授权服务器暂时连不上"
        )

    # 本地验证：没有配置服务器，或服务器连不上时的应急通道。
    stored = users.get(key, "")
    if key and stored and verify_password(password, stored):
        _mark_success(key, {})
        log_user_event("login")
        st.rerun()
        return

    _mark_fail()
    time.sleep(0.8)
    if server and not users:
        st.error(
            "连不上授权服务器，请稍后重试或联系管理员。"
        )
    elif server:
        st.error(
            f"账号或密码错误（授权服务器连不上，"
            f"已尝试本地应急通道：{worker_err}）"
        )
    else:
        st.error("账号或密码错误")


def require_login() -> None:
    """登录门：未登录则只显示登录页并停掉后续全部界面。

    本地账号和授权服务器都没配置时直接放行（旧行为）。
    """
    server = _worker_server()
    users = _load_users()
    if not users and not server:
        return

    if current_user():
        return

    _render_login_page(users, server)
    st.stop()


def render_sidebar_badge() -> None:
    """侧边栏显示当前账号 + 切换账号按钮。"""
    if not auth_enabled():
        return

    user = current_user()
    if not user:
        return

    marks = []
    if current_dept():
        head_mark = " · 总号" if is_dept_head() else ""
        marks.append(f" · {current_dept()}部{head_mark}")
    if is_admin_user():
        marks.append(" · 管理员")

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
            <b>账号</b>：{user}{''.join(marks)}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("切换账号", use_container_width=True):
        for name in (
            "auth_user",
            "auth_dept",
            "auth_head",
            "auth_token",
        ):
            st.session_state.pop(name, None)
        st.rerun()

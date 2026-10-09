# -*- coding: utf-8 -*-
"""我的产品桌面版 D1.0 —— 本地程序（像智赢那样装在自己电脑上跑）。

架构：
  界面 = 本地 HTML/JS（desktop/app/），在本机渲染，翻页/筛选零延迟；
  数据 = 还是云端 Worker（员工插件采集照常进库，这里刷新就能看到）；
  引擎 = 整套 AI 优化引擎原样复用（services/…，裸导入不依赖
         Streamlit 运行时），任务跑在本地进程里。

启动：pythonw desktop/main.py → 起 127.0.0.1:17891 → 自动弹独立窗口
（Chrome --app 模式，无地址栏）。端口被占说明已在运行 → 只弹窗口。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# =====================================================
# 路径：桌面版目录 / 仓库根（引擎代码） / 工作目录（tasks/）
# =====================================================

APP_DIR = Path(__file__).resolve().parent      # .../desktop
ROOT = APP_DIR.parent                          # 仓库根
os.chdir(ROOT)                                 # 引擎的 tasks/ 落在根目录
sys.path.insert(0, str(ROOT))

VERSION = "D1.8.1"
APP_DIR_NAME = "app"

DEFAULT_CONFIG = {
    "server": "https://wz-auth.liuweide9438.workers.dev",
    "port": 17891,
    "chrome": "C:\\Program Files\\Google\\Chrome\\App\\Chrome.exe",
    "chrome_fallback": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "proxy": "http://127.0.0.1:1080",
    "admins": ["liuwei1"],
    "admin_key_file": (
        "C:\\Users\\Administrator\\Desktop\\"
        "amazon_aliexpress_collector_v4_20\\auth-server\\admin_config.json"
    ),
    "ai": {"provider": "openai", "key": "", "model": ""},
    # 亚马逊 SP-API（上传/刊登）：凭证只存本机 config.json，
    # 绝不进 git、绝不回显到界面。lwa_base/spapi_base 是测试
    # 覆盖项（留空 = 官方域名）。
    "amazon": {
        "client_id": "", "client_secret": "", "refresh_token": "",
        "app_id": "", "seller_id": "",
        "marketplace": "ATVPDKIKX0DER", "product_type": "PRODUCT",
        "lwa_base": "", "spapi_base": "",
    },
}

CONFIG_PATH = APP_DIR / "config.json"
SESSION_PATH = APP_DIR / "session.json"
INDEX_CACHE_PATH = APP_DIR / "index_cache.json"
INDEX_CACHE_ALL_PATH = APP_DIR / "index_cache.all.json"


def _load_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json_file(path: Path, data) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception:
        pass


CONFIG = dict(DEFAULT_CONFIG)
_over = _load_json_file(CONFIG_PATH, {})
CONFIG.update({k: v for k, v in _over.items() if v not in (None, "")})

SESSION = _load_json_file(SESSION_PATH, {}) or {}

PROXY_MODE = "direct"   # direct / proxy / unknown
_STARTED_AT = time.time()   # 双开检测用：刚启动的实例自己会弹窗口


# =====================================================
# 网络：requests（引擎依赖里本来就有）+ 代理自适应
# =====================================================

import requests  # noqa: E402  (引擎依赖，必装)


def _probe_direct() -> bool:
    """直连 Worker 是否可达（3 秒）。"""
    try:
        requests.get(
            CONFIG["server"] + "/health",
            timeout=3,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return True
    except Exception:
        return False


def setup_proxy() -> None:
    """国外域（workers.dev / openai.com）在这台机器上要走本地代理。
    探测：直连通 → 不设；不通 → 设 HTTP 代理（1080 端口实测同时
    支持 HTTP CONNECT 和 SOCKS5）。设进环境变量，requests 引擎
    全部生效；DeepSeek（国内）也走代理没问题。"""
    global PROXY_MODE

    if _probe_direct():
        PROXY_MODE = "direct"
        return

    proxy = str(CONFIG.get("proxy") or "").strip()
    if not proxy:
        PROXY_MODE = "unknown"
        return

    os.environ["HTTP_PROXY"] = proxy
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["http_proxy"] = proxy
    os.environ["https_proxy"] = proxy
    PROXY_MODE = "proxy"


def src_api(path: str, payload: dict, timeout: int = 30, base: str = "") -> dict:
    """调 Worker（语义对齐 services.sourcing.src_api）：
    浏览器 UA 防 Cloudflare 拦截；瞬态失败 1.5 秒后重试一次。
    base 非空时覆盖 CONFIG["server"]（店铺接口测试用）。"""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }
    root = base or CONFIG["server"]

    for attempt in (1, 2):
        try:
            resp = requests.post(
                root + path,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            return resp.json()

        except Exception as exc:
            if attempt == 1:
                time.sleep(1.5)
                continue

            raise RuntimeError(
                f"连不上服务器（{CONFIG['server']}）：{exc}"
                "；如果一直失败，检查网络/代理是否开启。"
            ) from exc

    return {"ok": False, "error": "unreachable"}


# =====================================================
# 登录态 / 权限
# =====================================================

_ADMIN_KEY_CACHE: str | None = None


def _admin_key() -> str:
    """总管理密钥：只在本机 admin_config.json 里读（跟采集插件
    同源），绝不进员工分发包、绝不打进日志。"""
    global _ADMIN_KEY_CACHE

    if _ADMIN_KEY_CACHE is not None:
        return _ADMIN_KEY_CACHE

    key = ""
    try:
        data = _load_json_file(
            Path(str(CONFIG.get("admin_key_file") or "")), {}
        )
        key = str(
            data.get("ADMIN_KEY") or data.get("admin_key") or ""
        ).strip()
    except Exception:
        key = ""

    _ADMIN_KEY_CACHE = key
    return key


def _logged_user() -> str:
    return str(SESSION.get("user") or "")


def is_admin() -> bool:
    user = _logged_user().lower()
    admins = [str(a).lower() for a in CONFIG.get("admins") or []]
    return bool(user) and (user in admins or bool(SESSION.get("admin")))


def _prod_auth() -> dict:
    payload = {"token": str(SESSION.get("token") or "")}

    if is_admin() and _admin_key():
        payload["admin_key"] = _admin_key()

    return payload


def _profile() -> dict:
    return {
        "user": _logged_user(),
        "dept": str(SESSION.get("dept") or ""),
        "head": bool(SESSION.get("head")),
        "admin": is_admin(),
        "src": bool(SESSION.get("src")),
        "exp_lib": bool(SESSION.get("exp_lib", True)),
    }


# =====================================================
# AI Key 解析（优化开始时用）
# =====================================================

def resolve_ai() -> tuple[str, str, str]:
    """返回 (api_key, provider, model)。优先级：
    本机手填（⚙️ 里保存的）> 服务器全局 Key（管理员保存那把）。"""
    ai = CONFIG.get("ai") or {}
    manual_key = str(ai.get("key") or "").strip()
    manual_provider = str(ai.get("provider") or "openai").lower()
    manual_model = str(ai.get("model") or "").strip()

    if manual_key:
        return (
            manual_key,
            manual_provider,
            manual_model
            or (
                "deepseek-chat"
                if manual_provider == "deepseek"
                else "gpt-4.1-mini"
            ),
        )

    admin_key = _admin_key()

    if admin_key:
        try:
            data = src_api(
                "/admin",
                {"admin_key": admin_key, "action": "ai_key_get"},
                timeout=8,
            )

            if data.get("ok") and str(data.get("key") or "").strip():
                provider = (
                    "deepseek"
                    if str(data.get("provider") or "").lower()
                    == "deepseek"
                    else "openai"
                )
                return (
                    str(data["key"]).strip(),
                    provider,
                    manual_model
                    or (
                        "deepseek-chat"
                        if provider == "deepseek"
                        else "gpt-4.1-mini"
                    ),
                )

        except Exception:
            pass

    return "", "", ""


# =====================================================
# 产品库：索引缓存 / 读写（载荷语义对齐网页版 product_lib）
# =====================================================

_STATUS_LABELS = {
    "wait": "⚪ 待找货",
    "found": "✅ 已找到",
    "none": "❌ 没找到",
}

_COLLECTOR_HEADERS = [
    "父SKU(必填)", "SKU", "库存", "币种", "成本价(必填)", "运费",
    "材料", "包装材料", "语言", "标题(必填)", "颜色",
    "要点1", "要点2", "要点3", "要点4", "要点5",
    "简介", "产品图", "简介图", "参考网址",
]

_INDEX_MEM: dict[str, dict] = {}


def _cache_path(scope: str) -> Path:
    return INDEX_CACHE_ALL_PATH if scope == "all" else INDEX_CACHE_PATH


def fetch_index(scope: str = "", force: bool = False) -> dict:
    """拉产品库索引（自己的库 / 管理员可看全部）。带本地缓存：
    启动瞬间出列表，后台再刷新。"""
    scope = scope if scope in ("", "all") else ""

    if not force:
        cached = _INDEX_MEM.get(scope)

        if isinstance(cached, dict) and cached.get("ok"):
            return cached

        # 同一账号的磁盘缓存：启动瞬间先出列表（_user 防串号）
        user = _logged_user()

        if user:
            disk = _load_json_file(_cache_path(scope), {})

            if (
                isinstance(disk, dict)
                and disk.get("ok")
                and str(disk.get("_user") or "") == user
            ):
                return disk
        else:
            return {"ok": False}

    payload = _prod_auth()

    if scope == "all":
        payload["scope"] = "all"

    data = src_api("/prod_list", payload, timeout=40)

    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "读取产品库失败"))

    data["_fetched_at"] = int(time.time() * 1000)
    data["_user"] = _logged_user()
    _INDEX_MEM[scope] = data
    _save_json_file(_cache_path(scope), data)
    return data


def _index_heal() -> None:
    """写后读延迟自愈：KV 刚写完立刻读可能撞上没同步完的副本
    （返回旧列表、还会被当有效缓存存下来）。变更后 2.5 秒强制
    重拉一遍，把内存+磁盘缓存修正过来。"""
    scopes = ["", "all"] if is_admin() else [""]

    def heal():
        time.sleep(2.5)

        # 三遍兜底：副本同步偶尔超过 2.5 秒（实测撞到过），
        # 每遍都用最新读取覆盖缓存
        for _ in range(3):
            for scope in scopes:
                try:
                    fetch_index(scope, force=True)
                except Exception:
                    pass

            time.sleep(3)

    threading.Thread(target=heal, daemon=True).start()


def _cached_index(scope: str):
    data = _INDEX_MEM.get(scope)
    return data if isinstance(data, dict) and data.get("ok") else None


def _index_save(scope: str, data: dict) -> None:
    _INDEX_MEM[scope] = data
    _save_json_file(_cache_path(scope), data)


def _index_apply_updates(updates: list) -> None:
    """乐观更新：刚改的 cat/status 直接打进本地缓存立即显示
    （云端 KV 最长要 60 秒才全球同步，界面不能干等）；后台
    _index_heal 再拿真数据对齐。"""
    now = int(time.time() * 1000)
    by_pid = {
        str(u.get("pid")): u
        for u in updates
        if isinstance(u, dict) and u.get("pid")
    }

    if not by_pid:
        return

    for scope in ("", "all"):
        data = _cached_index(scope)

        if data is None:
            continue

        hit = False

        for it in data.get("items") or []:
            u = by_pid.get(str(it.get("pid")))

            if not u:
                continue

            hit = True

            if "cat" in u:
                it["cat"] = str(u.get("cat") or "")[:120]

            if "title" in u:
                it["title"] = str(u.get("title") or "")[:200]

            if "img" in u:
                it["img"] = str(u.get("img") or "")[:400]

            if "status" in u:
                it["status"] = str(u.get("status") or "wait")

            it["updated"] = now

        if hit:
            _index_save(scope, data)


def _index_apply_delete(pids: list) -> None:
    gone = {str(p) for p in pids}

    for scope in ("", "all"):
        data = _cached_index(scope)

        if data is None:
            continue

        data["items"] = [
            it for it in (data.get("items") or [])
            if str(it.get("pid")) not in gone
        ]
        _index_save(scope, data)


def _index_apply_import(products: list, by: str) -> None:
    data = _cached_index("")

    if data is None:
        return

    now = int(time.time() * 1000)
    old = {
        str(it.get("pid")): it
        for it in (data.get("items") or [])
    }

    for p in products:
        pid = str(p.get("pid") or "")

        if not pid:
            continue

        base = dict(old.get(pid) or {})
        base.update({
            "pid": pid,
            "owner": by[:32],
            "sku": str(p.get("sku") or "")[:60],
            "title": str(p.get("title") or "")[:200],
            "model": str(p.get("model") or "")[:60],
            "img": str(p.get("img") or "")[:400],
            "n_var": len(p.get("variants") or []),
            "n_rows": len(p.get("raw") or []),
            "updated": now,
        })
        base.setdefault("created", now)
        base.setdefault("cat", "")
        base.setdefault("kw", "")
        base.setdefault("status", "wait")
        base.setdefault("best", None)
        base.setdefault("has_opt", False)
        base.setdefault("opt_at", 0)
        base.setdefault("last_round", 0)
        old[pid] = base

    data["items"] = list(old.values())
    _index_save("", data)


# =====================================================
# 引擎复用（整套 AI 优化引擎：services/… 裸导入）
# =====================================================

from core.data_reader import read_workbook  # noqa: E402
from core.models import FieldMap, ProductRecord  # noqa: E402
from core.record_builder import build_product_records  # noqa: E402
from services.json_storage import load_json, save_json  # noqa: E402
from services.listing_exporter import ListingExporter  # noqa: E402
from services.result_storage import (  # noqa: E402
    load_failed_items,
    load_profiles,
)
from services.sourcing import fallback_model, first_url  # noqa: E402
from services.task_control import save_control  # noqa: E402
from services.task_manager import (  # noqa: E402
    create_task,
    get_task_dir,
    load_status,
)
from services.task_worker import start_worker  # noqa: E402

_TEMPLATE_FIELDS = FieldMap(
    parent_sku="父SKU(必填)",
    sku="SKU",
    title="标题(必填)",
    bullets=("要点1", "要点2", "要点3", "要点4", "要点5"),
    description="简介",
    images="产品图",
    detail_images="简介图",
    reference_url="参考网址",
    language="语言",
    color="颜色",
)

_RAW_MAX_CHARS = 300000

TASK_RUNNING_STATUS = ["created", "running", "processing"]


def _norm_pid(raw: str) -> str:
    text = re.sub(r"\s+", "", str(raw or "").strip().lower())
    text = re.sub(r"[^a-z0-9_-]", "", text)
    return text[:64]


# ---- 以下三段从 services/workbench.py 原样拷贝（那里缠着
# Streamlit，这里是纯 Python；语义逐行对齐，含 pid 规则）----

def _group_records(records: list) -> tuple[list, dict, list]:
    groups: dict[str, dict] = {}
    order: list[str] = []

    for rec in records:
        key_raw = (rec.parent_sku or rec.sku or "").strip()
        title = (rec.title or "").strip()

        if key_raw:
            pid = _norm_pid(key_raw)

        elif title:
            pid = "t" + hashlib.sha1(
                title.encode("utf-8")
            ).hexdigest()[:16]

        else:
            continue

        if pid not in groups:
            groups[pid] = {
                "pid": pid,
                "sku": key_raw[:60],
                "title": title[:200],
                "model": fallback_model(title),
                "img": "",
                "ref_url": "",
                "variants": [],
                "raw": [],
            }
            order.append(pid)

        group = groups[pid]

        if not group["title"] and title:
            group["title"] = title[:200]

        if not group["img"] and rec.image_urls:
            group["img"] = rec.image_urls[0][:400]

        if not group["ref_url"]:
            group["ref_url"] = first_url(
                rec.raw_data.get("参考网址")
            )

        raw = asdict(rec)

        raw["bullets"] = list(raw["bullets"] or [])
        raw["image_urls"] = list(raw["image_urls"] or [])
        raw["detail_image_urls"] = list(
            raw["detail_image_urls"] or []
        )
        group["raw"].append(raw)

    fat = []

    for pid in order:
        try:
            raw_str = json.dumps(
                groups[pid]["raw"], ensure_ascii=False
            )
        except Exception:
            raw_str = ""

        if len(raw_str) > _RAW_MAX_CHARS:
            groups[pid]["raw"] = []
            fat.append(pid)

    return [groups[pid] for pid in order], {
        "rows": len(records),
        "products": len(groups),
    }, fat


def _record_from_raw(rd: dict) -> ProductRecord:
    rd = dict(rd or {})
    return ProductRecord(
        row_number=int(rd.get("row_number") or 0),
        sku=str(rd.get("sku") or ""),
        parent_sku=str(rd.get("parent_sku") or ""),
        child_sku=str(rd.get("child_sku") or ""),
        title=str(rd.get("title") or ""),
        short_title=str(rd.get("short_title") or ""),
        bullets=tuple(rd.get("bullets") or []),
        description=str(rd.get("description") or ""),
        image_urls=tuple(rd.get("image_urls") or []),
        detail_image_urls=tuple(
            rd.get("detail_image_urls") or []
        ),
        language=str(rd.get("language") or ""),
        raw_data=dict(rd.get("raw_data") or {}),
    )


def _opt_from_profile(profile: dict, task_id: str) -> dict | None:
    def d(value) -> dict:
        return value if isinstance(value, dict) else {}

    gt = d(profile.get("generated_title"))
    short_r = d(profile.get("short_title_result"))
    bullet_r = d(profile.get("bullet_result"))
    desc_r = d(profile.get("description_result"))

    bullets = (
        bullet_r.get("bullets")
        or bullet_r.get("bullet_points")
        or bullet_r.get("points")
        or []
    )

    hl = profile.get("highlight_result")

    if isinstance(hl, dict):
        highlights = hl.get("highlights") or hl.get("highlight") or []
    elif isinstance(hl, list):
        highlights = hl
    else:
        highlights = []

    seo = d(profile.get("seo"))
    seo_terms = (
        list(seo.get("primary_keywords") or [])
        + list(seo.get("secondary_keywords") or [])
        + list(seo.get("model_keywords") or [])
    )

    image_r = d(profile.get("image_result"))
    image = (
        str(image_r.get("main_image_optimized") or "")
        if image_r.get("status") == "success"
        else ""
    )

    opt = {
        "title": str(gt.get("title") or "")[:600],
        "short_title": str(
            short_r.get("short_title")
            or short_r.get("title")
            or short_r.get("text")
            or ""
        )[:300],
        "bullets": [
            str(b)[:600] for b in bullets if str(b or "").strip()
        ][:8],
        "description": str(
            desc_r.get("description")
            or desc_r.get("text")
            or desc_r.get("content")
            or ""
        )[:8000],
        "highlight": [
            str(h)[:300] for h in highlights if str(h or "").strip()
        ][:12],
        "seo": [
            str(s)[:200] for s in seo_terms if str(s or "").strip()
        ][:20],
        "image": image[:400],
        "task_id": str(task_id)[:40],
    }

    if opt["title"] or opt["bullets"] or opt["description"]:
        return opt

    return None


def _paste_to_dataframe(text: str):
    import pandas as pd

    text = str(text or "").strip()

    if not text:
        return None, "请先粘贴内容"

    try:
        data = json.loads(text)
    except Exception:
        return None, (
            "粘贴的内容不是有效 JSON"
            "（请用插件面板的「发送到优化」复制）"
        )

    rows = data.get("rows") if isinstance(data, dict) else data

    if not isinstance(rows, list) or not rows:
        return None, "粘贴内容里没有产品行"

    records = [
        [str(row.get(h, "") or "") for h in _COLLECTOR_HEADERS]
        for row in rows[:2000]
        if isinstance(row, dict)
    ]

    if not records:
        return None, "粘贴内容里没有可识别的产品行"

    return pd.DataFrame(records, columns=_COLLECTOR_HEADERS), None


# =====================================================
# 优化任务（本地引擎线程 + 结果写回云端）
# =====================================================

TASK_LOCK = threading.Lock()
TASK = {
    "phase": "idle",   # idle/reading/running/writing/done/error
    "task_id": "",
    "pids": [],
    "total": 0,
    "completed": 0,
    "failed": 0,
    "message": "",
    "started_at": 0,
    "written_back": False,
}


def _task_reset() -> None:
    TASK.update(
        phase="idle", task_id="", pids=[], total=0, completed=0,
        failed=0, message="", started_at=0, written_back=False,
    )


def _opt_options() -> dict:
    """总后台优化设置（/opt_config，token 鉴权）→ 引擎开关。"""
    modules = {}
    desc_to_ai = True

    try:
        data = src_api(
            "/opt_config", {"token": str(SESSION.get("token") or "")},
            timeout=8,
        )

        if data.get("ok"):
            desc_to_ai = data.get("desc_to_ai", True) is not False
            raw = data.get("modules")

            if isinstance(raw, dict):
                modules = raw
    except Exception:
        pass

    def on(key):
        return modules.get(key) is not False

    return {
        "title": on("title"),
        "short_title": on("short_title"),
        "highlight": on("highlight"),
        "bullet": on("bullet"),
        "description": desc_to_ai and on("description"),
        "seo": on("seo"),
        "optimize_images": False,
        "max_workers": 4,
    }


def _start_optimize_async(pids: list) -> None:
    """后台线程：拉资料 → 重排行号 → 建任务 → 引擎起跑。"""

    def work():
        try:
            api_key, provider, model = resolve_ai()

            if not api_key:
                with TASK_LOCK:
                    TASK["phase"] = "error"
                    TASK["message"] = (
                        "还没配置 AI：点右上角 ⚙️ 填 Key 并保存，"
                        "或让管理员在服务器保存全局 Key。"
                    )
                return

            if provider == "deepseek":
                os.environ["OPENAI_BASE_URL"] = (
                    "https://api.deepseek.com"
                )
            else:
                os.environ["OPENAI_BASE_URL"] = (
                    "https://api.openai.com/v1"
                )

            records: list[ProductRecord] = []
            rec_pids: list[str] = []
            skipped: list[str] = []

            for i, pid in enumerate(pids):
                try:
                    data = src_api(
                        "/prod_list", _prod_auth() | {"pid": pid},
                        timeout=30,
                    )
                except Exception:
                    skipped.append(pid)
                    continue

                product = (
                    (data.get("product") or {}) if data.get("ok") else {}
                )

                for rd in product.get("raw") or []:
                    records.append(_record_from_raw(rd))
                    rec_pids.append(pid)

                with TASK_LOCK:
                    TASK["completed"] = i + 1
                    TASK["message"] = (
                        f"读取产品资料… {i + 1} / {len(pids)}"
                    )

            if skipped:
                with TASK_LOCK:
                    TASK["message"] = (
                        f"{len(skipped)} 个产品资料不全已跳过"
                    )

            if not records:
                with TASK_LOCK:
                    TASK["phase"] = "error"
                    TASK["message"] = (
                        "选中的产品都没有完整资料"
                        "（旧库数据），重新导入同一份 Excel 补全。"
                    )
                return

            renumbered: list[ProductRecord] = []
            row_pid: dict[str, str] = {}

            from dataclasses import replace

            for i, (rec, pid) in enumerate(zip(records, rec_pids)):
                renumbered.append(replace(rec, row_number=i + 2))
                row_pid[str(i + 2)] = pid

            options = _opt_options()

            task_id = create_task(
                total_products=len(renumbered),
                filename="我的产品.xlsx",
            )

            task_dir = get_task_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)

            keep = [p for p in pids if p not in set(skipped)]
            save_json(
                task_dir / "pl_manifest.json",
                {
                    "task_id": task_id,
                    "pids": keep,
                    "row_pid": row_pid,
                    "written_back": False,
                    "at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                },
            )

            with TASK_LOCK:
                TASK["task_id"] = task_id
                TASK["total"] = len(renumbered)
                TASK["completed"] = 0
                TASK["phase"] = "running"
                TASK["message"] = ""

            start_worker(renumbered, task_id, api_key, model, options)

        except Exception as exc:
            with TASK_LOCK:
                TASK["phase"] = "error"
                TASK["message"] = f"{exc}"
            traceback.print_exc()

    threading.Thread(target=work, daemon=True).start()


def _write_back_opt(task_id: str, manifest: dict) -> int:
    """跑完 → profiles 对回 pid → /prod_opt_set 写回云端（20/批）。"""
    row_pid = manifest.get("row_pid") or {}
    profiles = load_profiles(task_id)
    first_by_pid: dict[str, dict] = {}

    for profile in sorted(
        (p for p in profiles if isinstance(p, dict)),
        key=lambda p: (
            (p.get("source_identity") or {}).get("source_row_index")
            or 0
        ),
    ):
        rid = (profile.get("source_identity") or {}).get(
            "source_row_index"
        )
        pid = row_pid.get(str(rid)) if rid is not None else ""

        if pid and pid not in first_by_pid:
            first_by_pid[pid] = profile

    updates = []

    for pid, profile in first_by_pid.items():
        opt = _opt_from_profile(profile, task_id)

        if opt:
            updates.append({"pid": pid, "opt": opt})

    n = 0

    for start in range(0, len(updates), 20):
        chunk = updates[start:start + 20]
        resp = src_api(
            "/prod_opt_set",
            _prod_auth() | {"updates": chunk},
            timeout=60,
        )

        if not resp.get("ok"):
            raise RuntimeError(
                str(resp.get("error") or "写回产品失败")
            )

        n += int(resp.get("n") or 0)

    manifest["written_back"] = True
    manifest["n_written"] = n
    save_json(get_task_dir(task_id) / "pl_manifest.json", manifest)
    return n


def _poll_task() -> dict:
    """前端每次轮询：合并引擎状态文件；跑完自动写回 + 刷新索引。"""
    with TASK_LOCK:
        snap = dict(TASK)

    task_id = snap.get("task_id") or ""

    if not task_id or snap.get("phase") not in (
        "running", "writing", "done",
    ):
        return snap

    status = load_status(task_id) or {}
    state = str(status.get("status") or "")

    with TASK_LOCK:
        TASK["completed"] = int(status.get("completed", 0) or 0)
        TASK["failed"] = int(status.get("failed", 0) or 0)
        TASK["state"] = state

    if state in TASK_RUNNING_STATUS or state == "paused":
        return dict(TASK)

    # ---- 终态：写回 ----
    if TASK.get("phase") == "running":
        with TASK_LOCK:
            TASK["phase"] = "writing"
            TASK["message"] = "把优化结果写回产品…"

        manifest = load_json(
            get_task_dir(task_id) / "pl_manifest.json", default=None
        )

        if isinstance(manifest, dict) and manifest.get("pids"):
            try:
                n = _write_back_opt(task_id, manifest)

                for scope in ("", "all"):
                    try:
                        fetch_index(scope, force=True)
                    except Exception:
                        pass

                with TASK_LOCK:
                    TASK["phase"] = "done"
                    TASK["written_back"] = True
                    TASK["message"] = (
                        f"任务完成：结果已写回 {n} 个产品"
                        if n
                        else "任务结束，没有可写回的结果"
                    )

            except Exception as exc:
                with TASK_LOCK:
                    TASK["phase"] = "error"
                    TASK["message"] = (
                        f"结果写回失败：{exc}（已优化的都在，"
                        "点「重新写回」再试）"
                    )
                    TASK["written_back"] = False

        else:
            with TASK_LOCK:
                TASK["phase"] = "done"
                TASK["message"] = "任务结束。"

    return dict(TASK)


# =====================================================
# 导出（复用 ListingExporter：和网页版同一个 Excel 形状）
# =====================================================

def _export_pids(pids: list) -> tuple[str, bytes] | None:
    import pandas as pd

    frames = []
    profiles = []
    pos = 0

    for pid in pids:
        pid = str(pid)

        try:
            data = src_api(
                "/prod_list", _prod_auth() | {"pid": pid}, timeout=30,
            )
        except Exception:
            continue

        rec = (data.get("product") or {}) if data.get("ok") else {}
        raw_rows = rec.get("raw") or []

        if not raw_rows:
            continue

        start_index = pos + 2
        frames.append(
            pd.DataFrame(
                [dict(r.get("raw_data") or {}) for r in raw_rows]
            )
        )
        pos += len(raw_rows)

        opt = rec.get("opt") or {}

        if opt.get("title") or (opt.get("bullets") or []):
            profile = {
                "source_identity": {
                    "source_row_index": start_index,
                    "sku": str(rec.get("sku") or ""),
                },
                "generated_title": {
                    "title": str(opt.get("title") or ""),
                },
                "short_title_result": {
                    "short_title": str(opt.get("short_title") or ""),
                },
                "bullet_result": {
                    "bullets": list(opt.get("bullets") or []),
                },
                "description_result": {
                    "description": str(opt.get("description") or ""),
                },
                "highlight_result": {
                    "highlights": list(opt.get("highlight") or []),
                },
            }

            image = str(opt.get("image") or "")

            if image:
                profile["image_result"] = {
                    "status": "success",
                    "main_image_optimized": image,
                    "optimized_images": [image],
                }

            profiles.append(profile)

    if not profiles:
        return None

    dataframe = pd.concat(frames, ignore_index=True)
    buf = ListingExporter.export_unified(dataframe, profiles)
    return buf.getvalue()


# =====================================================
# 亚马逊 SP-API 上传（刊登）：LWA 令牌 + JSON_LISTINGS_FEED
# D1.4.1：店铺密钥存在服务器（worker /amz_store），桌面端每次
# 上传只拿 1 小时短命令牌——换电脑、换网络都能传多个店铺；
# 员工机上拿不到 refresh_token/Client Secret。
# 本机 config 的 amazon 段仅作旧数据/测试兜底（store_server
# 是隐藏的店铺接口测试覆盖项）。
# =====================================================

_MARKETPLACES = {           # marketplaceId → (区域, 语言, 名称)
    "ATVPDKIKX0DER": ("na", "en_US", "美国 US"),
    "A2EUQ1WTGCTBG2": ("na", "en_CA", "加拿大 CA"),
    "A1AM78C64UM0Y8": ("na", "es_MX", "墨西哥 MX"),
    "A2Q3Y263D00KWC": ("na", "pt_BR", "巴西 BR"),
    "A1F83G8C2ARO7P": ("eu", "en_GB", "英国 UK"),
    "A1PA6795UKMFR9": ("eu", "de_DE", "德国 DE"),
    "A13V1IB3VIYZZH": ("eu", "fr_FR", "法国 FR"),
    "APJ6JRA9NG5V4": ("eu", "it_IT", "意大利 IT"),
    "A1RKKUPIHCS9HS": ("eu", "es_ES", "西班牙 ES"),
    "A2NODRKZP88ZB9": ("eu", "nl_NL", "荷兰 NL"),
    "A1805IZSGTT6HS": ("eu", "se_SE", "瑞典 SE"),
    "A1C3SOZRARQ6R3": ("eu", "pl_PL", "波兰 PL"),
    "A2VIGQ35RCS4UG": ("eu", "en_AE", "阿联酋 AE"),
    "A17E79C6D8DWNP": ("eu", "ar_SA", "沙特 SA"),
    "A21TJRUUN4KGV": ("eu", "en_IN", "印度 IN"),
    "A1VC38T7YXB528": ("fe", "ja_JP", "日本 JP"),
    "A39IBJ37TRP1C6": ("fe", "en_AU", "澳大利亚 AU"),
    "A19VAU5U5O7RUS": ("fe", "en_SG", "新加坡 SG"),
}

_AMZ_LOCK = threading.Lock()
_AMZ_TASK = {
    "phase": "idle",   # idle/building/uploading/polling/done/error
    "message": "", "total": 0, "pids": [],
    "feed_id": "", "result": None, "started_at": 0,
}
_AMZ_TOKEN = {"at": "", "exp": 0.0}       # 旧本机凭证路径的令牌缓存
_AMZ_TOKENS = {}                          # 店铺id → {at,exp}（服务器短令牌）
_AMZ_RUN = {"store": None, "mid": "", "seller_id": ""}   # 当前上传上下文
_AMZ_AUTHZ = {"state": "idle", "message": "", "url": ""}
_AUTHZ_PORT = 9999
_AMZ_UPLOADS_PATH = APP_DIR / "amazon_uploads.json"


def _amz_cfg() -> dict:
    return CONFIG.get("amazon") or {}


def _amz_srv() -> str:
    """店铺接口的服务器地址（默认跟主服务器同源）。"""
    return (
        str(_amz_cfg().get("store_server") or "").strip()
        or CONFIG["server"]
    )


def _amz_store_api(payload: dict, timeout: int = 30) -> dict:
    body = _prod_auth() | payload
    return src_api("/amz_store", body, timeout=timeout, base=_amz_srv())


def _amz_stores() -> tuple[list, str]:
    """服务器上的店铺清单（无密钥）。返回 (stores, 错误信息)。
    刚绑定完 KV 边缘缓存可能还没同步（约 60 秒）——把授权流程
    刚拿到的店铺乐观合并进来。"""
    try:
        r = _amz_store_api({"action": "list"})
    except Exception as exc:
        return [], f"连不上服务器：{exc}"[:200]
    if not r.get("ok"):
        return [], str(r.get("error") or "读取失败")[:200]
    stores = list(r.get("stores") or [])
    just = _AMZ_AUTHZ.get("store") or {}
    if (
        _AMZ_AUTHZ.get("state") == "done"
        and just.get("id")
        and not any(str(s.get("id")) == str(just.get("id"))
                    for s in stores)
    ):
        stores = [just] + stores
    return stores, ""


def _amz_store_save(store: dict) -> dict:
    try:
        return _amz_store_api({"action": "save", "store": store})
    except Exception as exc:
        return {"ok": False, "error": f"连不上服务器：{exc}"[:300]}


def _amz_store_delete(sid: str) -> dict:
    try:
        return _amz_store_api({"action": "delete", "id": sid})
    except Exception as exc:
        return {"ok": False, "error": f"连不上服务器：{exc}"[:300]}


def _amz_app_info() -> dict:
    """公共应用凭证状态（服务器上配一次，全公司共用）。"""
    try:
        r = _amz_store_api({"action": "app_get"}, timeout=15)
    except Exception as exc:
        return {"set": False, "app_id": "", "err": f"连不上服务器：{exc}"[:150]}
    if not r.get("ok"):
        return {"set": False, "app_id": "", "err": str(r.get("error") or "")[:150]}
    app = r.get("app") or {}
    return {
        "set": bool(app.get("set")),
        "app_id": str(app.get("app_id") or ""),
        "err": "",
    }


def _ean_api(payload: dict, timeout: int = 25) -> dict:
    return src_api("/ean", _prod_auth() | payload, timeout=timeout,
                   base=_amz_srv())


def _amz_ean_assign(keys: list) -> tuple[dict, str, str]:
    """给一批 产品#SKU 领 EAN 条码（已领过的原样返回=永久绑定）。
    返回 (码表, 错误信息, 前缀)。读不到码不挡上传——feed 不带
    standard_product_id 而已，像侵权词典一样降级。"""
    try:
        r = _ean_api({"action": "assign", "keys": keys})
    except Exception as exc:
        return {}, f"连不上服务器：{exc}"[:150], ""
    if not r.get("ok"):
        return {}, str(r.get("error") or "领取失败")[:150], ""
    return dict(r.get("eans") or {}), "", str(r.get("prefix") or "")


_AMZ_UP = {"sid": "", "map": {}, "at": 0.0}   # 已上传清单缓存（按店铺）


def _amz_uploaded_map(sid: str, force: bool = False) -> dict:
    """这家店已上传的产品清单 {pid: 时间}（智赢式：传过的下次跳过）。
    读不到当空表——去重只是锦上添花，不该挡上传。"""
    if not sid:
        return {}
    now = time.time()
    if (
        not force
        and _AMZ_UP["sid"] == sid
        and now - float(_AMZ_UP.get("at") or 0) < 30
    ):
        return _AMZ_UP["map"]
    try:
        r = _amz_store_api({"action": "up_get", "id": sid}, timeout=15)
    except Exception:
        # 读失败退回旧缓存（同店），没有就当空表
        return dict(_AMZ_UP["map"]) if _AMZ_UP["sid"] == sid else {}
    if not r.get("ok"):
        return dict(_AMZ_UP["map"]) if _AMZ_UP["sid"] == sid else {}
    m = dict(r.get("uploaded") or {})
    _AMZ_UP.update(sid=sid, map=m, at=now)
    return m


def _amz_active_store(stores: list) -> dict | None:
    sid = str(_amz_cfg().get("active_store") or "")
    for s in stores:
        if str(s.get("id")) == sid:
            return s
    return stores[0] if stores else None


def _amz_spapi_host() -> str:
    base = str(_amz_cfg().get("spapi_base") or "").strip()
    if base:
        return base.rstrip("/")
    region = str(_amz_cfg().get("region") or "na").lower()
    mk = _AMZ_RUN.get("mid") or str(_amz_cfg().get("marketplace") or "")
    if mk in _MARKETPLACES:
        region = _MARKETPLACES[mk][0]
    return f"https://sellingpartnerapi-{region}.amazon.com"


def _amz_lwa_host() -> str:
    base = str(_amz_cfg().get("lwa_base") or "").strip()
    return base.rstrip("/") if base else "https://api.amazon.com"


def _amz_task_snap() -> dict:
    with _AMZ_LOCK:
        return dict(_AMZ_TASK)


def amz_token() -> str:
    """LWA 访问令牌（约 1 小时有效，自动续）。
    当前上传上下文有绑定店铺 → 向服务器拿短令牌（密钥不落地）；
    否则走本机 config 里的旧凭证。"""
    store = _AMZ_RUN.get("store")
    if store and store.get("id"):
        sid = str(store["id"])
        now = time.time()
        c = _AMZ_TOKENS.get(sid) or {}
        if c.get("at") and now < float(c.get("exp") or 0) - 120:
            return c["at"]
        r = _amz_store_api({"action": "token", "id": sid}, timeout=25)
        at = str(r.get("access_token") or "") if r.get("ok") else ""
        if not at:
            raise RuntimeError(
                "拿店铺令牌失败："
                + str(r.get("error") or "服务器没返回令牌")[:300]
            )
        _AMZ_TOKENS[sid] = {
            "at": at,
            "exp": now + float(r.get("expires_in") or 3600),
        }
        return at

    c = _amz_cfg()
    if not all(str(c.get(k) or "").strip() for k in (
        "refresh_token", "client_id", "client_secret",
    )):
        raise RuntimeError("亚马逊授权还没配置（点「⚙️ 授权设置」）")

    now = time.time()
    if _AMZ_TOKEN["at"] and now < _AMZ_TOKEN["exp"] - 120:
        return _AMZ_TOKEN["at"]

    resp = requests.post(
        _amz_lwa_host() + "/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": str(c.get("refresh_token")),
            "client_id": str(c.get("client_id")),
            "client_secret": str(c.get("client_secret")),
        },
        timeout=20,
    )
    data = {}
    try:
        data = resp.json()
    except Exception:
        pass
    at = str(data.get("access_token") or "")
    if resp.status_code != 200 or not at:
        raise RuntimeError(
            "拿亚马逊令牌失败："
            + str(data.get("error_description") or data.get("error")
                  or resp.text)[:300]
        )
    _AMZ_TOKEN["at"] = at
    _AMZ_TOKEN["exp"] = now + float(data.get("expires_in") or 3600)
    return at


def amz_verify() -> dict:
    """配置自检：令牌能用 + 列出这个店铺参加的站点。"""
    token = amz_token()
    resp = requests.get(
        _amz_spapi_host() + "/sellers/v1/marketplaceParticipations",
        headers={"x-amz-access-token": token},
        timeout=25,
    )
    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"亚马逊返回 {resp.status_code}：{resp.text[:300]}",
        }
    mks = []
    for it in resp.json() or []:
        mk = (it or {}).get("marketplace") or {}
        if mk.get("id"):
            mks.append({
                "id": str(mk.get("id")),
                "name": str(mk.get("name") or "")[:80],
            })
    return {"ok": True, "marketplaces": mks}


def _amz_api(method: str, path: str, token: str, payload=None):
    headers = {
        "x-amz-access-token": token,
        "Content-Type": "application/json",
    }
    resp = requests.request(
        method,
        _amz_spapi_host() + path,
        headers=headers,
        data=(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else None
        ),
        timeout=40,
    )
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:400]}
    return resp.status_code, body


# ---- 一键授权：本地 9999 端口接亚马逊跳回来的授权码，自动换令牌 ----

def _amz_begin_authorize(bind: dict) -> str:
    """智赢式授权第 1 步：生成授权链接（给用户拿去店铺自己的浏览器
    环境打开，绝不在本机自动弹——IP 不同才是关联风险）。"""
    global _AMZ_AUTHZ

    app = _amz_app_info()
    if app.get("err"):
        raise RuntimeError(f"读应用凭证状态失败：{app['err']}")
    if not (app.get("set") and app.get("app_id")):
        raise RuntimeError(
            "管理员还没配置公共应用凭证——点「🔧 配置应用凭证」粘贴一次即可"
        )

    state = secrets.token_hex(8)
    url = (
        "https://sellercentral.amazon.com/apps/authorize/consent"
        f"?application_id={app['app_id']}&version=beta&state={state}"
    )
    _AMZ_AUTHZ = {
        "state": "running",
        "message": (
            "① 复制授权链接 ② 在这家店自己的浏览器环境（紫鸟/智赢）"
            "里打开并登录 ③ 点「同意」 ④ 复制跳转后浏览器地址栏的"
            "完整网址，粘回下面"
        ),
        "url": url,
        "oauth_state": state,
        "bind": bind,
    }
    return url


def _amz_finish_authorize(pasted: str) -> dict:
    """智赢式授权第 2 步：用户把跳转后的网址粘回来 → 抽出 code →
    服务器端换 refresh_token 并存店铺（本机全程摸不到密钥）。"""
    global _AMZ_AUTHZ

    if _AMZ_AUTHZ.get("state") == "done":
        return {
            "ok": True, "state": "done",
            "message": _AMZ_AUTHZ.get("message") or "",
            "store": _AMZ_AUTHZ.get("store") or {},
        }
    if _AMZ_AUTHZ.get("state") != "running":
        raise RuntimeError("还没开始授权（先点「开始授权」拿链接）")

    raw = str(pasted or "").strip()
    if not raw:
        raise RuntimeError("先把浏览器地址栏的完整网址粘进来")
    if "://" not in raw:
        raw = "http://" + raw      # urlparse 没有 scheme 解不出 query

    q = parse_qs(urlparse(raw).query)
    code = (q.get("spapi_oauth_code") or q.get("code") or [""])[0]
    err = (q.get("error_description") or q.get("error") or [""])[0]

    if err:
        _AMZ_AUTHZ.update(
            state="error",
            message="亚马逊拒绝了授权：" + str(err)[:250],
        )
    elif not code:
        _AMZ_AUTHZ.update(
            state="error",
            message=(
                "这个网址里没有授权码——要粘「点同意之后」跳转到的"
                "那个网址（地址栏是 localhost 打不开没关系，照样复制）"
            ),
        )
    else:
        want = str(_AMZ_AUTHZ.get("oauth_state") or "")
        got = (q.get("state") or [""])[0]
        if want and got != want:
            _AMZ_AUTHZ.update(
                state="error",
                message=(
                    "回链对不上（可能是上一次的旧网址）——"
                    "重新点「开始授权」再走一遍"
                ),
            )
        else:
            r = _amz_store_api(
                {
                    "action": "exchange",
                    "code": code,
                    "store": _AMZ_AUTHZ.get("bind") or {},
                },
                timeout=45,
            )
            if not r.get("ok"):
                _AMZ_AUTHZ.update(
                    state="error",
                    message="绑定失败：" + str(r.get("error") or "")[:250],
                )
            else:
                _AMZ_TOKENS.clear()
                _AMZ_AUTHZ.update(
                    state="done",
                    message=(
                        "✅ 授权成功，店铺已绑定并安全保存在服务器"
                        "（换电脑免重绑）"
                    ),
                    store=(r.get("store") or {}),
                )

    ok = _AMZ_AUTHZ.get("state") == "done"
    msg = _AMZ_AUTHZ.get("message") or ""
    return {
        "ok": ok,
        "state": _AMZ_AUTHZ.get("state") or "error",
        "message": msg,
        "error": "" if ok else msg,   # api() 抛错时前端拿得到文案
        "store": (_AMZ_AUTHZ.get("store") or {}) if ok else {},
    }


# ---- 刊登数据：产品 → JSON_LISTINGS_FEED 的 messages ----

def _dict_words() -> list:
    """读服务器上的侵权词对照表 [(bad, fix)]（worker /dict，全公司
    共享，智赢「侵权词替换」同款）。读不到就当没有——词典服务故障
    不该挡住上传。"""
    try:
        r = src_api(
            "/dict", _prod_auth() | {"kind": "words", "action": "get"},
            timeout=20,
        )
        if r.get("ok"):
            doc = r.get("doc") or {}
            return [
                (str(p.get("bad") or ""), str(p.get("fix") or ""))
                for p in (doc.get("pairs") or [])
            ]
    except Exception:
        pass
    return []


def _dict_btmap() -> list:
    """读「分类→亚马逊类目节点」映射 [{cat,node,name}]（worker /dict
    btmap，全公司共享）。上传时按每个产品自己的分类带上
    browse_classification，把商品放进亚马逊对应的分类货架。
    读不到=空，不挡上传。"""
    try:
        r = src_api(
            "/dict", _prod_auth() | {"kind": "btmap", "action": "get"},
            timeout=20,
        )
        if r.get("ok"):
            doc = r.get("doc") or {}
            return [
                {
                    "cat": str(m.get("cat") or ""),
                    "node": str(m.get("node") or ""),
                    "name": str(m.get("name") or ""),
                }
                for m in (doc.get("map") or [])
            ]
    except Exception:
        pass
    return []


def _apply_words(text: str, pairs: list) -> tuple[str, int]:
    """按对照表替换（大小写不敏感）。返回 (新文本, 替换次数)。"""
    n = 0
    for bad, fix in pairs:
        if not bad:
            continue
        text, k = re.subn(
            re.escape(bad),
            (fix or "").replace("\\", "\\\\"),
            text, flags=re.IGNORECASE,
        )
        n += k
    return text, n


# =====================================================
# D1.8.0 AI 自动归类（像 AI 优化一样点一下跑后台：
# AI 从自己的分类树里挑最合适的分类，绝不发明新类；
# 没挑中=保持不变并计数，不会乱改）
# =====================================================

_AUTOCAT = {
    "phase": "idle", "message": "", "total": 0, "done": 0,
    "ok": 0, "miss": 0, "err": 0,
}
_AUTOCAT_LOCK = threading.Lock()


def _ai_chat_one(prompt: str, key: str, provider: str, model: str,
                 timeout: int = 45, max_tokens: int = 300) -> str:
    """一次普通对话补全（归类用，温度 0 求稳）。base 可被
    CONFIG["ai"]["base"] 覆盖（本地 mock 测试用）。"""
    base = str((CONFIG.get("ai") or {}).get("base") or "").strip() or (
        "https://api.deepseek.com"
        if provider == "deepseek" else "https://api.openai.com/v1"
    )
    r = requests.post(
        base.rstrip("/") + "/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return str(
        (((data.get("choices") or [{}])[0].get("message") or {})
         .get("content")) or ""
    )


def _cat_tree_paths() -> list:
    """当前账号分类树的全部完整路径（/cat_lib 文档节点）。"""
    paths: list = []
    try:
        data = src_api(
            "/cat_lib", _prod_auth() | {"action": "get"}, timeout=15,
        )
        if data.get("ok"):
            for n in (data.get("doc") or {}).get("nodes") or []:
                name = str((n or {}).get("name") or "").strip()
                parent = str((n or {}).get("parent") or "").strip()
                if name and "/" not in name:
                    paths.append(
                        (parent + "/" + name) if parent else name
                    )
    except Exception:
        pass
    return paths


def _autocat_launch(pids: list) -> None:
    """置运行态 + 起线程（POST 端点 / 优化完成自动链 共用）。"""
    with _AUTOCAT_LOCK:
        _AUTOCAT.update(
            phase="running", message="准备中…",
            total=len(pids), done=0, ok=0, miss=0, err=0,
        )
    _autocat_start(pids)


def _autocat_start(pids: list) -> None:
    """后台线程：让 AI 从分类树里挑分类，批量写回。
    D1.8.1 起 10 个产品合一次问（分类树只带一遍=省 token），
    AI 答案不在树里=丢弃不动（防乱编）。"""

    BATCH = 10   # 一次问几个产品（树只带一遍，10 个一问最省）

    def work():
        ok = miss = err = 0
        updates: list = []
        n = len(pids)
        try:
            key, provider, model = resolve_ai()
            if not key:
                with _AUTOCAT_LOCK:
                    _AUTOCAT.update(
                        phase="error",
                        message=(
                            "还没配置 AI：点右上角 ⚙️ 填 Key 并保存，"
                            "或让管理员在服务器保存全局 Key。"
                        ),
                    )
                return

            tree = _cat_tree_paths()
            if not tree:
                with _AUTOCAT_LOCK:
                    _AUTOCAT.update(
                        phase="error",
                        message="分类树读取失败，稍后再试一次。",
                    )
                return

            for bstart in range(0, n, BATCH):
                batch = [str(p) for p in pids[bstart:bstart + BATCH]]

                # 1) 取料（标题/卖点/简介，AI 优化结果优先）
                items: list = []
                for pid in batch:
                    try:
                        data = src_api(
                            "/prod_list", _prod_auth() | {"pid": pid},
                            timeout=30,
                        )
                        product = (
                            (data.get("product") or {})
                            if data.get("ok") else {}
                        )
                        if not product:
                            err += 1
                            continue
                        cur = str(product.get("cat") or "").strip()
                        opt = product.get("opt") or {}
                        r0 = (product.get("raw") or [{}])[0] or {}
                        rd = r0.get("raw_data") or {}
                        title = str(
                            opt.get("title") or rd.get("标题(必填)")
                            or product.get("title") or ""
                        )[:200]
                        bullets = "；".join(
                            str(b) for b in (opt.get("bullets") or [])[:3]
                        )[:200] or "；".join(
                            str(rd.get(f"要点{k}") or "")
                            for k in (1, 2, 3)
                        )[:200]
                        desc = str(
                            opt.get("description") or rd.get("简介") or ""
                        )[:160]
                        if not title:
                            miss += 1
                            continue
                        items.append({
                            "pid": pid, "cur": cur, "title": title,
                            "bullets": bullets, "desc": desc,
                        })
                    except Exception:
                        err += 1

                if items:
                    # 2) 这一批合一次问（分类树带一遍）
                    cand = list(dict.fromkeys(
                        tree + [it["cur"] for it in items if it["cur"]]
                    ))
                    lines = "\n".join(
                        f"{k}. 标题：{it['title']}"
                        f"｜卖点：{it['bullets']}｜简介：{it['desc']}"
                        for k, it in enumerate(items, 1)
                    )
                    prompt = (
                        "你是亚马逊运营助理，负责给产品挑分类。\n"
                        "我们的分类树，每行一个完整路径（只能从中选）：\n"
                        + "\n".join("- " + c for c in cand[:300]) + "\n\n"
                        "产品列表：\n" + lines + "\n\n"
                        "给每个产品从树里选最合适的一个分类。"
                        "只能选树里已有的（原样返回，不要改字、"
                        "不要造新分类、不要输出列表外的内容），"
                        "实在没有合适的选「其他」。\n"
                        '只输出 JSON：{"items":[{"i":编号,'
                        '"cat":"路径"},…]}，每个产品一条，'
                        "i=产品编号。\n"
                    )
                    pick: dict[int, str] = {}
                    try:
                        out = _ai_chat_one(
                            prompt, key, provider, model,
                            timeout=60, max_tokens=800,
                        )
                        try:
                            doc = json.loads(out)
                        except Exception:
                            s = out.find("{")
                            e = out.rfind("}")
                            doc = (
                                json.loads(out[s:e + 1])
                                if 0 <= s < e else {}
                            )
                        if isinstance(doc, list):
                            doc = {"items": doc}
                        for d in (doc.get("items") or []):
                            if not isinstance(d, dict):
                                continue
                            try:
                                i = int(d.get("i") or 0)
                            except (TypeError, ValueError):
                                continue
                            if 1 <= i <= len(items):
                                pick[i - 1] = str(
                                    d.get("cat") or ""
                                ).strip()
                    except Exception:
                        err += len(items)

                    # 3) 逐个校验：答案必须在树里，否则丢弃不动
                    for k, it in enumerate(items):
                        cat = pick.get(k, "")
                        if cat not in cand:
                            miss += 1
                            continue
                        if cat != it["cur"]:
                            updates.append(
                                {"pid": it["pid"], "cat": cat}
                            )
                        ok += 1

                with _AUTOCAT_LOCK:
                    _AUTOCAT["done"] = min(bstart + BATCH, n)
                    _AUTOCAT["message"] = (
                        f"AI 归类中… {_AUTOCAT['done']} / {n}"
                    )

            # 批量写回（和「🏷 移动分类」同一条通道）
            for start in range(0, len(updates), 20):
                src_api(
                    "/prod_update",
                    _prod_auth()
                    | {"updates": updates[start:start + 20]},
                    timeout=40,
                )
            if updates:
                _index_apply_updates(updates)
                _index_heal()

            with _AUTOCAT_LOCK:
                _AUTOCAT["done"] = n
                _AUTOCAT.update(
                    phase="done", ok=ok, miss=miss, err=err,
                    message=(
                        f"AI 归类完成：分好 {ok}"
                        + (f" · 没找到合适分类 {miss}" if miss else "")
                        + (f" · 失败 {err}" if err else "")
                        + f"；改了 {len(updates)} 个产品的分类"
                    ),
                )

        except Exception as exc:
            with _AUTOCAT_LOCK:
                _AUTOCAT.update(
                    phase="error", message=f"AI 归类出错：{exc}",
                )

    threading.Thread(target=work, daemon=True).start()


def _bt_norm(btmap: list | None) -> list:
    """btmap 词典 → [(cat, node)] 干净元组表。"""
    return [
        (str(m.get("cat") or ""), str(m.get("node") or ""))
        for m in (btmap or []) if m.get("cat") and m.get("node")
    ]


def _bt_node_of(cat: str, bt: list) -> str:
    """产品分类 → 亚马逊类目节点：精确命中优先，其次最长父级
    前缀（选了「汽车配件」的映射，方向盘套也跟着用）。
    没命中回 ""（=不上传时带不带节点由调用方决定）。"""
    if not bt:
        return ""
    cat = str(cat or "").strip()
    if not cat:
        return ""
    for c, n in bt:
        if c == cat:
            return n
    best, bn = 0, ""
    for c, n in bt:
        if cat.startswith(c + "/") and len(c) > best:
            best, bn = len(c), n
    return bn


def _amz_build_messages(
    products: list, mid: str, lang: str,
    variant_mode: str, product_type: str, words: list | None = None,
    eans: dict | None = None, btmap: list | None = None,
) -> tuple[list, list, dict]:
    """返回 (messages, notes)。内容来源：AI 优化结果优先，没有就
    原始资料。图片只收 https（亚马逊要公网可下载）。变体：>1 行且
    行行有颜色 → 父子变体（颜色主题），否则各行独立单品。
    words=侵权词对照表：上传内容里自动替换（只改这次提交的 feed，
    不动已存资料——和智赢刊登时替换一个道理）。
    eans={产品#SKU: EAN}：子体/独立单品自动带条码（智赢式补码，
    池子在服务器上，产品↔码永久绑定）。
    btmap=分类→亚马逊类目节点：产品分类精确匹配（其次父级前缀）
    命中时，子体/独立单品带 browse_classification，放进亚马逊
    对应分类（父体不带）。
    返回 (messages, notes, pid_skus)：pid_skus={pid: [进 feed 的 SKU…]}
    ——上传成功后按它记「已上传」（全部 SKU 成功才算传过）。"""

    pairs = words or []
    hits = 0
    pid_skus: dict[str, list] = {}
    bt = [
        (str(m.get("cat") or ""), str(m.get("node") or ""))
        for m in (btmap or []) if m.get("cat") and m.get("node")
    ]

    def node_of(p) -> str:
        return _bt_node_of(str(p.get("cat") or ""), bt)

    placed_nodes = 0

    def txt(v, cap):
        return [{
            "value": str(v or "").strip()[:cap],
            "language_tag": lang,
            "marketplace_id": mid,
        }]

    def img_attrs(imgs):
        out = {}
        for i, u in enumerate(imgs[:9]):
            key = (
                "main_product_image_locator" if i == 0
                else f"other_image_locator_{i}"
            )
            out[key] = [{"media_location": u}]
        return out

    def content_of(p, r):
        nonlocal hits
        opt = p.get("opt") or {}
        rd = r.get("raw_data") or {}
        title = str(
            opt.get("title") or rd.get("标题(必填)") or r.get("title")
            or p.get("title") or "",
        ).strip()
        bullets = [
            str(b).strip() for b in (opt.get("bullets") or [])
            if str(b or "").strip()
        ] or [
            str(b).strip() for b in (r.get("bullets") or [])
            if str(b or "").strip()
        ]
        desc = str(
            opt.get("description") or rd.get("简介")
            or r.get("description") or "",
        ).strip()
        if pairs:
            title, k1 = _apply_words(title, pairs)
            bullets, ks = zip(*(
                _apply_words(b, pairs) for b in bullets
            )) if bullets else ((), ())
            desc, k2 = _apply_words(desc, pairs)
            hits += k1 + k2 + sum(ks)
        return title, list(bullets)[:5], desc

    messages: list = []
    notes: list = []
    msg = lambda: len(messages) + 1     # noqa: E731

    for p in products:
        rows = p.get("raw") or []
        label = str(p.get("sku") or p.get("pid") or "")

        main = str(p.get("img") or "").strip()
        imgs = [main] if main.startswith("https://") else []
        for r in rows:
            for u in (
                (r.get("image_urls") or []) + (r.get("detail_image_urls") or [])
            ):
                u = str(u or "").strip()
                if u.startswith("https://") and u not in imgs:
                    imgs.append(u)
        if not imgs:
            notes.append(f"{label}：没有 https 公网图片，跳过（先在图片集补图）")
            continue

        colors = [
            str((r.get("raw_data") or {}).get("颜色") or "").strip()
            for r in rows
        ]
        use_variant = len(rows) > 1 and all(colors) and variant_mode != "single"
        if variant_mode == "variant" and not use_variant:
            notes.append(
                f"{label}：不是每行都有颜色，合成不了变体，改为各自独立上传"
            )
            use_variant = False

        if use_variant:
            parent_sku = str(
                p.get("sku")
                or (rows[0].get("raw_data") or {}).get("父SKU(必填)")
                or "",
            ).strip()
            key_pid = str(p.get("pid") or p.get("sku") or "")
            parent_title, _, _ = content_of(p, rows[0])
            messages.append({
                "messageId": msg(),
                "sku": parent_sku,
                "operationType": "UPDATE",
                "productType": product_type,
                "requirements": "LISTING",
                "attributes": {
                    "condition_type": [{"value": "new_new"}],
                    "item_name": txt(parent_title or parent_sku, 200),
                },
            })
            if key_pid and parent_sku:
                pid_skus.setdefault(key_pid, []).append(parent_sku)
            for i, (r, color) in enumerate(zip(rows, colors)):
                sku = str((r.get("raw_data") or {}).get("SKU") or "").strip()
                if not sku:
                    notes.append(f"{parent_sku}：第 {i + 1} 行没有 SKU，跳过")
                    continue
                title, bullets, desc = content_of(p, r)
                attrs = {
                    "condition_type": [{"value": "new_new"}],
                    "parent_sku": [{"value": parent_sku}],
                    "color": txt(color, 100),
                    "item_name": txt(title or sku, 200),
                }
                if bullets:
                    attrs["bullet_point"] = [
                        txt(b, 500)[0] for b in bullets
                    ]
                if desc:
                    attrs["product_description"] = txt(desc, 2000)
                attrs.update(img_attrs(imgs))
                ean = str(
                    (eans or {}).get(f"{p.get('pid')}#{sku}") or ""
                )
                if ean:
                    attrs["standard_product_id"] = [
                        {"value": ean, "type": "EAN"}
                    ]
                node = node_of(p)
                if node:
                    attrs["browse_classification"] = [{
                        "node_id": int(node) if node.isdigit() else node,
                        "marketplace_id": mid,
                    }]
                    placed_nodes += 1
                messages.append({
                    "messageId": msg(),
                    "sku": sku,
                    "operationType": "UPDATE",
                    "productType": product_type,
                    "requirements": "LISTING",
                    "attributes": attrs,
                })
                if key_pid and sku:
                    pid_skus.setdefault(key_pid, []).append(sku)
        else:
            for i, r in enumerate(rows):
                rd = r.get("raw_data") or {}
                sku = str(rd.get("SKU") or "").strip()
                if not sku:
                    notes.append(f"{label}：第 {i + 1} 行没有 SKU，跳过")
                    continue
                title, bullets, desc = content_of(p, r)
                attrs = {
                    "condition_type": [{"value": "new_new"}],
                    "item_name": txt(title or sku, 200),
                }
                if bullets:
                    attrs["bullet_point"] = [txt(b, 500)[0] for b in bullets]
                if desc:
                    attrs["product_description"] = txt(desc, 2000)
                attrs.update(img_attrs(imgs))
                ean = str(
                    (eans or {}).get(f"{p.get('pid')}#{sku}") or ""
                )
                if ean:
                    attrs["standard_product_id"] = [
                        {"value": ean, "type": "EAN"}
                    ]
                node = node_of(p)
                if node:
                    attrs["browse_classification"] = [{
                        "node_id": int(node) if node.isdigit() else node,
                        "marketplace_id": mid,
                    }]
                    placed_nodes += 1
                messages.append({
                    "messageId": msg(),
                    "sku": sku,
                    "operationType": "UPDATE",
                    "productType": product_type,
                    "requirements": "LISTING",
                    "attributes": attrs,
                })
                kpid = str(p.get("pid") or p.get("sku") or "")
                if kpid and sku:
                    pid_skus.setdefault(kpid, []).append(sku)

    if placed_nodes:
        notes.append(
            f"🗂 已按分类给 {placed_nodes} 条带亚马逊类目节点"
            "（放进对应分类货架）"
        )

    return messages, notes, pid_skus


# ---- 上传管道：文档 → feed → 轮询 → 结果 ----

def _amz_submit_feed(payload: dict, mid: str) -> str:
    token = amz_token()
    st, doc = _amz_api(
        "POST", "/feeds/2021-06-30/documents", token,
        {"contentType": "application/json; charset=UTF-8"},
    )
    doc_id = str((doc or {}).get("feedDocumentId") or "")
    url = str((doc or {}).get("url") or "")
    if st != 200 or not (doc_id and url):
        raise RuntimeError(
            f"创建上传文档失败（{st}）：{str(doc.get('raw') or doc)[:300]}"
        )

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = requests.put(
        url, data=body,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        timeout=90,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"传上传内容失败（{r.status_code}）：{r.text[:200]}"
        )

    st, feed = _amz_api(
        "POST", "/feeds/2021-06-30/feeds", token,
        {
            "feedType": "JSON_LISTINGS_FEED",
            "marketplaceIds": [mid],
            "inputFeedDocumentId": doc_id,
        },
    )
    feed_id = str((feed or {}).get("feedId") or "")
    if st != 200 or not feed_id:
        raise RuntimeError(
            f"创建上传任务失败（{st}）：{str(feed.get('raw') or feed)[:300]}"
        )
    return feed_id


def _amz_wait_feed(feed_id: str) -> dict:
    token = amz_token()
    deadline = time.time() + 15 * 60

    while time.time() < deadline:
        st, feed = _amz_api("GET", f"/feeds/2021-06-30/feeds/{feed_id}", token)
        status = str((feed or {}).get("processingStatus") or "") if st == 200 else ""

        if status:
            with _AMZ_LOCK:
                _AMZ_TASK["message"] = f"亚马逊处理中（{status}）…"

        if status == "DONE":
            rid = str(feed.get("resultFeedDocumentId") or "")
            if not rid:
                return {"header": {}, "messages": [], "issues": []}
            st, doc = _amz_api(
                "GET", f"/feeds/2021-06-30/documents/{rid}", token,
            )
            url = str((doc or {}).get("url") or "")
            if not url:
                raise RuntimeError("拿结果文档失败：" + str(doc)[:200])
            rr = requests.get(url, timeout=60)
            try:
                return rr.json()
            except Exception:
                raise RuntimeError("结果解析失败：" + rr.text[:200])

        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(
                f"亚马逊终止了这次上传（{status}），稍后再试一次"
            )

        time.sleep(6)

    raise RuntimeError("等亚马逊结果超时（15 分钟），稍后重传一次")


def _amz_parse_result(result: dict) -> dict:
    skus = []
    for m in result.get("messages") or []:
        skus.append({
            "sku": str(m.get("sku") or ""),
            "status": str(m.get("status") or ""),
            "issues": [
                {
                    "code": str(i.get("code") or ""),
                    "message": str(i.get("message") or "")[:500],
                }
                for i in (m.get("issues") or [])
            ],
        })
    n_ok = sum(1 for s in skus if s["status"].lower() == "success")
    return {
        "total": len(skus),
        "success": n_ok,
        "error": len(skus) - n_ok,
        "skus": skus,
        "feed_issues": [
            {
                "code": str(i.get("code") or ""),
                "message": str(i.get("message") or "")[:500],
            }
            for i in (result.get("issues") or [])
        ],
    }


def _amz_log_add(entry: dict) -> None:
    logs = _load_json_file(_AMZ_UPLOADS_PATH, [])
    if not isinstance(logs, list):
        logs = []
    logs.insert(0, entry)
    _save_json_file(_AMZ_UPLOADS_PATH, logs[:20])


def _amz_start_upload(
    pids: list, variant_mode: str, product_type: str,
    allow_unmapped: bool = True,
) -> None:
    def work():
        try:
            with _AMZ_LOCK:
                _AMZ_TASK.update(
                    phase="building", message="读取产品资料…",
                    total=len(pids), pids=list(pids),
                    feed_id="", result=None,
                    started_at=int(time.time()),
                )

            products = []
            for i, pid in enumerate(pids):
                try:
                    data = src_api(
                        "/prod_list", _prod_auth() | {"pid": pid}, timeout=30,
                    )
                except Exception:
                    continue
                product = (data.get("product") or {}) if data.get("ok") else {}
                if product.get("raw"):
                    products.append(product)
                with _AMZ_LOCK:
                    _AMZ_TASK["message"] = (
                        f"读取产品资料… {i + 1} / {len(pids)}"
                    )

            if not products:
                raise RuntimeError(
                    "选中的产品都没有完整资料（原始行），先重新导入补全"
                )

            c = _amz_cfg()
            mid = (
                _AMZ_RUN.get("mid")
                or str(c.get("marketplace") or "ATVPDKIKX0DER")
            )
            info = _MARKETPLACES.get(mid, ("na", "en_US", ""))

            words = _dict_words()   # 侵权词对照（读不到=空，不挡上传）
            btmap = _dict_btmap()   # 分类→亚马逊类目节点（同上不挡）

            # D1.7.2 防错分类封号：分类没配亚马逊节点的产品默认
            # 拦下不传（不让亚马逊瞎猜分类）。上传弹窗可显式放开。
            blocked_note = ""
            if not allow_unmapped:
                bt = _bt_norm(btmap)
                keep: list = []
                dropped: list = []
                for p in products:
                    (
                        keep if _bt_node_of(
                            str(p.get("cat") or ""), bt
                        ) else dropped
                    ).append(p)
                if dropped:
                    cats = sorted({
                        str(p.get("cat") or "").strip() or "（未分类）"
                        for p in dropped
                    })
                    blocked_note = (
                        f"⛔ 已拦下 {len(dropped)} 个产品没传：它们的分类"
                        "没配「亚马逊分类节点」，不让亚马逊自动猜分类"
                        "（防放错分类封号）。去「🛡 侵权词库」页第三张表"
                        f"配好再传：{'、'.join(cats[:8])}"
                    )
                    products = keep
                    if not products:
                        raise RuntimeError(blocked_note)

            # EAN 补码（智赢式：池子在服务器，产品↔码永久绑定）。
            # 只给「有 https 图、行行有 SKU」的算，免得浪费码池。
            ekeys: list = []
            for p in products:
                has_https = str(p.get("img") or "").startswith("https://")
                if not has_https:
                    for r in p.get("raw") or []:
                        for u in (r.get("image_urls") or []) + (
                            r.get("detail_image_urls") or []
                        ):
                            if str(u or "").strip().startswith("https://"):
                                has_https = True
                                break
                        if has_https:
                            break
                if not has_https:
                    continue
                pid = str(p.get("pid") or p.get("sku") or "")
                for r in p.get("raw") or []:
                    sku = str(
                        (r.get("raw_data") or {}).get("SKU") or ""
                    ).strip()
                    k = f"{pid}#{sku}"
                    if sku and k not in ekeys:
                        ekeys.append(k)
            eans, ean_err, ean_prefix = (
                _amz_ean_assign(ekeys) if ekeys else ({}, "", "")
            )
            messages, notes, pid_skus = _amz_build_messages(
                products, mid, info[1], variant_mode, product_type,
                words=words, eans=eans, btmap=btmap,
            )
            if ean_err:
                notes.append(
                    "⚠️ EAN 补码服务没连上，这次上传不带条码：" + ean_err
                )
            elif eans:
                notes.append(
                    f"🏷 已自动补 {len(eans)} 个 EAN 条码"
                    + (f"（前缀 {ean_prefix}…）" if ean_prefix else "")
                )
            if blocked_note:
                notes.append(blocked_note)
            if not messages:
                raise RuntimeError(
                    "没有可上传的内容：" + "；".join(notes)[:300]
                )

            payload = {
                "header": {
                    "sellerId": (
                        _AMZ_RUN.get("seller_id")
                        or str(c.get("seller_id") or "")
                    ),
                    "version": "2.0",
                    "issueLocale": "en_US",
                },
                "messages": messages,
            }

            with _AMZ_LOCK:
                _AMZ_TASK.update(
                    phase="uploading",
                    message=f"提交 {len(messages)} 条到亚马逊…",
                )

            feed_id = _amz_submit_feed(payload, mid)

            with _AMZ_LOCK:
                _AMZ_TASK.update(
                    phase="polling", feed_id=feed_id,
                    message="亚马逊处理中…",
                )

            result = _amz_wait_feed(feed_id)
            summary = _amz_parse_result(result)
            summary["notes"] = notes
            summary["feed_id"] = feed_id

            # D1.7.0 智赢式记账：产品进 feed 的 SKU 全部成功 → 给
            # 这家店记一笔「已上传」（下次按分类上传自动跳过）。
            store_rec = _AMZ_RUN.get("store") or {}
            if store_rec.get("id") and pid_skus:
                ok_skus = {
                    s["sku"] for s in summary.get("skus") or []
                    if str(s.get("status") or "").lower() == "success"
                }
                bad_skus = {
                    s["sku"] for s in summary.get("skus") or []
                    if str(s.get("status") or "").lower() != "success"
                }
                done_pids = [
                    pid for pid, skus in pid_skus.items()
                    if skus
                    and not (set(skus) & bad_skus)
                    and set(skus) <= ok_skus
                ]
                if done_pids:
                    try:
                        r = _amz_store_api(
                            {
                                "action": "up_add",
                                "id": str(store_rec["id"]),
                                "pids": done_pids,
                            },
                            timeout=30,
                        )
                        if r.get("ok"):
                            summary["notes"].append(
                                f"🔖 {len(done_pids)} 个产品已记为已上传"
                                "（下次按分类上传自动跳过）"
                            )
                            # 已传清单立刻刷新（不等 30 秒缓存）
                            _amz_uploaded_map(str(store_rec["id"]), force=True)
                        else:
                            summary["notes"].append(
                                "⚠️ 记「已上传」失败（不影响本次结果）："
                                + str(r.get("error") or "")[:120]
                            )
                    except Exception:
                        pass   # 记账失败不影响上传结果

            msg = (
                f"上传完成：成功 {summary['success']} / "
                f"失败 {summary['error']}"
                + ("（失败原因见下表）" if summary["error"] else "")
            )
            with _AMZ_LOCK:
                _AMZ_TASK.update(phase="done", message=msg, result=summary)
            _amz_log_add({
                "at": int(time.time() * 1000),
                "message": msg,
                "success": summary["success"],
                "error": summary["error"],
                "feed_id": feed_id,
            })

        except Exception as exc:
            with _AMZ_LOCK:
                _AMZ_TASK.update(
                    phase="error", message=f"上传失败：{exc}",
                )
            _amz_log_add({
                "at": int(time.time() * 1000),
                "message": f"上传失败：{exc}",
                "success": 0, "error": 0, "feed_id": "",
            })

    threading.Thread(target=work, daemon=True).start()


# =====================================================
# HTTP 服务
# =====================================================

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 基础 ----

    def log_message(self, fmt, *args):  # 静默
        pass

    def _send(
        self, code: int, body: bytes,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        )

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)

            if n <= 0:
                return {}

            return json.loads(
                self.rfile.read(n).decode("utf-8")
            )
        except Exception:
            return {}

    # ---- 静态 ----

    def _static(self, name: str) -> None:
        path = APP_DIR / APP_DIR_NAME / name

        try:
            data = path.read_bytes()
        except Exception:
            self._json({"ok": False, "error": "not found"}, 404)
            return

        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }.get(path.suffix.lower(), "application/octet-stream")

        self._send(200, data, ctype)

    # ---- 路由 ----

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = parse_qs(url.query)

        try:
            if url.path in ("/", "/index.html"):
                self._static("index.html")
                return

            if url.path in ("/app.js", "/style.css"):
                self._static(url.path.lstrip("/"))
                return

            if url.path == "/api/health":
                self._json({
                    "ok": True, "version": VERSION,
                    "proxy_mode": PROXY_MODE,
                    "uptime": round(time.time() - _STARTED_AT),
                })
                return

            if url.path == "/api/session":
                self._json({
                    "ok": True,
                    "logged_in": bool(_logged_user()),
                    "profile": _profile(),
                    "version": VERSION,
                    "proxy_mode": PROXY_MODE,
                    "server": CONFIG["server"],
                })
                return

            if url.path == "/api/products":
                if not _logged_user():
                    self._json({"ok": False, "error": "未登录"}, 401)
                    return

                scope = (q.get("scope") or [""])[0]

                if scope == "all" and not is_admin():
                    scope = ""

                force = (q.get("force") or ["0"])[0] in ("1", "true")

                try:
                    data = fetch_index(scope, force=force)
                    self._json({
                        "ok": True,
                        "items": data.get("items") or [],
                        "cats": data.get("cats") or [],
                        "owners": data.get("owners") or [],
                        "fetched_at": data.get("_fetched_at") or 0,
                    })
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                return

            if url.path == "/api/product":
                if not _logged_user():
                    self._json({"ok": False, "error": "未登录"}, 401)
                    return

                pid = (q.get("pid") or [""])[0]

                if not pid:
                    self._json({"ok": False, "error": "缺 pid"}, 400)
                    return

                payload = _prod_auth() | {"pid": pid}
                owner = (q.get("owner") or [""])[0]

                if owner:
                    payload["owner"] = owner

                data = src_api("/prod_list", payload)
                self._json(data)
                return

            # 分类库（D1.3.0 智赢式分类树）：全局共享文档，
            # 首次读取自动预置智赢同款 8 顶级分类。
            if url.path == "/api/cats":
                if not _logged_user():
                    self._json({"ok": False, "error": "未登录"}, 401)
                    return

                resp = src_api(
                    "/cat_lib", _prod_auth() | {"action": "get"},
                    timeout=30,
                )
                self._json(resp)
                return

            if url.path == "/api/optimize/status":
                self._json({"ok": True, "task": _poll_task()})
                return

            # D1.8.0 AI 自动归类进度（前端 1.8s 轮询）
            if url.path == "/api/products/autocat/status":
                with _AUTOCAT_LOCK:
                    task = dict(_AUTOCAT)
                self._json({"ok": True, "task": task})
                return

            if url.path == "/api/ai":
                key, provider, model = resolve_ai()
                self._json({
                    "ok": True,
                    "provider": provider,
                    "model": model,
                    "key_len": len(key),
                    "source": "本机配置" if str(
                        (CONFIG.get("ai") or {}).get("key") or ""
                    ).strip() else "服务器全局",
                })
                return

            if url.path == "/api/image/proxy":
                # 图片代理：浏览器直连加载不了的图（跨域编辑要读像素、
                # 个别域名被卡）走本机 Python 拉一遍再吐给页面。
                target = (q.get("u") or [""])[0].strip()
                parsed = urlparse(target)
                host = (parsed.hostname or "").lower()

                if parsed.scheme not in ("http", "https") or not host:
                    self._json({"ok": False, "error": "网址不对"}, 400)
                    return

                if (
                    host in ("localhost", "::1")
                    or host.startswith(
                        ("127.", "192.168.", "10.", "169.254.", "0.")
                    )
                ):
                    self._json({"ok": False, "error": "不允许的地址"}, 403)
                    return

                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                    "Referer": parsed.scheme + "://" + host + "/",
                }

                def _pull() -> requests.Response:
                    return requests.get(
                        target, timeout=25, headers=headers
                    )

                try:
                    try:
                        resp = _pull()
                    except Exception:
                        # 直连失败 → 本地代理再试一次（PROXY_MODE=proxy
                        # 时环境变量已带代理，这里不会重复）
                        if (
                            PROXY_MODE == "direct"
                            and CONFIG.get("proxy")
                        ):
                            resp = requests.get(
                                target,
                                timeout=25,
                                headers=headers,
                                proxies={
                                    "http": CONFIG["proxy"],
                                    "https": CONFIG["proxy"],
                                },
                            )
                        else:
                            raise

                    ctype = str(
                        resp.headers.get("Content-Type") or ""
                    ).split(";")[0].strip().lower()

                    if resp.status_code != 200 or not ctype.startswith(
                        "image/"
                    ):
                        self._json(
                            {"ok": False, "error": "取不到图片"}, 502
                        )
                        return

                    if len(resp.content) > 8 * 1024 * 1024:
                        self._json(
                            {"ok": False, "error": "图片超过 8MB"}, 413
                        )
                        return

                    self._send(200, resp.content, ctype)
                except Exception as exc:
                    self._json(
                        {"ok": False, "error": f"取图失败：{exc}"}, 502
                    )
                return

            # ---- 亚马逊上传（D1.4.1）：店铺清单 / 任务进度（不回显密钥） ----
            if url.path == "/api/amazon/status":
                c = _amz_cfg()
                configured = all(
                    str(c.get(k) or "").strip()
                    for k in ("client_id", "client_secret",
                              "refresh_token", "seller_id")
                )
                stores, srv_err = _amz_stores()
                active = _amz_active_store(stores)
                uploaded = (
                    _amz_uploaded_map(str(active.get("id") or ""))
                    if active else {}
                )
                app = _amz_app_info()
                try:
                    ean = _ean_api({"action": "status"}, timeout=15)
                except Exception:
                    ean = {}
                mk = (
                    str(active.get("marketplace") or "")
                    if active else ""
                ) or str(c.get("marketplace") or "ATVPDKIKX0DER")
                info = _MARKETPLACES.get(mk)
                self._json({
                    "ok": True,
                    "configured": bool(stores) or configured,
                    "stores": stores,
                    "active_store": active.get("id") if active else "",
                    "srv_err": srv_err,
                    "marketplace": mk,
                    "marketplace_name": info[2] if info else mk,
                    "language_tag": info[1] if info else "en_US",
                    "product_type": str(c.get("product_type") or "PRODUCT"),
                    "seller_id": (
                        str(active.get("seller_id") or "")
                        if active else str(c.get("seller_id") or "")
                    ),
                    "is_admin": is_admin(),
                    "app_set": bool(app.get("set")),
                    "app_err": app.get("err") or "",
                    "uploaded": uploaded,
                    "ean": ean if ean.get("ok") else None,
                    "markets": [
                        [i, f"{v[2]}（{v[0].upper()}）"]
                        for i, v in _MARKETPLACES.items()
                    ],
                    "task": _amz_task_snap(),
                    "history": _load_json_file(_AMZ_UPLOADS_PATH, [])[:6]
                    if isinstance(
                        _load_json_file(_AMZ_UPLOADS_PATH, []), list
                    ) else [],
                })
                return

            if url.path == "/api/amazon/upload/status":
                self._json({"ok": True, "task": _amz_task_snap()})
                return

            self._json({"ok": False, "error": "not found"}, 404)

        except BrokenPipeError:
            pass

        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        body = self._body()

        try:
            if url.path == "/api/login":
                user = str(body.get("user") or "").strip()
                password = str(body.get("pass") or "")

                if not user or not password:
                    self._json(
                        {"ok": False, "error": "请输入账号和密码"}, 400
                    )
                    return

                data = src_api(
                    "/login",
                    {"user": user, "pass": password, "kind": "web"},
                    timeout=15,
                )

                if not data.get("ok"):
                    self._json({
                        "ok": False,
                        "error": str(
                            data.get("error") or "账号或密码不对"
                        ),
                    }, 401)
                    return

                SESSION.clear()
                SESSION.update({
                    "user": str(data.get("user") or user),
                    "token": str(data.get("token") or ""),
                    "dept": str(data.get("dept") or ""),
                    "head": bool(data.get("head")),
                    "src": bool(data.get("src")),
                    "exp_lib": bool(data.get("exp_lib", True)),
                    "admin": bool(data.get("admin")),
                    "saved_at": int(time.time()),
                })
                _save_json_file(SESSION_PATH, SESSION)
                _INDEX_MEM.clear()

                self._json({"ok": True, "profile": _profile()})
                return

            if url.path == "/api/logout":
                SESSION.clear()

                try:
                    SESSION_PATH.unlink()
                except Exception:
                    pass

                _INDEX_MEM.clear()
                self._json({"ok": True})
                return

            # ---- 下面全部要登录 ----

            if not _logged_user():
                self._json({"ok": False, "error": "未登录"}, 401)
                return

            if url.path == "/api/cats":
                # 保存分类树（新建/改名/删除/恢复——前端算好整棵树传上来）
                lib_in = body.get("lib")

                if not isinstance(lib_in, dict):
                    self._json(
                        {"ok": False, "error": "分类数据不对"}, 400
                    )
                    return

                resp = src_api(
                    "/cat_lib",
                    _prod_auth() | {"action": "save", "lib": lib_in},
                    timeout=30,
                )
                self._json(resp)
                return

            if url.path == "/api/dict":
                # 刊登词典（D1.5.0 智赢式）：kind=words 侵权词对照表 /
                # kind=ptmap 分类→商品类型映射。带 doc=保存，不带=读取。
                kind = str(body.get("kind") or "")
                doc_in = body.get("doc")

                if kind not in ("words", "ptmap", "btmap") or (
                    doc_in is not None and not isinstance(doc_in, dict)
                ):
                    self._json({"ok": False, "error": "参数不对"}, 400)
                    return

                payload = _prod_auth() | {"kind": kind}
                if doc_in is None:
                    payload["action"] = "get"
                else:
                    payload["action"] = "save"
                    payload["doc"] = doc_in

                resp = src_api("/dict", payload, timeout=30)
                self._json(resp)
                return

            if url.path == "/api/products/refresh":
                scope = str(body.get("scope") or "")

                if scope == "all" and not is_admin():
                    scope = ""

                try:
                    data = fetch_index(scope, force=True)
                    self._json({
                        "ok": True,
                        "items": data.get("items") or [],
                        "cats": data.get("cats") or [],
                        "owners": data.get("owners") or [],
                    })
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                return

            if url.path == "/api/products/update":
                updates = body.get("updates") or []

                if not isinstance(updates, list) or not updates:
                    self._json(
                        {"ok": False, "error": "没有要更新的内容"}, 400
                    )
                    return

                for start in range(0, len(updates), 20):
                    resp = src_api(
                        "/prod_update",
                        _prod_auth()
                        | {"updates": updates[start:start + 20]},
                        timeout=40,
                    )

                    if not resp.get("ok"):
                        self._json({
                            "ok": False,
                            "error": str(
                                resp.get("error") or "保存失败"
                            ),
                        }, 502)
                        return

                _index_apply_updates(updates)
                _index_heal()
                self._json({"ok": True})
                return

            # D1.8.0 AI 自动归类：AI 从自己的分类树里挑分类（像 AI
            # 优化一样一键跑后台）。只挑已有的，挑不中=不动。
            if url.path == "/api/products/autocat":
                pids = [
                    str(p) for p in body.get("pids") or [] if str(p).strip()
                ]

                if not pids:
                    self._json(
                        {"ok": False, "error": "请先勾选产品"}, 400
                    )
                    return

                with _AUTOCAT_LOCK:
                    if _AUTOCAT.get("phase") == "running":
                        self._json({
                            "ok": False,
                            "error": "AI 归类正在跑，等它结束",
                        }, 409)
                        return

                _autocat_launch(pids)
                self._json({"ok": True, "count": len(pids)})
                return

            if url.path == "/api/products/delete":
                pids = [str(p) for p in body.get("pids") or [] if p]

                if not pids:
                    self._json(
                        {"ok": False, "error": "没有选中产品"}, 400
                    )
                    return

                done = 0

                for start in range(0, len(pids), 100):
                    chunk = pids[start:start + 100]
                    err = None

                    for _try in range(3):
                        try:
                            resp = src_api(
                                "/prod_del",
                                _prod_auth() | {"pids": chunk},
                                timeout=60,
                            )

                            if resp.get("ok"):
                                err = None
                                break

                            err = str(resp.get("error") or "删除失败")

                            if resp.get("denied"):
                                break

                        except Exception as exc:
                            err = str(exc)

                        time.sleep(1.5)

                    if err:
                        self._json({
                            "ok": False,
                            "error": (
                                f"前 {done} 条已删除；"
                                f"剩下的失败：{err}（稍后再点一次"
                                "会接着删）"
                            ),
                        }, 502)
                        return

                    done += len(chunk)

                _index_apply_delete(pids)
                _index_heal()
                self._json({"ok": True, "deleted": len(pids)})
                return

            if url.path == "/api/import":
                mode = str(body.get("mode") or "")
                records = None
                error = ""

                if mode == "paste":
                    import pandas as pd

                    dataframe, error = _paste_to_dataframe(
                        str(body.get("content") or "")
                    )

                    if dataframe is not None:
                        records = list(
                            build_product_records(
                                dataframe, _TEMPLATE_FIELDS
                            )
                        )

                elif mode == "xlsx":
                    name = str(body.get("name") or "导入.xlsx")
                    raw_b64 = str(body.get("data_b64") or "")

                    try:
                        raw = base64.b64decode(raw_b64)
                        envelope = read_workbook(name, raw)
                        records = list(envelope.records)
                    except Exception as exc:
                        error = f"Excel 解析失败：{exc}"

                else:
                    error = "未知的导入方式"

                if error:
                    self._json({"ok": False, "error": error}, 400)
                    return

                if not records:
                    self._json(
                        {"ok": False, "error": "没识别到产品行"}, 400
                    )
                    return

                products, stats, fat = _group_records(records)

                if not products:
                    self._json(
                        {"ok": False, "error": "没识别到产品行"}, 400
                    )
                    return

                by = _logged_user()[:32] or "desktop"
                added = updated = 0

                for start in range(0, len(products), 20):
                    chunk = products[start:start + 20]
                    resp = src_api(
                        "/prod_upsert",
                        _prod_auth()
                        | {"by": by, "products": chunk},
                        timeout=90,
                    )

                    if not resp.get("ok"):
                        self._json({
                            "ok": False,
                            "error": (
                                f"前 {start} 个已导入；"
                                f"剩下的失败："
                                f"{resp.get('error') or '导入失败'}"
                                "（相同产品重复导入算更新，再点一次"
                                "会接着传）"
                            ),
                        }, 502)
                        return

                    added += int(resp.get("added", 0) or 0)
                    updated += int(resp.get("updated", 0) or 0)

                _index_apply_import(products, by)
                _index_heal()
                self._json({
                    "ok": True,
                    "added": added,
                    "updated": updated,
                    "products": len(products),
                    "rows": stats.get("rows", 0),
                    "fat": len(fat),
                })
                return

            if url.path == "/api/optimize":
                pids = [
                    str(p) for p in body.get("pids") or [] if str(p).strip()
                ]

                if not pids:
                    self._json(
                        {"ok": False, "error": "请先勾选产品"}, 400
                    )
                    return

                with TASK_LOCK:
                    if TASK["phase"] in (
                        "reading", "running", "writing",
                    ):
                        self._json({
                            "ok": False,
                            "error": "已有任务在跑，等它结束",
                        }, 409)
                        return

                    _task_reset()
                    TASK["phase"] = "reading"
                    TASK["pids"] = pids
                    TASK["total"] = len(pids)
                    TASK["message"] = "读取产品资料…"
                    TASK["started_at"] = int(time.time())

                _start_optimize_async(pids)
                self._json({"ok": True, "count": len(pids)})
                return

            if url.path == "/api/optimize/control":
                cmd = str(body.get("cmd") or "")
                task_id = str(TASK.get("task_id") or "")

                if cmd in ("pause", "resume", "cancel") and task_id:
                    save_control(
                        task_id,
                        {
                            "pause": "pause",
                            "resume": "running",
                            "cancel": "cancel",
                        }[cmd],
                    )

                    if cmd == "cancel":
                        with TASK_LOCK:
                            TASK["message"] = "正在取消…"

                    self._json({"ok": True})
                else:
                    self._json(
                        {"ok": False, "error": "当前没有任务"}, 400
                    )
                return

            if url.path == "/api/optimize/writeback":
                task_id = str(TASK.get("task_id") or "")

                if not task_id:
                    self._json(
                        {"ok": False, "error": "没有可写回的任务"}, 400
                    )
                    return

                manifest = load_json(
                    get_task_dir(task_id) / "pl_manifest.json",
                    default=None,
                )

                if not (isinstance(manifest, dict) and manifest.get("pids")):
                    self._json(
                        {"ok": False, "error": "找不到任务清单"}, 400
                    )
                    return

                with TASK_LOCK:
                    TASK["phase"] = "writing"
                    TASK["message"] = "把优化结果写回产品…"

                try:
                    n = _write_back_opt(task_id, manifest)
                    fetch_index("", force=True)

                    with TASK_LOCK:
                        TASK["phase"] = "done"
                        TASK["written_back"] = True
                        TASK["message"] = f"已写回 {n} 个产品"

                    self._json({"ok": True, "n": n})
                except Exception as exc:
                    with TASK_LOCK:
                        TASK["phase"] = "error"
                        TASK["message"] = f"写回失败：{exc}"

                    self._json({"ok": False, "error": str(exc)}, 502)
                return

            if url.path == "/api/image/put":
                # 编辑好的图片 / 本地新图 → 传 Worker 拿公开 HTTPS 链接
                pid = re.sub(
                    r"[^a-z0-9_-]", "", str(body.get("pid") or "edit").lower()
                )[:40] or "edit"
                ct = str(body.get("ct") or "image/jpeg")

                if not ct.startswith("image/"):
                    ct = "image/jpeg"

                data_b64 = str(body.get("data_b64") or "")

                if not data_b64:
                    self._json(
                        {"ok": False, "error": "没有图片数据"}, 400
                    )
                    return

                if len(data_b64) > 4400000:
                    self._json(
                        {"ok": False, "error": "图片太大（超过 3MB）"}, 413
                    )
                    return

                try:
                    base64.b64decode(data_b64)
                except Exception:
                    self._json(
                        {"ok": False, "error": "图片数据无效"}, 400
                    )
                    return

                resp = src_api(
                    "/img_put",
                    _prod_auth()
                    | {"pid": pid, "ct": ct, "data_b64": data_b64},
                    timeout=60,
                )
                self._json(resp)
                return

            if url.path == "/api/product/images":
                # 保存图片集修改：重排 raw 各行的 产品图/简介图 →
                # /prod_upsert 回写（同 pid 合并更新，优化结果/标记不动），
                # 再用 /prod_update 同步主图（清空时也能清掉卡片缩略图）。
                pid = str(body.get("pid") or "").strip()
                imgs_in = body.get("imgs")

                if not pid or not isinstance(imgs_in, list):
                    self._json(
                        {"ok": False, "error": "参数不对"}, 400
                    )
                    return

                final: list[str] = []
                seen: set[str] = set()

                for u in imgs_in[:60]:
                    u = str(u or "").strip()[:500]

                    if u.startswith(("http://", "https://")) and u not in seen:
                        seen.add(u)
                        final.append(u)

                try:
                    data = src_api(
                        "/prod_list", _prod_auth() | {"pid": pid},
                        timeout=30,
                    )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                    return

                rec = (data.get("product") or {}) if data.get("ok") else {}

                if not rec:
                    self._json(
                        {"ok": False, "error": "产品不存在"}, 404
                    )
                    return

                raw_rows = rec.get("raw") or []

                if not raw_rows:
                    self._json({
                        "ok": False,
                        "error": (
                            "这条产品资料不全（没有原始行），改不了图片。"
                            "重新导入同一份 Excel 补全后即可编辑。"
                        ),
                    }, 400)
                    return

                # 逐行保留还在最终清单里的图（行内顺序不变）——导出和
                # 网页版仍能看到每行自己的 产品图/简介图。整份清单的
                # 顺序存在第一行的 image_urls：前端拼图集时逐行去重，
                # 第一行的完整顺序就是显示顺序（拖动排序才存得住）。
                final_set = set(final)

                for row in raw_rows:
                    iu = [
                        u for u in (row.get("image_urls") or [])
                        if str(u) in final_set
                    ]
                    diu = [
                        u for u in (row.get("detail_image_urls") or [])
                        if str(u) in final_set
                    ]
                    row["image_urls"] = iu
                    row["detail_image_urls"] = diu
                    rd = dict(row.get("raw_data") or {})
                    rd["产品图"] = "\n".join(iu)
                    rd["简介图"] = "\n".join(diu)
                    row["raw_data"] = rd

                first = raw_rows[0]
                first["image_urls"] = list(final)
                first["raw_data"] = dict(first.get("raw_data") or {})
                first["raw_data"]["产品图"] = "\n".join(final)

                main = final[0] if final else ""
                product = {
                    "pid": pid,
                    "sku": str(rec.get("sku") or ""),
                    "title": str(rec.get("title") or ""),
                    "model": str(rec.get("model") or ""),
                    "cat": str(rec.get("cat") or ""),
                    "kw": str(rec.get("kw") or ""),
                    "raw": raw_rows,
                }
                # 主图跟 raw 一次原子写完（upsert 不收空值，清空场景
                # 用 img_clear 显式清——绝不再补一发 prod_update 改图，
                # 那个读-改-写会撞 KV 旧副本把新 raw 冲掉）
                if main:
                    product["img"] = main
                else:
                    product["img_clear"] = True

                try:
                    resp = src_api(
                        "/prod_upsert",
                        _prod_auth()
                        | {"by": _logged_user()[:32], "products": [product]},
                        timeout=90,
                    )

                    if not resp.get("ok"):
                        raise RuntimeError(
                            str(resp.get("error") or "保存图片失败")
                        )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                    return

                _index_apply_updates([{"pid": pid, "img": main}])
                _index_heal()
                self._json({"ok": True, "n": len(final)})
                return

            if url.path == "/api/product/opt":
                # 手改 AI 优化结果（标题/短标题/五点/简介）→ /prod_opt_set。
                # 亮点/SEO/首图/任务号不在编辑范围：从旧记录原样透传，
                # 别让整体替换把它们清掉。
                pid = str(body.get("pid") or "").strip()
                o = body.get("opt")

                if not pid or not isinstance(o, dict):
                    self._json({"ok": False, "error": "参数不对"}, 400)
                    return

                title = str(o.get("title") or "").strip()[:600]
                short = str(o.get("short_title") or "").strip()[:300]
                desc = str(o.get("description") or "").strip()[:8000]
                bullets = [
                    str(b).strip()[:600]
                    for b in (o.get("bullets") or [])
                    if str(b or "").strip()
                ][:8]

                if not (title or bullets or desc):
                    self._json(
                        {"ok": False, "error": "标题、五点、简介都空了，至少留一条内容"},
                        400,
                    )
                    return

                try:
                    data = src_api(
                        "/prod_list", _prod_auth() | {"pid": pid}, timeout=30,
                    )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                    return

                rec = (data.get("product") or {}) if data.get("ok") else {}

                if not rec:
                    self._json({"ok": False, "error": "产品不存在"}, 404)
                    return

                old = rec.get("opt") or {}
                opt = {
                    "title": title,
                    "short_title": short,
                    "bullets": bullets,
                    "description": desc,
                    "highlight": [
                        str(h)[:300]
                        for h in (old.get("highlight") or [])[:12]
                    ],
                    "seo": [
                        str(s)[:200]
                        for s in (old.get("seo") or [])[:20]
                    ],
                    "image": str(old.get("image") or "")[:400],
                    "task_id": str(old.get("task_id") or "")[:40],
                }

                try:
                    resp = src_api(
                        "/prod_opt_set",
                        _prod_auth() | {"updates": [{"pid": pid, "opt": opt}]},
                        timeout=40,
                    )

                    if not resp.get("ok"):
                        raise RuntimeError(
                            str(resp.get("error") or "保存失败")
                        )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                    return

                _index_heal()
                self._json({"ok": True})
                return

            if url.path == "/api/product/text":
                # 手改原始资料文字（标题/要点1-5/简介）→ 单次原子
                # /prod_upsert 回写整份 raw。图片字段一行不动地带过、
                # 不传 img → 主图保持不变（和图片编辑共用同一套原子写法）。
                pid = str(body.get("pid") or "").strip()
                rows_in = body.get("rows")

                if not pid or not isinstance(rows_in, list):
                    self._json({"ok": False, "error": "参数不对"}, 400)
                    return

                edits: dict[int, tuple] = {}

                for r in rows_in[:60]:
                    if not isinstance(r, dict):
                        continue

                    try:
                        i = int(r.get("i"))
                    except (TypeError, ValueError):
                        continue

                    if i < 0:
                        continue

                    # 要点按 5 个位置保留（第 2 条空着就空着，别往前挪
                    # ——挪了会和 Excel 的 要点2/要点3 列错位）
                    bullets = [
                        str(b).strip()[:600]
                        for b in (r.get("bullets") or [])[:5]
                    ]

                    while len(bullets) < 5:
                        bullets.append("")

                    edits[i] = (
                        str(r.get("title") or "").strip()[:600],
                        bullets,
                        str(r.get("description") or "").strip()[:8000],
                    )

                if not edits:
                    self._json({"ok": False, "error": "没有要改的内容"}, 400)
                    return

                try:
                    data = src_api(
                        "/prod_list", _prod_auth() | {"pid": pid}, timeout=30,
                    )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                    return

                rec = (data.get("product") or {}) if data.get("ok") else {}

                if not rec:
                    self._json({"ok": False, "error": "产品不存在"}, 404)
                    return

                raw_rows = rec.get("raw") or []

                if not raw_rows:
                    self._json({
                        "ok": False,
                        "error": (
                            "这条产品资料不全（没有原始行），改不了文字。"
                            "重新导入同一份 Excel 补全后即可编辑。"
                        ),
                    }, 400)
                    return

                for i, (t, b, d) in edits.items():
                    if i >= len(raw_rows):
                        continue

                    row = raw_rows[i]
                    rd = dict(row.get("raw_data") or {})
                    rd["标题(必填)"] = t

                    for k in range(5):
                        rd[f"要点{k + 1}"] = b[k] if k < len(b) else ""

                    rd["简介"] = d
                    row["raw_data"] = rd
                    row["title"] = t
                    # 派生字段只留非空要点（引擎的 bullets 元组不要空串）
                    row["bullets"] = [x for x in b if x]
                    row["description"] = d

                # 产品标题跟着第一行非空标题走（导入归组就是这个规则）
                new_title = next(
                    (
                        str(r.get("title") or "").strip()
                        for r in raw_rows
                        if str(r.get("title") or "").strip()
                    ),
                    "",
                )
                product = {
                    "pid": pid,
                    "sku": str(rec.get("sku") or ""),
                    "title": new_title or str(rec.get("title") or ""),
                    "model": str(rec.get("model") or ""),
                    "cat": str(rec.get("cat") or ""),
                    "kw": str(rec.get("kw") or ""),
                    "raw": raw_rows,
                }

                try:
                    resp = src_api(
                        "/prod_upsert",
                        _prod_auth()
                        | {"by": _logged_user()[:32], "products": [product]},
                        timeout=90,
                    )

                    if not resp.get("ok"):
                        raise RuntimeError(
                            str(resp.get("error") or "保存失败")
                        )
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 502)
                    return

                _index_apply_updates([{"pid": pid, "title": product["title"]}])
                _index_heal()
                self._json({"ok": True, "n": len(raw_rows)})
                return

            if url.path == "/api/export":
                pids = [
                    str(p) for p in body.get("pids") or [] if str(p).strip()
                ]

                if not pids:
                    self._json(
                        {"ok": False, "error": "请先勾选产品"}, 400
                    )
                    return

                result = _export_pids(pids)

                if result is None:
                    self._json({
                        "ok": False,
                        "error": (
                            "选中的产品都还没有优化结果"
                            "（先跑一次 🚀 AI 优化）。"
                        ),
                    }, 400)
                    return

                filename = (
                    "我的产品_优化结果_"
                    + datetime.now().strftime("%m%d_%H%M")
                    + ".xlsx"
                )
                self._json({
                    "ok": True,
                    "filename": filename,
                    "data_b64": base64.b64encode(result).decode("ascii"),
                })
                return

            if url.path == "/api/ai":
                ai = dict(CONFIG.get("ai") or {})
                provider = str(body.get("provider") or "").lower()

                if provider in ("openai", "deepseek"):
                    ai["provider"] = provider

                if "key" in body:
                    ai["key"] = str(body.get("key") or "").strip()

                if "model" in body:
                    ai["model"] = str(body.get("model") or "").strip()

                CONFIG["ai"] = ai
                _save_json_file(CONFIG_PATH, CONFIG)
                self._json({"ok": True})
                return

            if url.path == "/api/shutdown":
                self._json({"ok": True})

                threading.Thread(
                    target=self.server.shutdown, daemon=True
                ).start()
                return

            # ---- 亚马逊上传（D1.4.0） ----
            if url.path == "/api/amazon/config":
                # 保存授权配置（空值不覆盖，防止误清）；test=1 顺手自检
                amz = dict(CONFIG.get("amazon") or {})
                for k in ("client_id", "client_secret", "refresh_token",
                          "seller_id", "app_id"):
                    v = str(body.get(k) or "").strip()
                    if v:
                        amz[k] = v
                mk = str(body.get("marketplace") or "").strip()
                if mk in _MARKETPLACES:
                    amz["marketplace"] = mk
                amz["product_type"] = (
                    str(body.get("product_type") or "").strip()[:60]
                    or "PRODUCT"
                )
                CONFIG["amazon"] = amz
                _save_json_file(CONFIG_PATH, CONFIG)
                _AMZ_TOKEN.update(at="", exp=0.0)

                resp = {"ok": True}
                if body.get("test"):
                    try:
                        resp["verify"] = amz_verify()
                    except Exception as exc:
                        resp["verify"] = {"ok": False, "error": str(exc)[:400]}
                self._json(resp)
                return

            if url.path == "/api/amazon/authorize/start":
                # D1.6.0 智赢式授权第 1 步：只要店铺名/卖家ID/站点
                # 三栏（应用凭证在服务器上配一次，全店共用）。
                name = str(body.get("name") or "").strip()[:40]
                seller_id = str(body.get("seller_id") or "").strip()[:40]
                marketplace = str(body.get("marketplace") or "").strip()
                if marketplace not in _MARKETPLACES:
                    marketplace = "ATVPDKIKX0DER"
                if not (name and seller_id):
                    self._json({
                        "ok": False,
                        "error": "先把 店铺名称 和 卖家ID 填好，再点开始授权",
                    }, 400)
                    return
                url_out = _amz_begin_authorize({
                    "name": name,
                    "seller_id": seller_id,
                    "marketplace": marketplace,
                })
                self._json({"ok": True, "url": url_out})
                return

            # 智赢式授权第 2 步：粘回跳转网址 → 抽 code → 服务器换令牌
            if url.path == "/api/amazon/authorize/finish":
                r = _amz_finish_authorize(str(body.get("url") or ""))
                self._json(r)      # ok:false 也回 200，前端照渲染红字回执
                return

            # 一次性配置公共应用凭证（仅管理员；存服务器，不再回显）
            if url.path == "/api/amazon/app/save":
                app = {
                    "app_id": str(
                        (body.get("app_id") or "").strip()
                    )[:300],
                    "client_id": str(
                        (body.get("client_id") or "").strip()
                    )[:300],
                    "client_secret": str(
                        (body.get("client_secret") or "").strip()
                    )[:300],
                }
                if not all(app.values()):
                    self._json({
                        "ok": False,
                        "error": "App ID / Client ID / Client Secret 都要填",
                    }, 400)
                    return
                try:
                    r = _amz_store_api({"action": "app_save", "app": app})
                except Exception as exc:
                    r = {"ok": False, "error": f"连不上服务器：{exc}"[:200]}
                self._json(
                    r if r.get("ok") else
                    {"ok": False, "error": str(r.get("error") or "")[:300]},
                    200 if r.get("ok") else 400,
                )
                return

            # 店铺管理页的「测连接」：向服务器要一次短令牌
            if url.path == "/api/amazon/store/test":
                sid = str(body.get("id") or "").strip()
                if not sid:
                    self._json({"ok": False, "error": "缺少店铺 id"}, 400)
                    return
                try:
                    r = _amz_store_api(
                        {"action": "token", "id": sid}, timeout=30
                    )
                except Exception as exc:
                    r = {"ok": False, "error": f"连不上服务器：{exc}"[:200]}
                self._json(
                    {"ok": True, "token_ok": bool(r.get("ok")),
                     "error": "" if r.get("ok")
                     else str(r.get("error") or "")[:300]}
                )
                return

            if url.path == "/api/ean/status":
                try:
                    r = _ean_api({"action": "status"}, timeout=15)
                except Exception as exc:
                    r = {"ok": False, "error": f"连不上服务器：{exc}"[:150]}
                self._json(
                    r if r.get("ok")
                    else {"ok": False, "error": str(r.get("error") or "")[:200]}
                )
                return

            # 选中店铺（本机记住默认用哪家店传）
            if url.path == "/api/amazon/store/select":
                sid = str(body.get("id") or "").strip()
                amz = dict(CONFIG.get("amazon") or {})
                amz["active_store"] = sid
                CONFIG["amazon"] = amz
                _save_json_file(CONFIG_PATH, CONFIG)
                self._json({"ok": True})
                return

            # 解绑店铺（仅管理员——worker 校验 admin_key）
            if url.path == "/api/amazon/store/delete":
                sid = str(body.get("id") or "").strip()
                if not sid:
                    self._json({"ok": False, "error": "缺少店铺 id"}, 400)
                    return
                r = _amz_store_delete(sid)
                if not r.get("ok"):
                    self._json(
                        {"ok": False, "error": str(r.get("error") or "")[:300]},
                        400,
                    )
                    return
                # 别让乐观合并把刚解绑的店铺又「复活」了
                just = _AMZ_AUTHZ.get("store") or {}
                if str(just.get("id") or "") == sid:
                    _AMZ_AUTHZ["store"] = {}
                _AMZ_TOKENS.pop(sid, None)
                amz = dict(CONFIG.get("amazon") or {})
                if amz.get("active_store") == sid:
                    amz["active_store"] = ""
                    CONFIG["amazon"] = amz
                    _save_json_file(CONFIG_PATH, CONFIG)
                self._json({"ok": True})
                return

            if url.path == "/api/amazon/authorize/poll":
                self._json({"ok": True, **_AMZ_AUTHZ})
                return

            if url.path == "/api/amazon/upload":
                pids = [
                    str(p) for p in body.get("pids") or [] if str(p).strip()
                ]
                if not pids:
                    self._json(
                        {"ok": False, "error": "请先勾选产品"}, 400
                    )
                    return

                c = _amz_cfg()
                with _AMZ_LOCK:
                    if _AMZ_TASK.get("phase") in (
                        "building", "uploading", "polling",
                    ):
                        self._json({
                            "ok": False,
                            "error": "已有一次上传在进行，等它完成",
                        }, 409)
                        return

                    # 店铺优先：服务器绑定的店铺；没绑过且本机有旧凭证
                    # 才走本机路径（测试/旧数据兜底）
                    stores, srv_err = _amz_stores()
                    sid = str(body.get("store") or "").strip()
                    store = next(
                        (s for s in stores if str(s.get("id")) == sid),
                        None,
                    )
                    if not store:
                        store = _amz_active_store(stores)
                    legacy_ok = all(
                        str(c.get(k) or "").strip()
                        for k in ("client_id", "client_secret",
                                  "refresh_token", "seller_id")
                    )
                    if not store and not legacy_ok:
                        self._json({
                            "ok": False,
                            "error": (
                                "还没绑定店铺（点「➕ 绑定新店铺」，"
                                "只需做一次）"
                                if not srv_err
                                else f"读店铺清单失败：{srv_err}"
                            ),
                        }, 400)
                        return

                    mk = str(body.get("marketplace") or "").strip()
                    if mk not in _MARKETPLACES:
                        mk = ""
                    _AMZ_RUN.update(
                        store=store,
                        mid=mk
                        or (
                            str(store.get("marketplace") or "")
                            if store else ""
                        )
                        or str(c.get("marketplace") or "ATVPDKIKX0DER"),
                        seller_id=(
                            str(store.get("seller_id") or "")
                            if store else str(c.get("seller_id") or "")
                        ),
                    )

                    variant_mode = (
                        body.get("variant_mode")
                        if body.get("variant_mode") in (
                            "auto", "single", "variant"
                        ) else "auto"
                    )
                    product_type = (
                        str(
                            body.get("product_type")
                            or c.get("product_type") or "PRODUCT"
                        ).strip()[:60]
                        or "PRODUCT"
                    )
                    _AMZ_TASK.update(
                        phase="building", message="准备中…",
                        total=len(pids), pids=pids,
                        feed_id="", result=None,
                        started_at=int(time.time()),
                    )

                _amz_start_upload(
                    pids, variant_mode, product_type,
                    allow_unmapped=bool(body.get("allow_unmapped", True)),
                )
                self._json({"ok": True, "count": len(pids)})
                return

            self._json({"ok": False, "error": "not found"}, 404)

        except BrokenPipeError:
            pass

        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 500)


# =====================================================
# 启动
# =====================================================

def _open_window(url: str) -> None:
    if os.environ.get("WZD_NO_WINDOW") == "1":
        return

    for chrome in (
        str(CONFIG.get("chrome") or ""),
        str(CONFIG.get("chrome_fallback") or ""),
    ):
        if chrome and Path(chrome).exists():
            subprocess.Popen(
                [
                    chrome,
                    "--app=" + url,
                    "--window-size=1440,900",
                ],
                close_fds=True,
            )
            return

    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    setup_proxy()

    port = int(CONFIG.get("port") or 17891)

    # D1.6.0 修「双窗口/双开」：Windows 上 SO_REUSEADDR 允许第二个
    # 实例把同一端口再 bind 一次（历史上的 17891 双绑定僵尸就是这么
    # 来的）。独占端口后，第二个实例才会老实走「已在跑」分支。
    class Srv(ThreadingHTTPServer):
        allow_reuse_address = 0

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                )
            super().server_bind()

    try:
        server = Srv(("127.0.0.1", port), Handler)
    except OSError:
        # 已有实例在跑。刚启动的实例自己会弹窗口——它启动 3 秒内
        # 我们再弹就成「双窗口」了，直接静默退出（D1.6.0 修复）。
        # os._exit：引擎的非 daemon 线程会拖着进程不退（留僵尸）。
        try:
            h = requests.get(
                f"http://127.0.0.1:{port}/api/health", timeout=2,
                proxies={"http": None, "https": None},
            ).json()
            if 0 <= int(h.get("uptime") or 99) < 3:
                os._exit(0)
        except Exception:
            pass
        _open_window(f"http://127.0.0.1:{port}/")
        os._exit(0)

    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.6, lambda: _open_window(url)).start()

    print(f"我的产品桌面版 {VERSION} → {url}（Ctrl+C 退出）")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

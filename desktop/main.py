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

VERSION = "D1.4.0"
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


def src_api(path: str, payload: dict, timeout: int = 30) -> dict:
    """调 Worker（语义对齐 services.sourcing.src_api）：
    浏览器 UA 防 Cloudflare 拦截；瞬态失败 1.5 秒后重试一次。"""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }

    for attempt in (1, 2):
        try:
            resp = requests.post(
                CONFIG["server"] + path,
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
# 凭证只存本机 config.json；worker 端零改动。
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
_AMZ_TOKEN = {"at": "", "exp": 0.0}
_AMZ_AUTHZ = {"state": "idle", "message": "", "url": ""}
_AUTHZ_PORT = 9999
_AMZ_UPLOADS_PATH = APP_DIR / "amazon_uploads.json"


def _amz_cfg() -> dict:
    return CONFIG.get("amazon") or {}


def _amz_spapi_host() -> str:
    base = str(_amz_cfg().get("spapi_base") or "").strip()
    if base:
        return base.rstrip("/")
    region = str(_amz_cfg().get("region") or "na").lower()
    mk = str(_amz_cfg().get("marketplace") or "")
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
    """LWA 访问令牌（约 1 小时有效，本地缓存自动续）。"""
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

def _amz_start_authorize(app_id: str, client_id: str, client_secret: str) -> str:
    global _AMZ_AUTHZ

    if _AMZ_AUTHZ.get("state") == "running":
        return _AMZ_AUTHZ.get("url") or ""

    redirect = f"http://localhost:{_AUTHZ_PORT}/callback"
    url = (
        "https://sellercentral.amazon.com/apps/authorize/consent"
        f"?application_id={app_id}&version=beta"
    )
    _AMZ_AUTHZ = {
        "state": "running",
        "message": "等您在浏览器里完成授权（登录亚马逊并点同意）…",
        "url": url,
    }

    class AuthHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _page(self, text: str) -> None:
            body = (
                "<meta charset='utf-8'><body style='font-family:sans-serif;"
                "font-size:20px;text-align:center;padding:70px'>"
                + text + "</body>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path != "/callback":
                self._page("不是授权回调地址")
                return

            q = parse_qs(u.query)
            code = (q.get("spapi_oauth_code") or q.get("code") or [""])[0]
            err = (
                q.get("error_description") or q.get("error") or [""]
            )[0]

            if err:
                _AMZ_AUTHZ.update(
                    state="error", message="授权失败：" + str(err)[:300],
                )
            elif code:
                try:
                    tok = requests.post(
                        _amz_lwa_host() + "/auth/o2/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": code,
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "redirect_uri": redirect,
                        },
                        timeout=20,
                    ).json()
                    rt = str(tok.get("refresh_token") or "")
                    if not rt:
                        raise RuntimeError(
                            str(tok.get("error_description") or tok)[:300]
                        )
                    amz = dict(CONFIG.get("amazon") or {})
                    amz["refresh_token"] = rt
                    CONFIG["amazon"] = amz
                    _save_json_file(CONFIG_PATH, CONFIG)
                    _AMZ_TOKEN.update(at="", exp=0.0)
                    _AMZ_AUTHZ.update(
                        state="done", message="✅ 授权成功，已自动保存",
                    )
                except Exception as exc:
                    _AMZ_AUTHZ.update(
                        state="error",
                        message="换令牌失败：" + str(exc)[:300],
                    )
            else:
                _AMZ_AUTHZ.update(state="error", message="没拿到授权码")

            self._page(
                "✅ 授权成功！可以关掉这个网页，回到「我的产品」窗口。"
                if _AMZ_AUTHZ["state"] == "done"
                else "❌ 授权没成功，回到「我的产品」窗口重试。"
            )
            threading.Timer(0.6, self.server.shutdown).start()

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", _AUTHZ_PORT), AuthHandler)
    except OSError:
        _AMZ_AUTHZ.update(
            state="error",
            message=f"本地 {_AUTHZ_PORT} 端口被占，关掉占它的程序再试",
        )
        return url

    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def _expire() -> None:
        if _AMZ_AUTHZ.get("state") == "running":
            _AMZ_AUTHZ.update(
                state="error",
                message="15 分钟没完成授权，重新点「🔓 一键授权」",
            )
        try:
            httpd.shutdown()
        except Exception:
            pass

    threading.Timer(900, _expire).start()
    return url


# ---- 刊登数据：产品 → JSON_LISTINGS_FEED 的 messages ----

def _amz_build_messages(
    products: list, mid: str, lang: str,
    variant_mode: str, product_type: str,
) -> tuple[list, list]:
    """返回 (messages, notes)。内容来源：AI 优化结果优先，没有就
    原始资料。图片只收 https（亚马逊要公网可下载）。变体：>1 行且
    行行有颜色 → 父子变体（颜色主题），否则各行独立单品。"""

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
        return title, bullets[:5], desc

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
                messages.append({
                    "messageId": msg(),
                    "sku": sku,
                    "operationType": "UPDATE",
                    "productType": product_type,
                    "requirements": "LISTING",
                    "attributes": attrs,
                })
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
                messages.append({
                    "messageId": msg(),
                    "sku": sku,
                    "operationType": "UPDATE",
                    "productType": product_type,
                    "requirements": "LISTING",
                    "attributes": attrs,
                })

    return messages, notes


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
            mid = str(c.get("marketplace") or "ATVPDKIKX0DER")
            info = _MARKETPLACES.get(mid, ("na", "en_US", ""))

            messages, notes = _amz_build_messages(
                products, mid, info[1], variant_mode, product_type,
            )
            if not messages:
                raise RuntimeError(
                    "没有可上传的内容：" + "；".join(notes)[:300]
                )

            payload = {
                "header": {
                    "sellerId": str(c.get("seller_id") or ""),
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

            # ---- 亚马逊上传（D1.4.0）：状态 / 任务进度（不回显密钥） ----
            if url.path == "/api/amazon/status":
                c = _amz_cfg()
                configured = all(
                    str(c.get(k) or "").strip()
                    for k in ("client_id", "client_secret",
                              "refresh_token", "seller_id")
                )
                mk = str(c.get("marketplace") or "ATVPDKIKX0DER")
                info = _MARKETPLACES.get(mk)
                self._json({
                    "ok": True,
                    "configured": configured,
                    "marketplace": mk,
                    "marketplace_name": info[2] if info else mk,
                    "language_tag": info[1] if info else "en_US",
                    "product_type": str(c.get("product_type") or "PRODUCT"),
                    "seller_id": str(c.get("seller_id") or ""),
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
                c = _amz_cfg()
                app_id = str(body.get("app_id") or c.get("app_id") or "").strip()
                client_id = str(
                    body.get("client_id") or c.get("client_id") or ""
                ).strip()
                client_secret = str(
                    body.get("client_secret") or c.get("client_secret") or ""
                ).strip()
                if not (app_id and client_id and client_secret):
                    self._json({
                        "ok": False,
                        "error": (
                            "先把 App ID / Client ID / Client Secret "
                            "填好并保存，再点一键授权"
                        ),
                    }, 400)
                    return
                url_out = _amz_start_authorize(
                    app_id, client_id, client_secret
                )
                self._json({"ok": True, "url": url_out})
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

                    if not all(
                        str(c.get(k) or "").strip()
                        for k in ("client_id", "client_secret",
                                  "refresh_token", "seller_id")
                    ):
                        self._json({
                            "ok": False,
                            "error": "亚马逊授权还没配置好（点「⚙️ 授权设置」）",
                        }, 400)
                        return

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

                _amz_start_upload(pids, variant_mode, product_type)
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

    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", port), Handler
        )
    except OSError:
        # 已有实例在跑：只弹窗口
        _open_window(f"http://127.0.0.1:{port}/")
        return

    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.4, lambda: _open_window(url)).start()

    print(f"我的产品桌面版 {VERSION} → {url}（Ctrl+C 退出）")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

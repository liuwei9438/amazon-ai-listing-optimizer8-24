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

VERSION = "D1.0.0"
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
                it["cat"] = str(u.get("cat") or "")[:40]

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

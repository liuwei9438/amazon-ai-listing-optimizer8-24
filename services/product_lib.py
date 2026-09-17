from __future__ import annotations


# =====================================================
# V2.10 产品库（优化程序端，仅管理员）
#
# 永久产品库：产品存在 Worker KV（prod:<pid>），Streamlit 重启
# 也不丢。核心能力：
#   ① 导入：Excel / 插件粘贴，相同 父SKU/SKU 自动合并成一个产品，
#      变体行挂在产品下；重复导入 = 合并更新，不重复建条
#   ② 管理：分类筛选 / 标题·SKU·型号搜索 / 状态计数
#      （待找货 / 已找到 / 没找到）/ 搜索词和分类表格里直接改
#   ③ 找货（V2.10 并入本页，独立找货页已删）：勾选 N 个 →
#      「一键找供应商」自动 AI 转中文搜索词 → 建任务带 pid →
#      老板插件自动接单跑 1688 → Worker 把结果永久挂到产品记录
#      （保留最近 5 轮，含找货时间）→ 这里算匹配分并保存最佳
#      供应商 → 状态变 ✅已找到
#   ④ 重新找货：换个日子核价，历史轮次都留着
#   ⑤ 导出：产品 + 采购五列 的 Excel
#
# V2.11 产品归属：每个员工自己的库（登录 token 鉴权；总后台
# 「开找货」= 产品库+找货权限）；管理员可看「全部产品」（只读）
# 或某个人的库（只读）。写操作永远只落自己的库。
#
# 复用 sourcing 的打分 / AI 转换 / 轮询逻辑。
# =====================================================


import hashlib
import io
import json
import math
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from core import read_workbook
from services.json_storage import load_json, save_json
from services.sourcing import (
    _buy_url,
    _cell_str,
    _counts_line,
    _current_user,
    convert_search_terms,
    fallback_model,
    first_url,
    get_src_key,
    image_similarity,
    prefetch_phashes,
    score_candidate,
    src_api,
)


PL_STORE = Path("tasks/productlib/batches.json")

PAGE_SIZE = 50  # 列表每页条数

_STATUS_LABELS = {
    "wait": "⚪ 待找货",
    "found": "✅ 已找到",
    "none": "❌ 没找到",
}

# 与采集插件「发送到优化」一致的 20 列（粘贴导入用）
_COLLECTOR_HEADERS = [
    "父SKU(必填)", "SKU", "库存", "币种", "成本价(必填)", "运费",
    "材料", "包装材料", "语言", "标题(必填)", "颜色",
    "要点1", "要点2", "要点3", "要点4", "要点5",
    "简介", "产品图", "简介图", "参考网址",
]


# =====================================================
# 权限 / 索引 / 本地批次登记
# =====================================================


def productlib_allowed() -> bool:
    """产品库入口：登录账号要开找货权限（总后台「开找货」）；
    管理员（app_admins）始终可见；没配登录的本地开发放行。"""
    try:
        from services.user_auth import current_user

        me = str(current_user() or "")

    except Exception:
        me = ""

    if not me:
        return True  # 没配登录（本地开发）

    if _pl_is_admin():
        return True

    return bool(st.session_state.get("auth_src"))


def _pl_is_admin() -> bool:
    try:
        from services.user_auth import is_admin_user

        return bool(is_admin_user())

    except Exception:
        return False


def _admin_key() -> str:
    """总管理密钥（优化程序 Secrets 的 auth_admin_key；只有管理员带）。"""
    try:
        return str(st.secrets["auth_admin_key"] or "").strip()

    except Exception:
        return ""


def _prod_auth() -> dict:
    """V2.11 产品库请求的鉴权：优先登录账号 token（各人自己的库）；
    没登录（本地开发）退回找货密钥（旧全局库）。"""
    token = str(st.session_state.get("auth_token") or "")

    if token:
        payload = {"token": token}

        if _pl_is_admin() and _admin_key():
            payload["admin_key"] = _admin_key()

        return payload

    return {"key": get_src_key()}


def _scope_owner() -> str:
    """管理员当前查看的范围："" = 自己的库；"all" = 全部（只读）；
    "<账号>" = 看某个人的（只读）。非管理员恒为 ""。"""
    if not _pl_is_admin():
        return ""

    return str(st.session_state.get("prodlib_scope_owner") or "")


def _pl_payload(extra: dict | None = None, scoped: bool = False) -> dict:
    """产品库请求的公共载荷。scoped=True（/prod_list）时带上管理员
    当前查看的范围；写操作一律走自己的库（别人的库在前端只读）。"""
    payload = _prod_auth()

    if scoped:
        scope = _scope_owner()

        if scope == "all":
            payload["scope"] = "all"

        elif scope:
            payload["owner"] = scope

    if extra:
        payload.update(extra)

    return payload


def _pid_owner(pid: str) -> str:
    """取产品详情时它在谁的库（管理员看「全部产品」按索引归属取；
    自己的库返回 ""）。"""
    scope = _scope_owner()

    if scope != "all":
        return scope

    for it in (st.session_state.get("prodlib_index") or {}).get("items") or []:
        if str(it.get("pid")) == str(pid):
            return str(it.get("owner") or "")

    return ""


def _refresh_index(src_key: str) -> dict:
    """从 Worker 拉产品库索引（V2.11 按登录账号 / 管理员选的范围）。"""
    data = src_api("/prod_list", _pl_payload(scoped=True), timeout=40)

    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "读取产品库失败"))

    st.session_state["prodlib_index"] = data

    # scope=all 的响应带全部库主名单（给「看谁的库」下拉用）
    if data.get("owners"):
        st.session_state["prodlib_all_owners"] = [
            str(o) for o in data.get("owners") or [] if str(o or "").strip()
        ]

    return data


def _ensure_index(src_key: str) -> dict:
    cached = st.session_state.get("prodlib_index")

    if (
        isinstance(cached, dict)
        and cached.get("ok")
        and isinstance(cached.get("items"), list)
    ):
        return cached

    try:
        return _refresh_index(src_key)

    except Exception as exc:
        st.error(f"产品库读取失败：{exc}")

        return {"ok": False, "items": [], "cats": [], "counts": {}}


def _load_pl_batches() -> list:
    data = load_json(PL_STORE, default=[])

    return data if isinstance(data, list) else []


def _save_pl_batches(batches: list) -> None:
    try:
        PL_STORE.parent.mkdir(parents=True, exist_ok=True)

        save_json(PL_STORE, batches[:20])

    except Exception:
        pass


def _append_pl_batch(record: dict) -> None:
    # V2.11：记是谁建的批次（各人只看自己的进行中批次）
    record["user"] = _current_user() or ""
    batches = _load_pl_batches()
    batches.insert(0, record)
    _save_pl_batches(batches)


def _set_pl_flag(batch_id: str, flag: str, value=True) -> None:
    batches = _load_pl_batches()
    hit = False

    for record in batches:
        if record.get("batch_id") == batch_id:
            record[flag] = value
            hit = True

    if hit:
        _save_pl_batches(batches)


def _pending_batch_id() -> str | None:
    """进行中的产品库批次：会话里的优先，否则最近一个没收尾的。
    V2.11：各人只认自己建的批次；旧记录（没记 user）只有管理员能接。"""
    active = st.session_state.get("prodlib_active")

    if active:
        return str(active)

    me = _current_user() or ""
    admin = _pl_is_admin()

    for record in _load_pl_batches():
        if not (
            record.get("batch_id")
            and not record.get("finalized")
            and not record.get("abandoned")
        ):
            continue

        owner = str(record.get("user") or "")

        if not admin and (owner or me) and owner != me:
            continue

        return str(record.get("batch_id"))

    return None


def _log_event(kind: str, rows: int = 0, **extra) -> None:
    try:
        from services.user_auth import log_user_event

        log_user_event(kind, rows=rows, **extra)

    except Exception:
        pass


# =====================================================
# 导入：表格 → 产品（变体自动合并）
# =====================================================


def _norm_pid(raw: str) -> str:
    text = re.sub(r"\s+", "", str(raw or "").strip().lower())
    text = re.sub(r"[^a-z0-9_-]", "", text)

    return text[:64]


def _pick_columns(df: pd.DataFrame) -> dict:
    cols = [str(c) for c in df.columns]

    def exact(*names):
        for name in names:
            if name in cols:
                return name

        return None

    title = exact("标题(必填)", "商品标题", "产品标题", "标题")

    if title is None:
        title = next(
            (c for c in cols if "标题" in c or "title" in c.lower()),
            None,
        )

    parent = exact("父SKU(必填)", "父SKU", "父sku")
    sku = next(
        (c for c in cols if "父" not in c and "sku" in c.lower()),
        None,
    )
    img = exact("产品图") or next(
        (c for c in cols if "产品图" in c or "主图" in c or "图片" in c),
        None,
    )
    ref = exact("参考网址") or next(
        (c for c in cols if "参考" in c),
        None,
    )
    color = exact("颜色") or next(
        (c for c in cols if "颜色" in c),
        None,
    )

    return {
        "title": title,
        "parent": parent,
        "sku": sku,
        "img": img,
        "ref": ref,
        "color": color,
    }


def parse_products(df: pd.DataFrame) -> tuple[list, dict]:
    """DataFrame → 产品列表。相同 父SKU/SKU 的行并成一个产品，
    其余行成为变体（保留各自的 SKU/颜色/图/标题差异）。"""
    cols = _pick_columns(df)

    if not cols["title"]:
        raise ValueError("表里找不到标题列（标题/标题(必填)）")

    groups: dict[str, dict] = {}
    order: list[str] = []
    variant_rows = 0

    for _, row in df.iterrows():
        title = _cell_str(row.get(cols["title"]))
        parent = _cell_str(row.get(cols["parent"])) if cols["parent"] else ""
        sku = _cell_str(row.get(cols["sku"])) if cols["sku"] else ""

        if not title and not parent and not sku:
            continue

        key_raw = parent or sku or ""

        if key_raw:
            pid = _norm_pid(key_raw)

        else:
            pid = "t" + hashlib.sha1(
                title.encode("utf-8")
            ).hexdigest()[:16]

        if pid not in groups:
            groups[pid] = {
                "pid": pid,
                "sku": key_raw[:60],
                "title": title[:200],
                "model": fallback_model(title),
                "cat": "",
                "kw": "",
                "img": "",
                "ref_url": "",
                "variants": [],
            }
            order.append(pid)

        group = groups[pid]

        variant_img = first_url(row.get(cols["img"])) if cols["img"] else ""

        if not group["img"] and variant_img:
            group["img"] = variant_img

        if not group["ref_url"] and cols["ref"]:
            group["ref_url"] = first_url(row.get(cols["ref"]))

        variant_sku = sku if (sku and sku != key_raw) else ""
        variant_attr = _cell_str(row.get(cols["color"])) if cols["color"] else ""
        variant_title = title if title and title != group["title"] else ""

        if variant_sku or variant_attr or (variant_img and variant_img != group["img"]):
            variant_rows += 1

            if len(group["variants"]) < 50:
                group["variants"].append(
                    {
                        "sku": variant_sku[:60],
                        "title": variant_title[:200],
                        "img": variant_img[:400],
                        "attr": variant_attr[:60],
                    }
                )

    products = [groups[pid] for pid in order]

    return products, {
        "rows": len(df),
        "products": len(products),
        "variant_rows": variant_rows,
    }


def _paste_to_dataframe(text: str):
    """插件复制的 JSON → DataFrame（和 Excel 导入同一条解析路径）。"""
    text = str(text or "").strip()

    if not text:
        return None, "请先粘贴内容"

    try:
        data = json.loads(text)

    except Exception:
        return None, "粘贴的内容不是有效 JSON（请用插件面板的「发送到优化」复制）"

    rows = data.get("rows") if isinstance(data, dict) else data

    if not isinstance(rows, list) or not rows:
        return None, "粘贴内容里没有产品行"

    records = []

    for row in rows[:2000]:
        if isinstance(row, dict):
            records.append(
                [str(row.get(header, "") or "") for header in _COLLECTOR_HEADERS]
            )

    if not records:
        return None, "粘贴内容里没有可识别的产品行"

    return pd.DataFrame(records, columns=_COLLECTOR_HEADERS), None


def _prod_upsert(products: list, by: str, progress=None) -> dict:
    """分块调 /prod_upsert，progress(完成数, 总数) 每块回调一次。

    Worker 免费版单次调用最多 50 次 KV 子操作（每次读写都算）：
    一块 20 个产品 = 1 读索引 + 20 读 + 20 写 + 1 存索引 = 42 次，
    稳在限内；块超过约 24 个就会中途报 Too many subrequests。
    """
    added = 0
    updated = 0
    total = 0
    n = len(products)

    for start in range(0, n, 20):
        chunk = products[start:start + 20]

        data = src_api(
            "/prod_upsert",
            _pl_payload({"by": by, "products": chunk}),
            timeout=60,
        )

        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "导入失败"))

        added += int(data.get("added", 0))
        updated += int(data.get("updated", 0))
        total = int(data.get("total", 0))

        if progress:
            try:
                progress(min(start + len(chunk), n), n)
            except Exception:
                pass

    return {"added": added, "updated": updated, "total": total}


def _prod_write(
    path: str,
    field: str,
    values: list,
    timeout: int = 60,
    label: str = "写入失败",
) -> None:
    """批量写产品库（改分类/删产品/回写最佳供应商）分块提交。

    和 _prod_upsert 同理：一块 20 条 ≈ 42 次 KV 子操作，
    不超过 Worker 免费版单次 50 次的上限。失败抛 RuntimeError。
    """
    for start in range(0, len(values), 20):
        chunk = values[start:start + 20]

        resp = src_api(
            path,
            _pl_payload({field: chunk}),
            timeout=timeout,
        )

        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error") or label))


# =====================================================
# 小工具
# =====================================================


def _fmt_ms(ms) -> str:
    try:
        value = int(ms or 0)

        if value <= 0:
            return "—"

        moment = datetime.fromtimestamp(value / 1000)

        return moment.strftime("%m-%d %H:%M")

    except Exception:
        return "—"


def _best_text(best: dict) -> str:
    if not isinstance(best, dict):
        return "—"

    if not (best.get("url") or best.get("company")):
        return "—"

    parts = []

    if best.get("score"):
        parts.append(f"{best['score']}分")

    if best.get("price"):
        parts.append(str(best["price"]))

    if best.get("moq"):
        parts.append(f"起订{best['moq']}")

    if best.get("company"):
        parts.append(str(best["company"])[:14])

    return "｜".join(parts) if parts else "—"


def _export_products(items: list) -> io.BytesIO:
    rows = []

    for item in items:
        best = item.get("best") or {}

        rows.append(
            {
                "SKU": item.get("sku", ""),
                "标题": item.get("title", ""),
                "型号": item.get("model", ""),
                "分类": item.get("cat", ""),
                "变体数": item.get("n_var", 0),
                "状态": _STATUS_LABELS.get(
                    item.get("status") or "wait",
                    "",
                ).replace(" ", ""),
                "中文搜索词": item.get("kw", ""),
                "匹配分": best.get("score", ""),
                "采购价": best.get("price", ""),
                "起订量": best.get("moq", ""),
                "供应商": best.get("company", ""),
                "采购链接": best.get("url", ""),
                "找货时间": _fmt_ms(item.get("last_round")),
            }
        )

    output = io.BytesIO()

    pd.DataFrame(rows).to_excel(
        output,
        index=False,
        sheet_name="产品库",
    )

    output.seek(0)

    return output


# =====================================================
# 页面
# =====================================================


def render_product_library(api_key: str, model: str) -> None:
    """产品库主页面（管理员 + 开了找货权限的账号，见 productlib_allowed）。"""
    st.markdown(
        """
        <div class="hint-bar">
        <b>📚 产品库：</b>导入产品（相同 SKU 自动合并变体）→ 分分类 →
        勾选找供应商 → 结果永久挂在产品上（匹配分/价格/起订量/采购链接），
        随时重新找货核价、随时导出。
        </div>
        """,
        unsafe_allow_html=True,
    )

    # V2.11：管理员可切换看谁的库（自己的可操作，别人的只读）
    if _pl_is_admin():
        _render_scope_selector()

    src_key = get_src_key()

    # V2.11：登录账号走 token，不需要 SRC_KEY；只拦没登录又没配密钥的
    if (
        not str(st.session_state.get("auth_token") or "")
        and not src_key
    ):
        st.warning(
            "还没配置找货密钥：管理后台（Worker /manage）点「生成找货密钥」，"
            "再把密钥填进优化程序 Secrets 的 SRC_KEY。"
            "（登录账号使用产品库不需要密钥，这条只影响没登录的本地开发。）"
        )

        return

    index = _ensure_index(src_key)

    _render_import(src_key, index)
    _render_sourcing_panel(src_key, api_key, model)
    _render_active_batch(src_key)
    _render_table(src_key)
    _render_detail(src_key)


def _render_scope_selector() -> None:
    """管理员看谁的库：自己的 / 全部产品（只读）/ 某个员工（只读）。"""
    owners = st.session_state.get("prodlib_all_owners") or []
    options = ["", "all"] + [
        str(o)
        for o in owners
        if str(o or "").strip() and str(o) not in ("", "all")
    ]
    labels = {"": "我的库", "all": "全部产品（只读）"}

    current = _scope_owner()

    if current not in options:
        current = ""

    pick = st.selectbox(
        "看谁的库",
        options,
        index=options.index(current),
        format_func=lambda v: labels.get(v, f"@{v} 的库（只读）"),
        key="prodlib_scope_pick",
    )

    if pick != _scope_owner():
        st.session_state["prodlib_scope_owner"] = pick
        st.session_state.pop("prodlib_index", None)
        st.rerun()


# -----------------------------------------------------
# ① 导入
# -----------------------------------------------------


def _render_import(src_key: str, index: dict) -> None:
    if _scope_owner():
        return  # 只读视图（别人的库）：导入只进自己的库

    items = index.get("items") or []

    with st.expander(
        "📥 导入产品（Excel / 从采集插件粘贴）",
        expanded=not items,
    ):
        col_a, col_b = st.columns(2)

        with col_a:
            upload = st.file_uploader(
                "上传采集 Excel（20 列模板）",
                type=["xlsx"],
                key="prodlib_uploader",
            )

        with col_b:
            paste = st.text_area(
                "或粘贴插件复制的 JSON（插件面板「发送到优化」复制的内容）",
                height=120,
                key="prodlib_paste",
            )

        dataframe = None
        error = None

        if upload is not None:
            try:
                envelope = read_workbook(upload.name, upload.getvalue())
                dataframe = envelope.dataframe

            except Exception as exc:
                error = f"Excel 解析失败：{exc}"

        elif str(paste or "").strip():
            dataframe, error = _paste_to_dataframe(paste)

        if error:
            st.error(error)

            return

        if dataframe is None:
            st.caption(
                "相同 SKU/父SKU 再次导入会合并更新（不重复建条）；"
                "同产品的变体行自动并成一个产品。"
            )

            return

        try:
            products, stats = parse_products(dataframe)

        except Exception as exc:
            st.error(f"解析失败：{exc}")

            return

        if not products:
            st.warning("没识别到产品行（表里要有标题列）")

            return

        st.caption(
            f"识别 {stats['rows']} 行 → **{stats['products']} 个产品**"
            f"（其中 {stats['variant_rows']} 行并成变体）"
        )

        preview = pd.DataFrame(
            [
                {
                    "SKU": p["sku"][:20],
                    "标题": p["title"][:40],
                    "型号": p["model"],
                    "变体": len(p["variants"]),
                    "图": "✓" if p["img"] else "—",
                }
                for p in products[:8]
            ]
        )

        st.dataframe(
            preview,
            use_container_width=True,
            hide_index=True,
        )

        if len(products) > 800:
            st.caption(
                "⚠️ 一次导入超过 800 个产品：KV 免费档每天共 1000 次写入，"
                "这次会占用大部分额度；如中途报配额错误，明早 8 点后"
                "重新导入同一份 Excel 即可（已导入的自动算更新，不重复）。"
            )

        if st.button(
            f"✅ 导入产品库（{len(products)} 个产品）",
            type="primary",
            key="prodlib_import_btn",
        ):
            bar = st.progress(0.0, "导入产品库…")

            try:
                result = _prod_upsert(
                    products,
                    _current_user()[:32] or "optimizer",
                    progress=lambda done, total: bar.progress(
                        done / total if total else 1.0,
                        f"导入产品库… {done} / {total}",
                    ),
                )

            except Exception as exc:
                bar.empty()
                st.error(f"导入失败：{exc}")

                return

            bar.empty()

            _log_event("prod_import", rows=len(products))

            try:
                _refresh_index(src_key)

            except Exception:
                pass

            st.toast(
                f"✅ 新增 {result['added']} · 更新 {result['updated']}"
                "（重复导入的算更新）"
            )
            st.rerun()


# -----------------------------------------------------
# ② 找供应商面板（勾选后出现）
# -----------------------------------------------------


def _render_sourcing_panel(src_key: str, api_key: str, model: str) -> None:
    if _scope_owner():
        return  # 只读视图：找货只对自己的库发起

    if not st.session_state.get("prodlib_src_panel"):
        return

    pids = st.session_state.get("prodlib_src_pids") or []

    index = st.session_state.get("prodlib_index") or {}
    items = index.get("items") or []

    pid_set = {str(p) for p in pids}
    products = [it for it in items if str(it.get("pid")) in pid_set]

    if not products:
        st.session_state.pop("prodlib_src_panel", None)
        st.session_state.pop("prodlib_src_pids", None)

        return

    convert_key = "prodlib_convert"

    with st.expander(
        f"🔍 给 {len(products)} 个产品找供应商（1688）",
        expanded=True,
    ):
        # V2.11.2：这里原来也是 st.expander —— 折叠面板套折叠面板会被
        # Streamlit 直接抛异常（Expanders may not be nested），换成边框容器。
        with st.container(border=True):
            st.caption("⚙ 高级选项（按变体找 / 修改搜索词）")
            mode = st.radio(
                "找货粒度",
                [
                    "整品找（推荐：1 个产品 1 个任务）",
                    "按变体分别找（变体是不同零件时用，各自带图搜）",
                ],
                key="prodlib_mode",
            )

            per_variant = str(mode).startswith("按变体")

            convert_map = st.session_state.get(convert_key) or {}

            # ---- 搜索词微调（留空的点按钮时自动 AI 转换） ----
            edit_rows = []

            for it in products:
                conv = convert_map.get(str(it.get("pid"))) or {}
                title = str(it.get("title") or "")

                edit_rows.append(
                    {
                        "pid": str(it.get("pid")),
                        "SKU": str(it.get("sku") or "")[:20],
                        "标题": title[:36],
                        "搜索词": str(
                            conv.get("kw") or it.get("kw") or ""
                        )[:60],
                        "型号": str(
                            conv.get("model")
                            or it.get("model")
                            or fallback_model(title)
                        )[:60],
                        "品牌": str(conv.get("brand") or "")[:30],
                    }
                )

            st.caption(
                "搜索词可以直接改；留空的会在点「一键找供应商」时自动用 AI 转好。"
            )

            kw_edited = st.data_editor(
                pd.DataFrame(edit_rows),
                key="pl_kw_editor",
                num_rows="fixed",
                use_container_width=True,
                hide_index=True,
                disabled=["pid", "SKU", "标题"],
            )

        act_a, act_b = st.columns([3, 1])

        create_clicked = act_a.button(
            f"🚀 一键找供应商（{len(products)} 个产品）",
            type="primary",
            key="pl_create_btn",
            use_container_width=True,
        )

        if act_b.button("收起", key="pl_cancel_btn", use_container_width=True):
            st.session_state.pop("prodlib_src_panel", None)
            st.session_state.pop("prodlib_src_pids", None)
            st.session_state.pop(convert_key, None)
            st.rerun()

            return

        if create_clicked:
            _one_click_create(
                src_key,
                api_key,
                model,
                products,
                kw_edited,
                per_variant,
            )


def _one_click_create(
    src_key: str,
    api_key: str,
    model: str,
    products: list,
    kw_frame: pd.DataFrame,
    per_variant: bool,
) -> None:
    """一键找供应商：没中文搜索词的先自动 AI 转换，然后直接建任务。"""
    records = kw_frame.to_dict("records")

    need_convert = [
        it
        for rec, it in zip(records, products)
        if not str(rec.get("搜索词") or "").strip()
        and not str(it.get("kw") or "").strip()
    ]

    converted = None

    if need_convert and str(api_key or "").strip():
        with st.spinner(
            f"🤖 AI 转中文搜索词（{len(need_convert)} 个）…"
        ):
            try:
                converted = convert_search_terms(
                    [it.get("title") or "" for it in need_convert],
                    api_key,
                    model,
                )

            except Exception as exc:
                converted = None
                st.warning(
                    f"AI 转换失败，这次先用英文标题跑：{exc}"
                )

    elif need_convert:
        st.caption(
            "💡 左侧没配 AI Key：没搜索词的这次用英文标题先跑"
            "（英文在 1688 效果差，之后可在表格里补中文再重找）。"
        )

    if converted:
        merged = dict(st.session_state.get("prodlib_convert") or {})

        for it, entry in zip(need_convert, converted):
            merged[str(it.get("pid"))] = entry

        st.session_state["prodlib_convert"] = merged

        conv_by_pid = {
            str(it.get("pid")): entry
            for it, entry in zip(need_convert, converted)
        }

        for rec, it in zip(records, products):
            entry = conv_by_pid.get(str(it.get("pid")))

            if entry and not str(rec.get("搜索词") or "").strip():
                rec["搜索词"] = str(entry.get("kw") or "")[:60]

                if not str(rec.get("型号") or "").strip():
                    rec["型号"] = str(entry.get("model") or "")[:60]

                if not str(rec.get("品牌") or "").strip():
                    rec["品牌"] = str(entry.get("brand") or "")[:30]

    _create_product_batch(
        src_key,
        products,
        pd.DataFrame(records),
        per_variant,
    )


def _create_product_batch(
    src_key: str,
    products: list,
    kw_frame: pd.DataFrame,
    per_variant: bool,
) -> None:
    kw_map = {}

    for row in kw_frame.to_dict("records"):
        kw_map[str(row.get("pid"))] = {
            "kw": str(row.get("搜索词") or "").strip()[:60],
            "model": str(row.get("型号") or "").strip()[:60],
            "brand": str(row.get("品牌") or "").strip()[:30],
        }

    # 按变体找时需要完整记录（索引里没有变体明细）
    full_records = {}

    if per_variant:
        for it in products:
            pid = str(it.get("pid"))

            try:
                data = src_api(
                    "/prod_list",
                    _pl_payload({"pid": pid}),
                )

                if data.get("ok") and data.get("product"):
                    full_records[pid] = data["product"]

            except Exception:
                pass

    tasks = []
    meta_items = []

    for it in products:
        pid = str(it.get("pid"))
        title = str(it.get("title") or "")

        entry = kw_map.get(pid) or {}
        kw = entry.get("kw") or title[:60]
        part_model = entry.get("model") or fallback_model(title)
        brand = entry.get("brand") or ""
        image_url = str(it.get("img") or "")

        variants = (full_records.get(pid) or {}).get("variants") or []

        if per_variant and variants:
            for vi, variant in enumerate(variants[:20]):
                vid = str(variant.get("sku") or f"v{vi + 1}")[:60]
                variant_img = str(variant.get("img") or "") or image_url

                if not kw and not variant_img:
                    continue

                tasks.append(
                    {
                        "kw": kw[:60],
                        "title": title[:200],
                        "model": part_model[:60],
                        "image_url": variant_img[:400],
                        "pid": pid,
                        "vid": vid,
                    }
                )
                meta_items.append(
                    {
                        "tid": "",
                        "pid": pid,
                        "vid": vid,
                        "kw": kw[:60],
                        "model": part_model,
                        "brand": brand,
                        "title_en": title[:200],
                        "image_url": variant_img,
                    }
                )

        else:
            if not kw and not image_url:
                continue

            tasks.append(
                {
                    "kw": kw[:60],
                    "title": title[:200],
                    "model": part_model[:60],
                    "image_url": image_url[:400],
                    "pid": pid,
                    "vid": "",
                }
            )
            meta_items.append(
                {
                    "tid": "",
                    "pid": pid,
                    "vid": "",
                    "kw": kw[:60],
                    "model": part_model,
                    "brand": brand,
                    "title_en": title[:200],
                    "image_url": image_url,
                }
            )

    if not tasks:
        st.error("没有可创建的任务（选中的产品既没搜索词也没图）")

        return

    if len(tasks) > 200:
        st.error(
            f"一次最多 200 个任务（当前 {len(tasks)} 个），请少选一些分批跑。"
        )

        return

    try:
        data = src_api(
            "/src_task_add",
            _pl_payload(
                {
                    "by": _current_user()[:32] or "optimizer",
                    "note": f"产品库 {len(tasks)} 个",
                    "tasks": tasks,
                }
            ),
            timeout=60,
        )

    except Exception as exc:
        st.error(f"连不上 Worker：{exc}")

        return

    if not data.get("ok"):
        st.error(f"创建失败：{data.get('error') or '未知错误'}")

        return

    batch_id = str(data.get("batch_id"))

    for index, meta in enumerate(meta_items):
        meta["tid"] = f"t{index + 1}"

    _append_pl_batch(
        {
            "batch_id": batch_id,
            "created_at": datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
            "count": len(meta_items),
            "finalized": False,
            "items": meta_items,
        }
    )

    _log_event("prod_batch", rows=len(meta_items), batch_id=batch_id)

    st.session_state["prodlib_active"] = batch_id
    st.session_state.pop("prodlib_src_panel", None)
    st.session_state.pop("prodlib_src_pids", None)
    st.session_state.pop("prodlib_convert", None)
    st.rerun()


# -----------------------------------------------------
# ③ 进行中的批次：进度 + 收尾打分
# -----------------------------------------------------


def _render_active_batch(src_key: str) -> None:
    batch_id = _pending_batch_id()

    if not batch_id:
        return

    record = next(
        (r for r in _load_pl_batches() if r.get("batch_id") == batch_id),
        None,
    )

    if not record:
        st.session_state.pop("prodlib_active", None)

        return

    st.markdown("#### 🔍 产品库找货批次")

    try:
        data = src_api(
            "/src_tasks_view",
            _pl_payload({"batch_id": batch_id, "with_results": False}),
        )

    except Exception as exc:
        st.error(f"读取批次失败：{exc}")

        return

    if not data.get("ok"):
        st.error(f"读取批次失败：{data.get('error')}")

        return

    batches = data.get("batches") or []

    if not batches:
        st.warning("Worker 上查不到这个批次（可能已被后台删除）。")

        return

    counts = batches[0].get("counts") or {}

    pending = counts.get("pending", 0) + counts.get("running", 0)

    if pending > 0:
        st.markdown(f"**{_counts_line(counts)}**")

        _render_live(src_key, batch_id)

        st.info(
            "老板 Chrome 的插件保持开启就行（v5.1 起自动接单：每分钟检查一次"
            "队列，有任务自动执行）；这里每 20 秒自动刷新。"
        )

        if st.button(
            "不看了（结果以后也会自动挂到产品上）",
            key="pl_batch_hide",
        ):
            st.session_state.pop("prodlib_active", None)
            _set_pl_flag(batch_id, "abandoned")

            st.rerun()

        return

    if not record.get("finalized"):
        with st.spinner("📥 下载候选图片并计算匹配分…"):
            try:
                n_done = _finalize_batch(src_key, record)

            except Exception as exc:
                st.error(f"收尾失败：{exc}")

                if st.button("重新收尾", key="pl_refinal"):
                    st.rerun()

                return

        _set_pl_flag(batch_id, "finalized")

        try:
            _refresh_index(src_key)

        except Exception:
            pass

        st.success(
            f"✅ 批次跑完：{n_done} 个产品已算出匹配分并保存最佳供应商"
            "（列表里看「最佳供应商」列）。"
        )

    else:
        st.caption("✅ 这个批次已完成。")

    if st.button("关闭", key="pl_batch_close"):
        st.session_state.pop("prodlib_active", None)
        _set_pl_flag(batch_id, "finalized")

        st.rerun()


def _render_live(src_key: str, batch_id: str) -> None:
    """pending 时每 20 秒自动拉一次进度的小块；跑完触发整页刷新。"""
    fragment = getattr(st, "fragment", None) or getattr(
        st,
        "experimental_fragment",
        None,
    )

    if fragment is None:
        st.caption("插件执行中…点「🔄」重新读取产品库看最新状态。")

        return

    @fragment(run_every=20)
    def _live():
        try:
            data = src_api(
                "/src_tasks_view",
                _pl_payload({"batch_id": batch_id, "with_results": False}),
            )

            live_counts = (
                (data.get("batches") or [{}])[0].get("counts")
                if data.get("ok")
                else None
            )

        except Exception:
            live_counts = None

        if live_counts is None:
            st.caption("⏳ 插件执行中…（暂时连不上 Worker）")

            return

        left = (
            live_counts.get("pending", 0)
            + live_counts.get("running", 0)
        )

        if left > 0:
            st.caption(
                f"⏳ 插件执行中…{_counts_line(live_counts)}"
                "（每 20 秒自动更新）"
            )

        else:
            st.caption("✅ 插件已跑完，正在计算匹配分…")

            try:
                st.rerun(scope="app")

            except TypeError:
                st.rerun()

    _live()


def _finalize_batch(src_key: str, record: dict) -> int:
    """批次跑完后：拉结果 → 算匹配分 → 最佳供应商写回产品。"""
    data = src_api(
        "/src_tasks_view",
        _pl_payload(
            {"batch_id": record.get("batch_id"), "with_results": True}
        ),
        timeout=40,
    )

    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "读取结果失败"))

    tasks_by_tid = {
        str(task.get("tid")): task
        for task in (data.get("batches") or [{}])[0].get("tasks") or []
    }

    items = record.get("items") or []

    urls = []

    for item in items:
        if item.get("image_url"):
            urls.append(item["image_url"])

        task = tasks_by_tid.get(str(item.get("tid")), {})

        for cand in task.get("results") or []:
            if cand.get("img"):
                urls.append(cand["img"])

    cache = prefetch_phashes(urls)

    updates = []

    for item in items:
        task = tasks_by_tid.get(str(item.get("tid")), {})
        results = task.get("results") or []

        if not results:
            continue

        scored = []

        for cand in results:
            img_sim = image_similarity(
                item.get("image_url", ""),
                cand.get("img", ""),
                cache,
            )

            total, _parts = score_candidate(item, cand, img_sim)

            scored.append((total, cand))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        top_score, top = scored[0]

        updates.append(
            {
                "pid": item.get("pid"),
                "best": {
                    "score": str(top_score),
                    "url": _buy_url(top),
                    "price": str(top.get("price") or ""),
                    "moq": str(top.get("moq") or ""),
                    "company": str(top.get("company") or ""),
                    "img": str(top.get("img") or ""),
                },
            }
        )

    if updates:
        # 分块写（免费版 Worker 单次最多 50 次子操作）
        _prod_write(
            "/prod_best_set",
            "updates",
            updates,
            label="保存最佳供应商失败",
        )

    return len(updates)


# -----------------------------------------------------
# ④ 产品列表（筛选 / 编辑 / 批量动作 / 导出）
# -----------------------------------------------------


def _render_table(src_key: str) -> None:
    index = st.session_state.get("prodlib_index") or {}
    items = index.get("items") or []
    cats = list(index.get("cats") or [])
    counts = index.get("counts") or {}

    # V2.11：只读视图（别人的库 / 全部产品）
    read_only = bool(_scope_owner())
    show_owner = _scope_owner() == "all"

    if any(not str(it.get("cat") or "").strip() for it in items):
        if "未分类" not in cats:
            cats.append("未分类")

    st.markdown("#### 产品列表")

    f1, f2, f3, f4 = st.columns([1.2, 1.2, 2.4, 0.5])

    with f1:
        status_choice = st.selectbox(
            "状态",
            [
                f"全部 {counts.get('all', 0)}",
                f"待找货 {counts.get('wait', 0)}",
                f"已找到 {counts.get('found', 0)}",
                f"没找到 {counts.get('none', 0)}",
            ],
            key="prodlib_status",
        )

        status_code = ""

        if status_choice.startswith("待"):
            status_code = "wait"

        elif status_choice.startswith("已"):
            status_code = "found"

        elif status_choice.startswith("没"):
            status_code = "none"

    with f2:
        cat_choice = st.selectbox(
            "分类",
            ["全部分类"] + cats,
            key="prodlib_cat",
        )

    with f3:
        query = st.text_input(
            "搜索（标题 / SKU / 型号 / 搜索词）",
            key="prodlib_q",
        )

    with f4:
        st.caption("")
        refresh_clicked = st.button(
            "🔄",
            key="prodlib_refresh",
            help="重新读取产品库",
            use_container_width=True,
        )

    if refresh_clicked:
        try:
            _refresh_index(src_key)
            st.rerun()

        except Exception as exc:
            st.error(f"刷新失败：{exc}")

        return

    needle = re.sub(r"\s+", "", str(query or "")).lower()

    filtered = []

    for it in items:
        if status_code and (it.get("status") or "wait") != status_code:
            continue

        if cat_choice != "全部分类" and (it.get("cat") or "未分类") != cat_choice:
            continue

        if needle:
            haystack = re.sub(
                r"\s+",
                "",
                f"{it.get('title', '')}{it.get('sku', '')}"
                f"{it.get('model', '')}{it.get('kw', '')}",
            ).lower()

            if needle not in haystack:
                continue

        filtered.append(it)

    if not filtered:
        st.info("没有符合条件的产品。用上面「📥 导入」添加第一批。")

        return

    total_pages = max(1, math.ceil(len(filtered) / PAGE_SIZE))

    page = max(
        0,
        min(
            int(st.session_state.get("prodlib_page", 0)),
            total_pages - 1,
        ),
    )
    st.session_state["prodlib_page"] = page

    page_items = filtered[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    nav1, nav2, nav3 = st.columns([1, 2, 1])

    if nav1.button(
        "⬅ 上一页",
        key="pl_prev",
        disabled=page == 0,
        use_container_width=True,
    ):
        st.session_state["prodlib_page"] = page - 1
        st.rerun()

        return

    nav2.caption(
        f"第 {page + 1} / {total_pages} 页 · 符合条件 {len(filtered)} 个"
    )

    if nav3.button(
        "下一页 ➡",
        key="pl_next",
        disabled=page >= total_pages - 1,
        use_container_width=True,
    ):
        st.session_state["prodlib_page"] = page + 1
        st.rerun()

        return

    # ---- 勾选状态：会话里存 pid 集合，跨页 / 翻页保留 ----
    sel_state_key = "prodlib_selected"

    valid_pids = {str(it.get("pid")) for it in filtered}

    try:
        selected = set(
            st.session_state.get(sel_state_key) or []
        ) & valid_pids

    except Exception:
        selected = set()

    page_pid_set = {str(it.get("pid")) for it in page_items}

    rows_src = []
    row_pids = []

    for it in page_items:
        best = it.get("best") or {}

        rows_src.append(
            {
                **({} if read_only else {"选": str(it.get("pid")) in selected}),
                **(
                    {"归属": str(it.get("owner") or "—")[:16]}
                    if show_owner
                    else {}
                ),
                "图": str(it.get("img") or ""),
                "标题": str(it.get("title") or "")[:60],
                "型号": str(it.get("model") or "")[:20],
                "分类": str(it.get("cat") or ""),
                "变体": int(it.get("n_var") or 0),
                "状态": _STATUS_LABELS.get(
                    it.get("status") or "wait",
                    "",
                ),
                "搜索词": str(it.get("kw") or ""),
                "最佳供应商": _best_text(best),
                "采购链接": str(best.get("url") or ""),
                "找货时间": _fmt_ms(it.get("last_round")),
            }
        )
        row_pids.append(str(it.get("pid")))

    if read_only:
        st.caption("👁 只读视图 — 要操作产品请切回自己的库。")

    else:
        sel_bar1, sel_bar2, sel_bar3 = st.columns([1, 1, 2])

        if sel_bar1.button(
            "☑ 全选本页",
            key="pl_sel_all",
            use_container_width=True,
        ):
            st.session_state[sel_state_key] = sorted(selected | page_pid_set)
            st.rerun()

            return

        if sel_bar2.button(
            "✖ 清空勾选",
            key="pl_sel_none",
            use_container_width=True,
        ):
            st.session_state[sel_state_key] = []
            st.rerun()

            return

        sel_bar3.caption(f"已勾选 **{len(selected)}** 个（跨页保留）")

    edited = st.data_editor(
        pd.DataFrame(rows_src),
        num_rows="fixed",
        hide_index=True,
        use_container_width=True,
        height=min(38 * len(rows_src) + 60, 640),
        disabled=True if read_only else [
            "图", "标题", "型号", "变体", "状态",
            "最佳供应商", "采购链接", "找货时间",
        ],
        column_config={
            "选": st.column_config.CheckboxColumn(
                "选",
                width="small",
                default=False,
            ),
            "图": st.column_config.ImageColumn("图", width="small"),
            "标题": st.column_config.TextColumn("标题", width="large"),
            "搜索词": st.column_config.TextColumn(
                "中文搜索词",
                width="medium",
            ),
            "分类": st.column_config.TextColumn("分类", width="small"),
            "采购链接": st.column_config.LinkColumn(
                "链接",
                display_text="打开",
                width="small",
            ),
        },
    )

    try:
        edited_rows = edited.to_dict("records")

    except Exception:
        edited_rows = []

    # ---- 本页勾选写回会话（其他页的保留；只读视图不碰勾选） ----
    if not read_only:
        page_checked = {
            pid
            for ed_row, pid in zip(edited_rows, row_pids)
            if bool(ed_row.get("选"))
        }

        selected = (selected - page_pid_set) | page_checked
        st.session_state[sel_state_key] = sorted(selected)

    # ---- 表格里改的 搜索词/分类 自动保存 ----
    updates = []

    for src_row, ed_row, pid in zip(rows_src, edited_rows, row_pids):
        new_kw = str(ed_row.get("搜索词") or "").strip()
        new_cat = str(ed_row.get("分类") or "").strip()

        if (
            new_kw != str(src_row["搜索词"]).strip()
            or new_cat != str(src_row["分类"]).strip()
        ):
            updates.append({"pid": pid, "kw": new_kw, "cat": new_cat})

    if updates:
        try:
            # 分块写（免费版 Worker 单次最多 50 次子操作）
            _prod_write(
                "/prod_update",
                "updates",
                updates,
                label="保存修改失败",
            )

            by_pid = {u["pid"]: u for u in updates}
            session_index = st.session_state.get("prodlib_index")

            if isinstance(session_index, dict):
                for it in session_index.get("items", []):
                    change = by_pid.get(it.get("pid"))

                    if change:
                        it["kw"] = change["kw"]
                        it["cat"] = change["cat"]

            st.toast(f"✅ 已保存 {len(updates)} 处修改")
            st.rerun()

            return

        except Exception as exc:
            st.error(f"保存修改失败：{exc}")

    sel_pids = sorted(selected)

    n_sel = len(sel_pids)

    src_btn = cat_btn = del_btn = False

    if read_only:
        b4 = st.columns(1)[0]

    else:
        b1, b2, b3, b4 = st.columns(4)

        src_btn = b1.button(
            f"🔍 找供应商（{n_sel}）",
            type="primary",
            key="pl_src_btn",
            use_container_width=True,
            disabled=not n_sel,
        )

        cat_btn = b2.button(
            f"🏷 设分类（{n_sel}）",
            key="pl_cat_btn",
            use_container_width=True,
            disabled=not n_sel,
        )

        del_btn = b3.button(
            f"🗑 删除（{n_sel}）",
            key="pl_del_btn",
            use_container_width=True,
            disabled=not n_sel,
        )

    export_items = (
        [it for it in filtered if str(it.get("pid")) in set(sel_pids)]
        if n_sel
        else filtered
    )

    try:
        export_buf = _export_products(export_items)

        b4.download_button(
            f"⬇️ 导出（{len(export_items)}）",
            data=export_buf.getvalue(),
            file_name=(
                f"产品库_{datetime.now().strftime('%m%d_%H%M')}.xlsx"
            ),
            mime=(
                "application/vnd.openxmlformats-"
                "officedocument.spreadsheetml.sheet"
            ),
            key="pl_export_btn",
            use_container_width=True,
        )

    except Exception as exc:
        b4.caption(f"导出失败：{exc}")

    if src_btn:
        st.session_state["prodlib_src_pids"] = sel_pids
        st.session_state["prodlib_src_panel"] = True
        st.rerun()

        return

    if cat_btn:
        st.session_state["prodlib_cat_panel"] = True
        st.rerun()

        return

    if del_btn:
        st.session_state["prodlib_del_panel"] = True
        st.rerun()

        return

    # ---- 批量设分类 ----
    if st.session_state.get("prodlib_cat_panel") and n_sel:
        with st.expander(
            f"🏷 给选中的 {n_sel} 个产品设置分类",
            expanded=True,
        ):
            new_cat = st.text_input(
                "分类名（如：打印机配件 / 冰箱配件）",
                key="pl_cat_input",
                max_chars=40,
            )

            c1, c2 = st.columns(2)

            if c1.button(
                "✅ 应用分类",
                type="primary",
                key="pl_cat_apply",
                use_container_width=True,
            ):
                name = new_cat.strip()

                if not name:
                    st.error("先填分类名")

                else:
                    try:
                        # 分块写（免费版 Worker 单次最多 50 次子操作）
                        _prod_write(
                            "/prod_update",
                            "updates",
                            [{"pid": p, "cat": name} for p in sel_pids],
                            label="设置失败",
                        )
                        st.session_state.pop("prodlib_cat_panel", None)
                        _refresh_index(src_key)
                        st.toast(f"✅ 已设置分类：{name}")
                        st.rerun()

                    except Exception as exc:
                        st.error(f"设置失败：{exc}")

            if c2.button("取消", key="pl_cat_cancel", use_container_width=True):
                st.session_state.pop("prodlib_cat_panel", None)
                st.rerun()

    # ---- 删除确认 ----
    if st.session_state.get("prodlib_del_panel") and n_sel:
        st.warning(
            f"确定删除选中的 {n_sel} 个产品？"
            "变体和找货历史都会一起删，不可恢复。"
        )

        d1, d2 = st.columns(2)

        if d1.button(
            "🗑 确认删除",
            type="primary",
            key="pl_del_apply",
            use_container_width=True,
        ):
            try:
                # 分块删（免费版 Worker 单次最多 50 次子操作）
                _prod_write("/prod_del", "pids", sel_pids, label="删除失败")
                st.session_state.pop("prodlib_del_panel", None)
                _refresh_index(src_key)
                st.toast(f"🗑 已删除 {len(sel_pids)} 个产品")
                st.rerun()

            except Exception as exc:
                st.error(f"删除失败：{exc}")

        if d2.button("取消", key="pl_del_cancel", use_container_width=True):
            st.session_state.pop("prodlib_del_panel", None)
            st.rerun()


# -----------------------------------------------------
# ⑤ 产品详情（变体 / 找货历史 / 换供应商 / 重新找货）
# -----------------------------------------------------


def _render_detail(src_key: str) -> None:
    # V2.11：只读视图（别人的库）— 能看能算分，不能改不能重找
    ro = bool(_scope_owner())

    index = st.session_state.get("prodlib_index") or {}
    items = index.get("items") or []

    if not items:
        return

    head = items[:300]

    labels = [
        f"{str(it.get('sku') or it.get('pid'))[:18]}｜"
        f"{str(it.get('title') or '')[:34]}"
        for it in head
    ]

    choice = st.selectbox(
        "📋 产品详情（变体 / 找货历史 / 换供应商 / 重新找货）",
        ["（选择产品…）"] + labels,
        key="prodlib_detail_sel",
    )

    if not choice or choice.startswith("（"):
        return

    it = head[labels.index(choice)]
    pid = str(it.get("pid"))

    try:
        data = src_api(
            "/prod_list",
            _pl_payload({"pid": pid, "owner": _pid_owner(pid)}),
        )

    except Exception as exc:
        st.error(f"读取产品失败：{exc}")

        return

    if not data.get("ok"):
        st.error(f"读取产品失败：{data.get('error')}")

        return

    rec = data.get("product") or {}

    with st.expander(
        f"📋 {str(rec.get('title') or '')[:60]}",
        expanded=True,
    ):
        col_a, col_b = st.columns([1, 2])

        with col_a:
            if rec.get("img"):
                st.image(
                    rec["img"],
                    width=160,
                    caption="产品主图",
                )

        with col_b:
            best = rec.get("best") or {}

            st.markdown(
                f"搜索词：**{rec.get('kw') or '—'}**　"
                f"型号：**{rec.get('model') or '—'}**　"
                f"分类：**{rec.get('cat') or '未分类'}**　"
                f"状态：**{_STATUS_LABELS.get(rec.get('status') or 'wait', '')}**"
            )

            if best.get("url") or best.get("company"):
                st.markdown(
                    f"✅ 最佳供应商：匹配分 **{best.get('score') or '—'}** · "
                    f"{best.get('price') or ''} · "
                    f"起订 {best.get('moq') or '—'} · "
                    f"{best.get('company') or ''}"
                )

                if best.get("url"):
                    st.markdown(f"采购链接：{best['url']}")

                st.caption(f"确定时间：{_fmt_ms(best.get('at'))}")

            else:
                st.caption("还没有最佳供应商（找一次货或从历史轮次里选）。")

        e1, e2, e3, e4 = st.columns(4)

        with e1:
            new_kw = st.text_input(
                "中文搜索词",
                value=str(rec.get("kw") or ""),
                key=f"pl_d_kw_{pid}",
                max_chars=60,
                disabled=ro,
            )

        with e2:
            new_cat = st.text_input(
                "分类",
                value=str(rec.get("cat") or ""),
                key=f"pl_d_cat_{pid}",
                max_chars=40,
                disabled=ro,
            )

        with e3:
            status_now = str(rec.get("status") or "wait")

            new_status = st.selectbox(
                "状态",
                ["wait", "found", "none"],
                index=["wait", "found", "none"].index(status_now),
                format_func=lambda s: _STATUS_LABELS.get(s, s),
                key=f"pl_d_st_{pid}",
                disabled=ro,
            )

        with e4:
            st.caption("")

            if st.button(
                "💾 保存",
                key=f"pl_d_save_{pid}",
                use_container_width=True,
                disabled=ro,
            ):
                try:
                    src_api(
                        "/prod_update",
                        _pl_payload(
                            {
                                "updates": [
                                    {
                                        "pid": pid,
                                        "kw": new_kw.strip(),
                                        "cat": new_cat.strip(),
                                        "status": new_status,
                                    }
                                ]
                            }
                        ),
                        timeout=30,
                    )
                    _refresh_index(src_key)
                    st.toast("✅ 已保存")
                    st.rerun()

                except Exception as exc:
                    st.error(f"保存失败：{exc}")

        variants = rec.get("variants") or []

        if variants:
            st.markdown(f"**变体（{len(variants)} 个）**")

            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "SKU": v.get("sku", ""),
                            "属性": v.get("attr", ""),
                            "标题": str(v.get("title") or "")[:40],
                            "图": v.get("img", ""),
                        }
                        for v in variants
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "图": st.column_config.ImageColumn("图", width="small"),
                },
            )

        rounds = rec.get("rounds") or []

        st.markdown(f"**找货历史（{len(rounds)} 轮，保留最近 5 轮）**")

        if not rounds:
            st.caption("还没找过货。")

        else:
            score_key = f"prodlib_detail_scores_{pid}"

            for ri, rnd in enumerate(reversed(rounds)):
                results = rnd.get("results") or []
                vid = str(rnd.get("vid") or "")

                label = (
                    f"{_fmt_ms(rnd.get('at'))} · "
                    f"{rnd.get('kw') or ''}"
                    + (f" · 变体 {vid}" if vid else "")
                    + f" · {len(results)} 个候选"
                )

                # V2.11.2：同上，外层产品详情已是 expander，轮次改用边框容器
                with st.container(border=True):
                    st.caption("🕒 " + label)
                    if not results:
                        st.caption("这轮没有找到结果。")

                        continue

                    round_key = str(rnd.get("batch") or ri)
                    scored = (st.session_state.get(score_key) or {}).get(
                        round_key
                    )

                    if st.button(
                        "🎯 计算这轮的匹配分",
                        key=f"pl_round_score_{pid}_{ri}",
                    ):
                        with st.spinner("下载对比图片…"):
                            urls = [rec.get("img") or ""] + [
                                c.get("img")
                                for c in results
                                if c.get("img")
                            ]
                            cache = prefetch_phashes(urls)

                        item_like = {
                            "model": rec.get("model") or "",
                            "brand": "",
                            "title_en": rec.get("title") or "",
                            "image_url": rec.get("img") or "",
                        }

                        entries = []

                        for cand in results:
                            img_sim = image_similarity(
                                item_like["image_url"],
                                cand.get("img", ""),
                                cache,
                            )

                            total, parts = score_candidate(
                                item_like,
                                cand,
                                img_sim,
                            )

                            entries.append(
                                {
                                    "score": total,
                                    "cand": cand,
                                    "parts": parts,
                                }
                            )

                        entries.sort(
                            key=lambda entry: entry["score"],
                            reverse=True,
                        )

                        st.session_state.setdefault(score_key, {})[
                            round_key
                        ] = entries

                        scored = entries

                    if scored:
                        pick_labels = [
                            f"{index + 1}. {entry['score']}分 · "
                            f"{str(entry['cand'].get('title', ''))[:26]}"
                            for index, entry in enumerate(scored[:12])
                        ]

                        pick = st.selectbox(
                            "选择供应商",
                            pick_labels,
                            key=f"pl_round_pick_{pid}_{ri}",
                        )

                        try:
                            pick_index = int(str(pick).split(".")[0]) - 1

                        except Exception:
                            pick_index = 0

                        chosen = scored[pick_index]
                        cand = chosen["cand"]

                        st.markdown(
                            f"**{chosen['score']} 分** · "
                            f"{cand.get('price', '')} · "
                            f"起订 {cand.get('moq') or '—'} · "
                            f"{cand.get('company', '')}"
                        )
                        st.markdown(f"采购链接：{_buy_url(cand)}")

                        part_text = "　".join(
                            f"{name} {value}"
                            for name, value in (chosen.get("parts") or {}).items()
                            if value
                        )

                        if part_text:
                            st.caption(f"分项：{part_text}")

                        if not ro and st.button(
                            "⭐ 保存为最佳供应商",
                            key=f"pl_round_best_{pid}_{ri}",
                        ):
                            try:
                                src_api(
                                    "/prod_best_set",
                                    _pl_payload(
                                        {
                                            "updates": [
                                                {
                                                    "pid": pid,
                                                    "best": {
                                                        "score": str(
                                                            chosen["score"]
                                                        ),
                                                        "url": _buy_url(cand),
                                                        "price": str(
                                                            cand.get("price") or ""
                                                        ),
                                                        "moq": str(
                                                            cand.get("moq") or ""
                                                        ),
                                                        "company": str(
                                                            cand.get("company") or ""
                                                        ),
                                                        "img": str(
                                                            cand.get("img") or ""
                                                        ),
                                                    },
                                                }
                                            ]
                                        }
                                    ),
                                    timeout=30,
                                )
                                _refresh_index(src_key)
                                st.toast("✅ 已保存为最佳供应商")
                                st.rerun()

                            except Exception as exc:
                                st.error(f"保存失败：{exc}")

                    raw_rows = [
                        {
                            "价格": c.get("price", ""),
                            "起订量": c.get("moq", ""),
                            "供应商": c.get("company", ""),
                            "标题": str(c.get("title", ""))[:36],
                            "来源": c.get("from", ""),
                        }
                        for c in results
                    ]

                    st.dataframe(
                        pd.DataFrame(raw_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

        if not ro and st.button(
            "🔍 重新找货（这个产品）",
            key=f"pl_d_resrc_{pid}",
            type="primary",
        ):
            st.session_state["prodlib_src_pids"] = [pid]
            st.session_state["prodlib_src_panel"] = True
            st.rerun()

from __future__ import annotations

# =====================================================
# V2.12.0 一体工作台「我的产品」
#
# 优化 + 产品库合并成唯一页面：
#   导入（插件粘贴 / Excel）→ 产品永久存 Worker KV（含完整 20 列
#   资料 raw，v13.4 起 /prod_upsert 直接收）→ 列表勾选 →
#   🚀 AI 优化（复用原任务引擎 create_task/start_worker）→
#   结果经 /prod_opt_set 写回产品（has_opt 标记）→ 详情里看
#   AI 标题/五点/简介/亮点/SEO → 勾选导出优化后的 Excel。
#
# 设计要点（对着两处行号坑）：
#   ① 不同时间导入的产品 raw 里 row_number 都从 2 开始——优化
#      启动时把本次所有记录统一重排（运行内唯一），manifest 里
#      记 row_number→pid，任务跑完按它把结果写回产品。
#   ② 导出时把各产品的 raw_data 行拼成一张表，source_row_index
#      按拼接后的位置现场重算，和 export_unified 的定位规则
#      （Excel 行号 - 2 = pandas 行）严格一致。
#
# 旧数据（没有 raw 的产品）标「资料不全」，重新导入同一份
# Excel 即可补全（相同 pid 自动合并）。
# =====================================================


import hashlib
import io
import json
import math
import re
from dataclasses import asdict, replace
from datetime import datetime

import pandas as pd
import streamlit as st

from core import read_workbook
from core.models import FieldMap, ProductRecord
from core.record_builder import build_product_records

from services.current_task import (
    clear_current_task,
    load_current_task,
    save_current_task,
)
from services.json_storage import load_json, save_json
from services.listing_exporter import ListingExporter
from services.result_storage import load_failed_items, load_profiles
from services.sourcing import (
    _current_user,
    fallback_model,
    first_url,
    get_src_key,
    src_api,
)
from services.task_control import save_control
from services.task_manager import create_task, get_task_dir, load_status
from services.task_worker import start_worker

# 复用产品库的存储/鉴权辅助（product_lib 本身不渲染，页面在这边）
from services.product_lib import (
    _ensure_index,
    _fmt_ms,
    _norm_pid,
    _pl_is_admin,
    _pl_payload,
    _prod_upsert,
    _paste_to_dataframe,
    _refresh_index,
    _render_scope_selector,
    _scope_owner,
)


TASK_RUNNING_STATUS = ["created", "running", "processing"]

PAGE_SIZE = 20  # 卡片墙每页张数（V2.13.4）

# 采集插件 20 列模板 → FieldMap（粘贴导入直接建 ProductRecord，
# 和 Excel 上传走同一条 build_product_records 路径）
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

# KV 单条产品 raw 的序列化上限（和 worker v13.4 的 300000 对齐）
_RAW_MAX_CHARS = 300000


def _exp_lib_ok() -> bool:
    """网页导出权限：worker /login 的 exp_lib（总后台「网页导出」开关，
    默认允许）；管理员永远可导出。"""
    return bool(
        _pl_is_admin()
        or st.session_state.get("auth_exp_lib", True)
    )


# =====================================================
# 权限门 / 页面骨架
# =====================================================


def render_workbench(
    api_key: str,
    model: str,
    options: dict | None = None,
) -> None:
    """V2.12.0 唯一主页面：我的产品（导入 → 列表 → 优化 → 结果 → 导出）。"""
    options = options or {}

    me = _current_user() or ""

    if me and not _pl_is_admin() and not st.session_state.get("auth_src"):
        st.warning(
            "这个页面需要产品库权限：请联系管理员在管理后台"
            "（Worker /manage → 员工账号）点「开找货」开启。"
        )
        return

    src_key = get_src_key()

    # 登录账号走 token，不需要 SRC_KEY；只拦没登录又没配密钥的本地开发
    if (
        not str(st.session_state.get("auth_token") or "")
        and not src_key
    ):
        st.warning(
            "还没配置找货密钥：管理后台（Worker /manage）点「生成找货密钥」，"
            "再把密钥填进优化程序 Secrets 的 SRC_KEY。"
            "（登录账号不需要密钥，这条只影响没登录的本地开发。）"
        )
        return

    if _pl_is_admin():
        _render_scope_selector()

    _ensure_index(src_key)

    _render_task_card(api_key, model, options)
    _render_wb_import(src_key)
    _render_wb_table(src_key, api_key, model, options)
    _render_wb_detail(src_key)


# =====================================================
# 导入：Excel / 插件粘贴 → 产品 + 完整资料（raw）
# =====================================================


def _render_wb_import(src_key: str) -> None:
    if _scope_owner():
        return  # 只读视图（别人的库）：导入只进自己的库

    index = st.session_state.get("prodlib_index") or {}
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
                key="wb_uploader",
            )

        with col_b:
            paste = st.text_area(
                "或粘贴插件「发送到优化」复制的内容",
                height=120,
                key="wb_paste",
            )

        records = None
        error = None

        if upload is not None:
            try:
                envelope = read_workbook(upload.name, upload.getvalue())
                records = list(envelope.records)

            except Exception as exc:
                error = f"Excel 解析失败：{exc}"

        elif str(paste or "").strip():
            dataframe, error = _paste_to_dataframe(paste)

            if dataframe is not None:
                records = list(
                    build_product_records(dataframe, _TEMPLATE_FIELDS)
                )

        if error:
            st.error(error)

            return

        if records is None:
            st.caption(
                "相同 SKU/父SKU 再次导入会合并更新（不重复建条）；"
                "导入的产品带完整资料，勾选后直接 AI 优化，"
                "不用再单独上传 Excel。"
            )

            return

        products, stats, fat = _group_records(records)

        if not products:
            st.warning("没识别到产品行（表里要有标题列）")

            return

        info = (
            f"识别 {stats['rows']} 行 → **{stats['products']} 个产品**"
            f"（同产品的行自动并成一个产品）"
        )

        if fat:
            info += f"；⚠️ {len(fat)} 个产品资料过大，只存了基本信息"

        st.caption(info)

        preview = pd.DataFrame(
            [
                {
                    "SKU": p["sku"][:20],
                    "标题": p["title"][:40],
                    "资料行": len(p["raw"]),
                }
                for p in products[:8]
            ]
        )

        st.dataframe(
            preview,
            use_container_width=True,
            hide_index=True,
        )

        if st.button(
            f"✅ 导入我的产品（{len(products)} 个）",
            type="primary",
            key="wb_import_btn",
        ):
            bar = st.progress(0.0, "导入产品…")

            try:
                result = _prod_upsert(
                    products,
                    _current_user()[:32] or "optimizer",
                    progress=lambda done, total: bar.progress(
                        done / total if total else 1.0,
                        f"导入产品… {done} / {total}",
                    ),
                )

            except Exception as exc:
                bar.empty()
                st.error(f"导入失败：{exc}")

                return

            bar.empty()

            try:
                from services.user_auth import log_user_event

                log_user_event("prod_import", rows=len(products))

            except Exception:
                pass

            try:
                _refresh_index(src_key)

            except Exception:
                pass

            # 导入即选中：新进来的产品自动勾上，直接点 🚀 就能优化
            selected = set(
                st.session_state.get("prodlib_selected") or []
            )
            selected |= {p["pid"] for p in products}
            st.session_state["prodlib_selected"] = sorted(selected)

            st.toast(
                f"✅ 新增 {result['added']} · 更新 {result['updated']}"
                "（重复导入的算更新），已自动勾选"
            )
            st.rerun()


def _group_records(records: list) -> tuple[list, dict, list]:
    """ProductRecord 列表 → 产品列表（pid 规则和 parse_products 一致：
    父SKU / SKU 归一化，都没用标题哈希），每个产品带完整资料 raw。

    返回 (products, stats, 资料过大被丢弃 raw 的 pid 列表)。"""
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
            continue  # 没父SKU/SKU/标题的空行

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
        # tuple → list：存进 KV 再读回来就是 list，形状从入口统一
        raw["bullets"] = list(raw["bullets"] or [])
        raw["image_urls"] = list(raw["image_urls"] or [])
        raw["detail_image_urls"] = list(raw["detail_image_urls"] or [])
        group["raw"].append(raw)

    fat = []

    for pid in order:
        raw_str = ""

        try:
            raw_str = json.dumps(groups[pid]["raw"], ensure_ascii=False)

        except Exception:
            raw_str = ""

        if len(raw_str) > _RAW_MAX_CHARS:
            groups[pid]["raw"] = []
            fat.append(pid)

    products = [groups[pid] for pid in order]

    return products, {
        "rows": len(records),
        "products": len(products),
    }, fat


def _record_from_raw(rd: dict) -> ProductRecord:
    """KV 里的 raw 行 → ProductRecord（list 还原成 tuple）。"""
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
        detail_image_urls=tuple(rd.get("detail_image_urls") or []),
        language=str(rd.get("language") or ""),
        raw_data=dict(rd.get("raw_data") or {}),
    )


# =====================================================
# 🚀 从产品库发起优化（复用任务引擎）
# =====================================================


def _start_optimize(
    pids: list,
    api_key: str,
    model: str,
    options: dict,
) -> None:
    """勾选的产品 → 拉完整资料 → 重排行号 → 建任务跑 AI。

    行号重排：不同时间导入的产品 raw 里 row_number 都从 2 开始，
    混在一起会撞车（manifest 的 row→pid 映射会串产品）。这里把
    本次所有记录按顺序重编（i+2），manifest 记的就是重排后的号，
    任务结果写回产品时按它对号入座。"""
    pids = [str(p) for p in pids if str(p or "").strip()]

    if not pids:
        st.error("请先勾选产品")

        return

    if not str(api_key or "").strip():
        st.error(
            "还没配置 AI：员工请联系组长在小组看板填写 Key；"
            "管理员在左侧「AI 配置」里填写。"
        )

        return

    records: list[ProductRecord] = []
    rec_pids: list[str] = []
    skipped: list[str] = []

    bar = st.progress(0.0, "读取产品资料…")

    for i, pid in enumerate(pids):
        try:
            data = src_api(
                "/prod_list",
                _pl_payload({"pid": pid}),
                timeout=30,
            )

        except Exception:
            skipped.append(pid)
            continue

        product = (data.get("product") or {}) if data.get("ok") else {}
        raw_rows = product.get("raw") or []

        if not raw_rows:
            skipped.append(pid)
            continue

        for rd in raw_rows:
            records.append(_record_from_raw(rd))
            rec_pids.append(pid)

        bar.progress(
            (i + 1) / len(pids),
            f"读取产品资料… {i + 1} / {len(pids)}",
        )

    bar.empty()

    if skipped:
        st.warning(
            f"{len(skipped)} 个产品没有完整资料（旧库数据），本次已跳过；"
            "重新导入同一份 Excel 即可补全。"
        )

    if not records:
        st.error("选中的产品都没有完整资料，请先重新导入。")

        return

    # ---- 行号重排（见函数注释）----
    renumbered: list[ProductRecord] = []
    row_pid: dict[str, str] = {}

    for i, (rec, pid) in enumerate(zip(records, rec_pids)):
        renumbered.append(replace(rec, row_number=i + 2))
        row_pid[str(i + 2)] = pid

    # 总后台关了「详情描述参与AI优化」→ 简介不进 AI（省 token）
    if not st.session_state.get("desc_to_ai", True):
        renumbered = [
            replace(rec, description="") for rec in renumbered
        ]

    task_id = create_task(
        total_products=len(renumbered),
        filename="我的产品.xlsx",
    )

    task_dir = get_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    keep = [pid for pid in pids if pid not in set(skipped)]
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

    try:
        from services.user_auth import log_user_event

        log_user_event(
            "task_start",
            task_id=task_id,
            rows=len(renumbered),
            model=model,
            filename="我的产品.xlsx",
        )

    except Exception:
        pass

    start_worker(
        renumbered,
        task_id,
        api_key,
        model,
        options,
    )

    save_current_task(task_id)
    st.session_state["current_task"] = task_id
    st.session_state["task_started"] = True

    st.rerun()


# =====================================================
# 任务进度卡（置顶）：运行中进度 / 完成后写回产品
# =====================================================


def _render_task_card(api_key: str, model: str, options: dict) -> None:
    # V2.13.0：没有任务时整块不渲染（不留空标题/说明文字）
    task_id = str(
        st.session_state.get("current_task")
        or load_current_task()
        or ""
    )

    if not task_id:
        return

    status = load_status(task_id)

    if not status:
        # 任务指针失效（任务目录被清）——清掉指针别卡住页面
        clear_current_task()
        st.session_state.pop("current_task", None)
        st.session_state["task_started"] = False

        return

    st.markdown("#### 🚀 AI 优化任务")

    state = str(status.get("status") or "")
    completed = int(status.get("completed", 0) or 0)
    total = int(
        status.get("total")
        or status.get("total_products")
        or 0
    )
    failed_count = int(status.get("failed", 0) or 0)

    c1, c2 = st.columns([2, 1])

    with c2:
        if st.button(
            "🔄 刷新",
            key="wb_task_refresh",
            use_container_width=True,
        ):
            st.rerun()

    with c1:
        if _pl_is_admin():
            st.caption(f"任务 {task_id}")

    if state in TASK_RUNNING_STATUS or state == "paused":
        if total:
            st.progress(
                min(completed / total, 1.0),
                f"AI 处理中 {completed} / {total}",
            )

        st.markdown(
            f"**状态：{state} · 成功 {completed - failed_count}"
            f" · 失败 {failed_count}**"
        )

        ctl1, ctl2, ctl3 = st.columns(3)

        with ctl1:
            if st.button(
                "⏸️ 暂停",
                key="wb_pause",
                use_container_width=True,
                disabled=state == "paused",
            ):
                save_control(task_id, "pause")
                st.rerun()

        with ctl2:
            if st.button(
                "▶️ 继续",
                key="wb_resume",
                use_container_width=True,
                disabled=state != "paused",
            ):
                save_control(task_id, "running")
                st.rerun()

        with ctl3:
            if st.button(
                "⛔ 取消任务",
                key="wb_cancel",
                use_container_width=True,
            ):
                save_control(task_id, "cancel")
                st.rerun()

        st.caption(
            "AI 在后台运行，可以离开页面；这里每 20 秒自动刷新。"
        )
        _live_tick(task_id)

        return

    # ---- 任务结束（completed / cancelled / failed）----
    manifest_path = get_task_dir(task_id) / "pl_manifest.json"
    manifest = load_json(manifest_path, default=None)
    has_manifest = isinstance(manifest, dict) and manifest.get("pids")

    if has_manifest and not manifest.get("written_back"):
        with st.spinner("📥 把优化结果写回产品…"):
            try:
                n_written = _write_back_opt(task_id, manifest)

            except Exception as exc:
                st.error(f"结果写回失败：{exc}")

                if st.button("重试写回", key="wb_wb_retry"):
                    st.rerun()

                return

        try:
            _refresh_index(get_src_key())

        except Exception:
            pass

        if n_written:
            st.success(
                f"✅ 任务完成：结果已写回 {n_written} 个产品"
                "（列表状态变「已优化」，详情里看 AI 结果）。"
            )

        else:
            st.warning(
                "任务结束了，但没有可写回的结果"
                "（可能全部失败或被取消）。"
            )

    elif state == "completed":
        st.success("✅ 任务完成。")

    elif state == "cancelled":
        st.caption("⛔ 任务已取消（已完成的结果保留）。")

    else:
        st.error(f"❌ 任务失败：{status.get('message') or state}")

    profiles = load_profiles(task_id)
    failed_items = load_failed_items(task_id)

    m1, m2, m3 = st.columns(3)

    with m1:
        st.metric("成功行", len(profiles))

    with m2:
        st.metric("失败行", len(failed_items))

    with m3:
        st.metric("任务总数", total)

    # ---- 失败产品：列表 + 一键重跑（资料在库里，不用重传 Excel）----
    if failed_items and has_manifest and not _scope_owner():
        with st.expander(
            f"🚨 失败的产品（{len(failed_items)} 行）",
            expanded=False,
        ):
            rows = []

            for item in failed_items:
                if not isinstance(item, dict):
                    continue

                rows.append(
                    {
                        "行": item.get("source_row_index", ""),
                        "SKU": str(item.get("sku") or "")[:20],
                        "错误": str(item.get("error") or "")[:80],
                    }
                )

            if rows:
                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                )

            row_pid = manifest.get("row_pid") or {}
            fail_pids = []

            for item in failed_items:
                rid = (
                    item.get("source_row_index")
                    if isinstance(item, dict)
                    else None
                )

                pid = row_pid.get(str(rid)) if rid is not None else ""

                if pid and pid not in fail_pids:
                    fail_pids.append(pid)

            if fail_pids and st.button(
                f"🔄 重跑这 {len(fail_pids)} 个失败产品",
                key="wb_retry_failed",
                type="primary",
            ):
                _start_optimize(
                    fail_pids,
                    api_key,
                    model,
                    options,
                )

    # ---- 本批导出 / 关闭 ----
    act1, act2 = st.columns(2)

    with act1:
        if has_manifest and _exp_lib_ok():
            pids = [str(p) for p in manifest.get("pids") or []]

            try:
                buf = _export_pids(pids)

                if buf is not None:
                    st.download_button(
                        f"⬇️ 导出本批优化结果（{len(pids)} 个产品）",
                        data=buf.getvalue(),
                        file_name=(
                            "本批优化结果_"
                            + datetime.now().strftime("%m%d_%H%M")
                            + ".xlsx"
                        ),
                        mime=(
                            "application/vnd.openxmlformats-"
                            "officedocument.spreadsheetml.sheet"
                        ),
                        key="wb_batch_export",
                        use_container_width=True,
                    )

            except Exception as exc:
                st.caption(f"导出失败：{exc}")

    with act2:
        if st.button(
            "🧹 关闭这个任务",
            key="wb_close_task",
            use_container_width=True,
        ):
            clear_current_task()
            st.session_state.pop("current_task", None)
            st.session_state["task_started"] = False
            st.rerun()


def _live_tick(task_id: str) -> None:
    """运行中每 20 秒查一次本地状态文件，跑完触发整页刷新。"""
    fragment = getattr(st, "fragment", None) or getattr(
        st,
        "experimental_fragment",
        None,
    )

    if fragment is None:
        st.caption("（点「🔄 刷新」看最新进度）")

        return

    @fragment(run_every=20)
    def _tick():
        state = str((load_status(task_id) or {}).get("status") or "")

        if state in TASK_RUNNING_STATUS or state == "paused":
            st.caption("⏳ 每 20 秒自动刷新进度。")

        else:
            try:
                st.rerun(scope="app")

            except TypeError:
                st.rerun()

    _tick()


def _opt_from_profile(profile: dict, task_id: str) -> dict | None:
    """AI profile → 存进产品库的 opt（字段名和 worker /prod_opt_set
    对齐）。没有实质文字结果返回 None。"""

    def d(value) -> dict:
        return value if isinstance(value, dict) else {}

    gt = d(profile.get("generated_title"))
    title = str(gt.get("title") or "")

    short_r = d(profile.get("short_title_result"))
    short = (
        short_r.get("short_title")
        or short_r.get("title")
        or short_r.get("text")
        or ""
    )

    bullet_r = d(profile.get("bullet_result"))
    bullets = (
        bullet_r.get("bullets")
        or bullet_r.get("bullet_points")
        or bullet_r.get("points")
        or []
    )

    desc_r = d(profile.get("description_result"))
    desc = (
        desc_r.get("description")
        or desc_r.get("text")
        or desc_r.get("content")
        or ""
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
        "title": title[:600],
        "short_title": str(short)[:300],
        "bullets": [
            str(b)[:600] for b in bullets if str(b or "").strip()
        ][:8],
        "description": str(desc)[:8000],
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


def _write_back_opt(task_id: str, manifest: dict) -> int:
    """任务跑完：profiles（按行号）→ manifest 对回 pid →
    /prod_opt_set 分块写产品（一块 20 条，稳在 Worker 子操作限内）。
    一个产品多行资料的，取行号最小的那行结果。"""
    row_pid = manifest.get("row_pid") or {}

    profiles = load_profiles(task_id)

    first_by_pid: dict[str, dict] = {}

    for profile in sorted(
        (p for p in profiles if isinstance(p, dict)),
        key=lambda p: (
            (p.get("source_identity") or {}).get(
                "source_row_index"
            )
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
            _pl_payload({"updates": chunk}),
            timeout=60,
        )

        if not resp.get("ok"):
            raise RuntimeError(
                str(resp.get("error") or "写回产品失败")
            )

        n += int(resp.get("n") or 0)

    manifest["written_back"] = True
    manifest["n_written"] = n
    save_json(
        get_task_dir(task_id) / "pl_manifest.json",
        manifest,
    )

    return n


# =====================================================
# 产品列表（勾选 / 搜索 / 状态 / 分类 / 批量动作 / 导出）
# =====================================================


def _wb_state(item: dict) -> str:
    if item.get("has_opt"):
        return "opt"

    if int(item.get("n_rows") or 0) > 0:
        return "ready"

    return "thin"


_WB_STATUS = {
    "opt": "✅ 已优化",
    "ready": "⚪ 待优化",
    "thin": "⚠️ 资料不全",
}


# =====================================================
# V2.13.4 产品卡片（老产品库样式：图片 + 信息 + 状态角标）
# =====================================================

CARDS_PER_ROW = 5

_WB_BADGE = {
    "opt": ("✅ 已优化", "#1a6b32", "#e8f5e9"),
    "ready": ("⏳ 待优化", "#9a5b00", "#fff3e0"),
    "thin": ("⚠️ 资料不全", "#8a6d3b", "#f6efe4"),
}


def _render_product_card(
    it: dict,
    selected: set,
    read_only: bool = False,
    show_owner: bool = False,
) -> bool:
    """画一张产品卡：图片 / 状态角标 / 标题 / SKU / 型号 /
    分类 / 资料行数 / 优化时间。

    返回 True = 用户点了卡片上的选择按钮
    （由调用方统一改勾选集，跨页保留）。
    """
    pid = str(it.get("pid"))
    label, fg, bg = _WB_BADGE.get(
        _wb_state(it),
        ("⏳ 待优化", "#9a5b00", "#fff3e0"),
    )

    with st.container(border=True):
        img = str(it.get("img") or "")

        if img:
            try:
                st.image(img, width=160)

            except Exception:
                st.caption("（图片打不开）")

        else:
            st.caption("（无图片）")

        st.markdown(
            f'''<span style="background:{bg};color:{fg};
            border-radius:999px;padding:2px 10px;
            font-size:12px;font-weight:700;">{label}</span>''',
            unsafe_allow_html=True,
        )

        title = str(it.get("title") or "（无标题）")
        st.markdown(f"**{title[:60]}**")

        st.caption(
            f"SKU：{str(it.get('sku') or '—')[:24]}　"
            f"型号：{str(it.get('model') or '—')[:20]}"
        )
        st.caption(
            f"分类：{str(it.get('cat') or '未分类')}"
            f"（资料 {int(it.get('n_rows') or 0)} 行）"
        )

        opt_ms = int(it.get("opt_at") or 0)

        if opt_ms:
            st.caption(f"优化时间：{_fmt_ms(opt_ms)}")

        if show_owner:
            st.caption(f"👤 {str(it.get('owner') or '—')[:16]}")

        if read_only:
            return False

        is_sel = pid in selected

        return st.button(
            "☑ 已选中" if is_sel else "☐ 选择",
            key=f"wb_card_{pid}",
            type="primary" if is_sel else "secondary",
            use_container_width=True,
        )


def _render_wb_table(
    src_key: str,
    api_key: str,
    model: str,
    options: dict,
) -> None:
    index = st.session_state.get("prodlib_index") or {}
    items = index.get("items") or []
    cats = list(index.get("cats") or [])

    read_only = bool(_scope_owner())
    show_owner = _scope_owner() == "all"

    if any(not str(it.get("cat") or "").strip() for it in items):
        if "未分类" not in cats:
            cats.append("未分类")

    counts = {"all": len(items), "opt": 0, "ready": 0, "thin": 0}

    for it in items:
        counts[_wb_state(it)] += 1

    st.markdown("#### 产品列表")

    # ---- V2.13.4 老产品库样式：状态标签页（带数量）----
    st.session_state.setdefault("wb_status_code", "")

    tab_cols = st.columns(4)

    status_defs = [
        ("", f"全部（{counts['all']}）"),
        ("ready", f"⏳ 待优化（{counts['ready']}）"),
        ("opt", f"✅ 已优化（{counts['opt']}）"),
        ("thin", f"⚠️ 资料不全（{counts['thin']}）"),
    ]

    for col, (code, label) in zip(tab_cols, status_defs):
        active = st.session_state["wb_status_code"] == code

        if col.button(
            label,
            key=f"wb_tab_{code or 'all'}",
            type="primary" if active else "secondary",
            use_container_width=True,
        ):
            st.session_state["wb_status_code"] = code
            st.rerun()

            return

    state_code = st.session_state["wb_status_code"]

    # ---- 导入日期快捷筛选（created 毫秒，客户端过滤）----
    st.session_state.setdefault("wb_date_code", "all")

    date_cols = st.columns(8)

    date_defs = [
        ("all", "全部时间"),
        ("w1", "一周内"),
        ("m1", "一月内"),
        ("m3", "三月内"),
        ("m6", "半年内"),
        ("y1", "一年内"),
        ("y3", "三年内"),
        ("custom", "自定义"),
    ]

    for col, (code, label) in zip(date_cols, date_defs):
        active = st.session_state["wb_date_code"] == code

        if col.button(
            label,
            key=f"wb_date_{code}",
            type="primary" if active else "secondary",
            use_container_width=True,
        ):
            st.session_state["wb_date_code"] = code
            st.rerun()

            return

    date_days = {
        "w1": 7,
        "m1": 30,
        "m3": 91,
        "m6": 183,
        "y1": 365,
        "y3": 1096,
    }

    lo_ms = hi_ms = None

    if st.session_state["wb_date_code"] in date_days:
        lo_ms = (
            datetime.now().timestamp() * 1000
            - date_days[st.session_state["wb_date_code"]]
            * 86400000
        )

    elif st.session_state["wb_date_code"] == "custom":
        lo_col, hi_col = st.columns(2)

        with lo_col:
            lo_date = st.date_input("起始日期", key="wb_date_lo")

        with hi_col:
            hi_date = st.date_input("截止日期", key="wb_date_hi")

        if lo_date:
            lo_ms = datetime.combine(
                lo_date, datetime.min.time()
            ).timestamp() * 1000

        if hi_date:
            hi_ms = datetime.combine(
                hi_date, datetime.max.time()
            ).timestamp() * 1000

    # ---- 分类 / 搜索 / 排序 / 刷新 ----
    f1, f2, f3, f4 = st.columns([1.4, 2.2, 1.2, 0.5])

    with f1:
        cat_choice = st.selectbox(
            "分类",
            ["全部分类"] + cats,
            key="wb_cat",
        )

    with f2:
        query = st.text_input(
            "搜索（标题 / SKU / 型号）",
            key="wb_q",
        )

    with f3:
        sort_choice = st.selectbox(
            "排序",
            ["最近更新", "最近导入", "最早导入", "最近优化"],
            key="wb_sort",
        )

    with f4:
        st.caption("")
        refresh_clicked = st.button(
            "🔄",
            key="wb_refresh",
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

    sort_keys = {
        "最近更新": lambda it: -(int(it.get("updated") or 0)),
        "最近导入": lambda it: -(int(it.get("created") or 0)),
        "最早导入": lambda it: int(it.get("created") or 0),
        "最近优化": lambda it: -(int(it.get("opt_at") or 0)),
    }

    filtered = []

    for it in items:
        if state_code and _wb_state(it) != state_code:
            continue

        if cat_choice != "全部分类" and (
            it.get("cat") or "未分类"
        ) != cat_choice:
            continue

        created_ms = int(it.get("created") or 0)

        if lo_ms is not None and (
            not created_ms or created_ms < lo_ms
        ):
            continue

        if hi_ms is not None and (
            not created_ms or created_ms > hi_ms
        ):
            continue

        if needle:
            haystack = re.sub(
                r"\s+",
                "",
                f"{it.get('title', '')}{it.get('sku', '')}"
                f"{it.get('model', '')}",
            ).lower()

            if needle not in haystack:
                continue

        filtered.append(it)

    filtered.sort(
        key=sort_keys.get(sort_choice, sort_keys["最近更新"])
    )

    if not filtered:
        st.info(
            "没有符合条件的产品。用上面「📥 导入」添加第一批。"
        )

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
        key="wb_prev",
        disabled=page == 0,
        use_container_width=True,
    ):
        st.session_state["prodlib_page"] = page - 1
        st.rerun()

        return

    nav2.caption(
        f"第 {page + 1} / {total_pages} 页 · "
        f"共找到 {len(filtered)} 个产品（库里共 {len(items)} 个）"
    )

    if nav3.button(
        "下一页 ➡",
        key="wb_next",
        disabled=page >= total_pages - 1,
        use_container_width=True,
    ):
        st.session_state["prodlib_page"] = page + 1
        st.rerun()

        return

    # ---- 跳页（产品多了光靠上一页/下一页翻不过来）----
    jump1, jump2, _jump3 = st.columns([0.7, 0.7, 3.6])

    jump_to = jump1.number_input(
        "跳到第几页",
        min_value=1,
        max_value=total_pages,
        value=page + 1,
        key="wb_jump",
        label_visibility="collapsed",
    )

    if jump2.button(
        "跳页",
        key="wb_jump_btn",
        use_container_width=True,
    ):
        st.session_state["prodlib_page"] = max(
            0, min(int(jump_to) - 1, total_pages - 1)
        )
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



    sel_pids = sorted(selected)
    n_sel = len(sel_pids)

    # ---- 任务运行中不许再开一个（会和进行中的任务抢）----
    task_busy = False
    _tid = str(
        st.session_state.get("current_task")
        or load_current_task()
        or ""
    )

    if _tid:
        _st = load_status(_tid)

        task_busy = bool(
            _st
            and (
                _st.get("status") in TASK_RUNNING_STATUS
                or _st.get("status") == "paused"
            )
        )

    opt_btn = cat_btn = del_btn = export_clicked = False

    if read_only:
        st.caption("👁 只读视图 — 要操作产品请切回自己的库。")

    else:
        sel_bar1, sel_bar2, sel_bar3 = st.columns([1, 1, 2])

        if sel_bar1.button(
            "☑ 全选本页",
            key="wb_sel_all",
            use_container_width=True,
        ):
            st.session_state[sel_state_key] = sorted(
                selected | page_pid_set
            )
            st.rerun()

            return

        if sel_bar2.button(
            "✖ 清空勾选",
            key="wb_sel_none",
            use_container_width=True,
        ):
            st.session_state[sel_state_key] = []
            st.rerun()

            return

        sel_bar3.caption(f"已勾选 **{n_sel}** 个（跨页保留）")

        # 操作按钮在表格上方：勾选完不用滚过整页数据找按钮
        b1, b2, b3, b4 = st.columns(4)

        opt_btn = b1.button(
            f"🚀 AI 优化（{n_sel}）",
            type="primary",
            key="wb_opt_btn",
            use_container_width=True,
            disabled=not n_sel or task_busy,
        )

        cat_btn = b2.button(
            f"🏷 设分类（{n_sel}）",
            key="wb_cat_btn",
            use_container_width=True,
            disabled=not n_sel,
        )

        del_btn = b3.button(
            f"🗑 删除（{n_sel}）",
            key="wb_del_btn",
            use_container_width=True,
            disabled=not n_sel,
        )

        # V2.13.1：没有「网页导出」权限的账号不显示导出按钮
        export_clicked = _exp_lib_ok() and b4.button(
            f"⬇️ 导出优化结果（{n_sel}）",
            key="wb_export_btn",
            use_container_width=True,
            disabled=not n_sel,
        )

        if task_busy:
            b1.caption("任务运行中，结束后才能再优化。")

        if opt_btn:
            _start_optimize(sel_pids, api_key, model, options)

            return

        if export_clicked:
            buf = None

            try:
                buf = _export_pids(sel_pids)

            except Exception as exc:
                st.error(f"导出失败：{exc}")

            if buf is None:
                st.info(
                    "选中的产品都还没有优化结果"
                    "（先跑一次 🚀 AI 优化，或把「已优化」的勾进来）。"
                )

            else:
                st.download_button(
                    f"⬇️ 下载优化结果（{len(sel_pids)} 个产品）",
                    data=buf.getvalue(),
                    file_name=(
                        "我的产品_优化结果_"
                        + datetime.now().strftime("%m%d_%H%M")
                        + ".xlsx"
                    ),
                    mime=(
                        "application/vnd.openxmlformats-"
                        "officedocument.spreadsheetml.sheet"
                    ),
                    type="primary",
                    key="wb_export_dl",
                    use_container_width=True,
                )

    # ---- V2.13.4 产品卡片墙：图片 + 信息 + 状态角标 ----
    toggles = []

    for r0 in range(0, len(page_items), CARDS_PER_ROW):
        row_slice = page_items[r0:r0 + CARDS_PER_ROW]
        row_cols = st.columns(CARDS_PER_ROW)

        for col, it in zip(row_cols, row_slice):
            with col:
                if _render_product_card(
                    it,
                    selected,
                    read_only=read_only,
                    show_owner=show_owner,
                ):
                    toggles.append(str(it.get("pid")))

    # 卡片上的 ☑ 点击 → 并进跨页勾选集合（翻页 / 换筛选仍保留）
    if toggles:
        for pid in toggles:
            if pid in selected:
                selected.discard(pid)

            else:
                selected.add(pid)

        st.session_state[sel_state_key] = sorted(selected)
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
                key="wb_cat_input",
                max_chars=40,
            )

            c1, c2 = st.columns(2)

            if c1.button(
                "✅ 应用分类",
                type="primary",
                key="wb_cat_apply",
                use_container_width=True,
            ):
                name = new_cat.strip()

                if not name:
                    st.error("先填分类名")

                else:
                    try:
                        from services.product_lib import _prod_write

                        _prod_write(
                            "/prod_update",
                            "updates",
                            [
                                {"pid": p, "kw": "", "cat": name}
                                for p in sel_pids
                            ],
                            label="设置失败",
                        )
                        st.session_state.pop(
                            "prodlib_cat_panel", None
                        )
                        _refresh_index(src_key)
                        st.toast(f"✅ 已设置分类：{name}")
                        st.rerun()

                    except Exception as exc:
                        st.error(f"设置失败：{exc}")

            if c2.button(
                "取消",
                key="wb_cat_cancel",
                use_container_width=True,
            ):
                st.session_state.pop("prodlib_cat_panel", None)
                st.rerun()

    # ---- 删除确认 ----
    if st.session_state.get("prodlib_del_panel") and n_sel:
        st.warning(
            f"确定删除选中的 {n_sel} 个产品？"
            "资料和优化结果都会一起删，不可恢复。"
        )

        d1, d2 = st.columns(2)

        if d1.button(
            "🗑 确认删除",
            type="primary",
            key="wb_del_apply",
            use_container_width=True,
        ):
            try:
                from services.product_lib import _prod_write

                _prod_write(
                    "/prod_del", "pids", sel_pids, label="删除失败"
                )
                st.session_state.pop("prodlib_del_panel", None)

                st.session_state[sel_state_key] = []

                _refresh_index(src_key)
                st.toast(f"🗑 已删除 {len(sel_pids)} 个产品")
                st.rerun()

            except Exception as exc:
                st.error(f"删除失败：{exc}")

        if d2.button(
            "取消",
            key="wb_del_cancel",
            use_container_width=True,
        ):
            st.session_state.pop("prodlib_del_panel", None)
            st.rerun()

    if cat_btn:
        st.session_state["prodlib_cat_panel"] = True
        st.rerun()

        return

    if del_btn:
        st.session_state["prodlib_del_panel"] = True
        st.rerun()

        return


# =====================================================
# 导出：库里的产品 → 优化结果 Excel
# =====================================================


def _export_pids(pids: list) -> io.BytesIO | None:
    """选中的产品 → 拼原资料表 + 用库里的 opt 造 profiles →
    export_unified 出优化 Excel。

    行号在拼接表里现场重算（第 pos+2 行），和 export_unified
    「Excel 行号 - 2 = pandas 行」的定位规则一致；多行资料的
    产品取第一行写 AI 结果。没有优化结果返回 None。"""
    frames = []
    profiles = []
    pos = 0

    for pid in pids:
        pid = str(pid)

        try:
            data = src_api(
                "/prod_list",
                _pl_payload({"pid": pid}),
                timeout=30,
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
            image = str(opt.get("image") or "")

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

    return ListingExporter.export_unified(dataframe, profiles)


# =====================================================
# 产品详情（AI 结果 / 资料 / 单品操作）
# =====================================================


def _render_wb_detail(src_key: str) -> None:
    read_only = bool(_scope_owner())

    index = st.session_state.get("prodlib_index") or {}
    items = index.get("items") or []

    if not items:
        return

    head = items[:300]

    labels = [
        f"{str(it.get('sku') or it.get('pid'))[:18]}｜"
        f"{_WB_STATUS.get(_wb_state(it), '')}｜"
        f"{str(it.get('title') or '')[:30]}"
        for it in head
    ]

    choice = st.selectbox(
        "📋 产品详情（AI 优化结果 / 完整资料 / 单品操作）",
        ["（选择产品…）"] + labels,
        key="wb_detail_sel",
    )

    if not choice or choice.startswith("（"):
        return

    it = head[labels.index(choice)]
    pid = str(it.get("pid"))

    try:
        data = src_api(
            "/prod_list",
            _pl_payload({"pid": pid}),
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
        opt = rec.get("opt") or {}
        raw_rows = rec.get("raw") or []

        col_a, col_b = st.columns([1, 2])

        with col_a:
            img = str(opt.get("image") or "") or str(
                rec.get("img") or ""
            )

            if img:
                try:
                    st.image(
                        img,
                        width=200,
                        caption=(
                            "优化首图"
                            if opt.get("image")
                            else "产品主图"
                        ),
                    )
                except Exception:
                    st.caption("（图片链接打不开）")

        with col_b:
            st.markdown(
                f"SKU：**{rec.get('sku') or '—'}**　"
                f"型号：**{rec.get('model') or '—'}**　"
                f"分类：**{rec.get('cat') or '未分类'}**　"
                f"资料：**{len(raw_rows)} 行**　"
                f"状态：**{_WB_STATUS.get(_wb_state(it), '')}**"
            )

            if opt:
                _when = f"上次优化：{_fmt_ms(opt.get('at'))}"

                if _pl_is_admin():
                    _when += (
                        f"（任务 "
                        f"{str(opt.get('task_id') or '')[:18]}）"
                    )

                st.caption(_when)

        if opt:
            st.markdown("##### 🤖 AI 优化结果")

            if opt.get("title"):
                st.markdown("**标题：** " + str(opt["title"]))

            if opt.get("short_title"):
                st.markdown(
                    "**短标题：** " + str(opt["short_title"])
                )

            bullets = list(opt.get("bullets") or [])

            if bullets:
                st.markdown("**五点：**")

                for b in bullets:
                    st.write("• " + str(b))

            highlights = list(opt.get("highlight") or [])

            if highlights:
                st.markdown("**商品亮点：**")

                for h in highlights:
                    st.write("• " + str(h))

            if opt.get("description"):
                st.markdown("**简介：**")

                st.write(str(opt["description"]))

            seo = list(opt.get("seo") or [])

            if seo:
                st.markdown(
                    "**SEO 关键词：** " + "、".join(
                        str(s) for s in seo[:10]
                    )
                )

        elif raw_rows:
            st.caption(
                "还没优化过。勾选后点「🚀 AI 优化」，结果会挂在这个"
                "产品上。"
            )

        else:
            st.warning(
                "⚠️ 资料不全：这个产品是旧库数据，没有存完整资料。"
                "重新导入同一份 Excel 即可补全（相同 SKU 自动合并）。"
            )

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
                        }
                        for v in variants
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

        if not read_only:
            e1, e2, e3 = st.columns([2, 1, 1])

            with e1:
                new_cat = st.text_input(
                    "改分类",
                    value=str(rec.get("cat") or ""),
                    key=f"wb_d_cat_{pid}",
                    max_chars=40,
                )

            with e2:
                if st.button(
                    "💾 保存分类",
                    key=f"wb_d_save_{pid}",
                    use_container_width=True,
                ):
                    try:
                        src_api(
                            "/prod_update",
                            _pl_payload(
                                {
                                    "updates": [
                                        {
                                            "pid": pid,
                                            "kw": "",
                                            "cat": new_cat.strip(),
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

            with e3:
                if st.button(
                    "🚀 优化这个产品",
                    key=f"wb_d_opt_{pid}",
                    type="primary",
                    use_container_width=True,
                ):
                    st.session_state["prodlib_selected"] = [pid]
                    st.rerun()

                    return

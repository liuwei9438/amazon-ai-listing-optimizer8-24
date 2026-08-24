from __future__ import annotations


import hashlib
import json
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
    load_status,
)
from services.task_control import save_control

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

from analyzer.title_strategy_generator import (
    TitleStrategyGenerator,
)

VERSION = "V2.4.4-Performance-3-Title-Completion"


TASK_RUNNING_STATUS = [
    "created",
    "running",
    "processing",
]



DEBUG_MODE = False



# =====================================================
# 页面配置
# =====================================================

st.set_page_config(
    page_title="Amazon AI Listing Optimizer",
    layout="wide",
)



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
# 内容展示
# =====================================================


def display_generated_content(
    profile
):


    title = profile.get(
        "generated_title",
        {}
    )


    if title.get(
        "title"
    ):

        st.write(
            "### AI标题"
        )


        st.write(
            title["title"]
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

        st.write(
            "### AI五点"
        )


        for item in bullets:

            st.write(
                "• "
                +
                str(item)
            )



    description = profile.get(
        "description_result",
        {}
    )


    if description.get(
        "description"
    ):

        st.write(
            "### AI详情"
        )


        st.write(
            description["description"]
        )



# =====================================================
# 页面主体
# =====================================================


st.title(
    "Amazon AI Listing Optimizer"
)


st.caption(
    VERSION
)


st.info(
    "基于 AI 商品理解生成标题、五点、详情和商品亮点。"
    "采用 Worker 后台任务模式，避免长任务导致页面阻塞。"
)



# =====================================================
# 上传文件
# =====================================================


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
        st.stop()


    st.success(
        f"读取成功："
        f"{len(envelope.records)} 个产品"
    )



    # =================================================
    # API KEY
    # =================================================


    saved_api_key = get_openai_api_key()


    manual_api_key = st.text_input(
        "OpenAI API Key",
        type="password",
    )


    api_key = (
        manual_api_key.strip()
        or
        saved_api_key
    )


    model = st.text_input(
        "模型",
        value="gpt-4.1-mini"
    )



    # =================================================
    # 优化模块选择
    # =================================================


    st.subheader(
        "优化内容选择"
    )


    enable_title = st.checkbox(
        "优化标题",
        True
    )


    enable_short_title = st.checkbox(
        "优化短标题",
        True
    )


    enable_highlight = st.checkbox(
        "优化商品亮点",
        True
    )


    enable_bullet = st.checkbox(
        "优化五点描述",
        True
    )


    enable_description = st.checkbox(
        "优化详情描述",
        True
    )


    enable_seo = st.checkbox(
        "优化SEO关键词",
        True
    )



    # =================================================
    # 开始任务
    # =================================================


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
        "开始 AI 商品理解",
        type="primary",
        disabled=button_disabled,
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

            # Internal safe default for product-level concurrency.
            "max_workers":
                4,

        }



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

# =====================================================
# 当前任务状态
# =====================================================


profiles = []


if current_task:


    status = load_status(
        current_task
    )


    profiles = load_profiles(
        current_task
    )


    if DEBUG_MODE:
        st.caption(
            f"DEBUG: task={current_task}, success={len(profiles)}"
        )


    if status:


        st.subheader(
            "任务状态"
        )
        if st.button("刷新任务状态"):

            st.session_state.pop(
                "current_task",
                None
            )
        
            st.rerun()
        col1, col2, col3 = st.columns(3)
        
        
        with col1:
        
            if st.button(
                "暂停任务"
            ):
        
                save_control(
                    current_task,
                    "pause"
                )
        
                st.warning(
                    "暂停请求已发送"
                )
        
        
        
        with col2:
        
            if st.button(
                "继续任务"
            ):
        
                save_control(
                    current_task,
                    "running"
                )
        
                st.success(
                    "继续请求已发送"
                )
        
        
        
        with col3:
        
            if st.button(
                "取消任务"
            ):
        
                save_control(
                    current_task,
                    "cancel"
                )
        
                st.error(
                    "取消请求已发送"
                )
        status_value = status.get(
            "status",
            ""
        )


        message = status.get(
            "message",
            ""
        )


        completed = status.get(
            "completed",
            0
        )


        total = (
            status.get("total")
            or
            status.get("total_products")
            or
            0
        )


        success_count = status.get("success", len(profiles))
        failed_count = status.get("failed", 0)

        st.info(
            f"""
状态：

{status_value}


消息：

{message}


进度：

{completed}
/
{total}

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
# =====================================================
# 显示优化结果
# =====================================================

failed_items = load_failed_items(
    current_task
) if current_task else []

if DEBUG_MODE:
    st.caption(
        f"DEBUG: success={len(profiles)} / failed={len(failed_items)}"
    )


# =====================================================
# Title Strategy 测试
# 临时验证 AI 标题策略能力
# =====================================================

with st.expander(
    "Title Strategy 测试"
):

    # =============================================
    # 必须先确认已经有优化结果
    # =============================================

    if not profiles:

        st.info(
            "暂无可测试的产品，请先完成至少 1 个产品的 AI 商品理解。"
        )

    else:

        # =============================================
        # 测试产品选择
        #
        # 默认：
        # 有 3 个及以上产品 → 使用第 3 个产品
        # 不足 3 个产品 → 使用当前最后一个已完成产品
        #
        # 这样不会再出现 profiles[2] IndexError。
        # =============================================

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

                api_key = get_openai_api_key()


                if not api_key:

                    st.error(
                        "未找到 OpenAI API Key"
                    )

                else:

                    strategy_result = (
                        TitleStrategyGenerator.generate(
                            test_profile,
                            api_key,
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

# =====================================================
# V2.4.4 Failure Observability
#
# A failed product must be visible as a terminal result.
# Do not hide failure diagnostics inside profiles-only JSON.
# =====================================================

terminal_success = len(profiles)
terminal_failed = len(failed_items)
terminal_completed = terminal_success + terminal_failed

if current_task:
    current_status = load_status(current_task) or {}
    expected_total = (
        current_status.get("total")
        or current_status.get("total_products")
        or 0
    )
else:
    current_status = {}
    expected_total = 0

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

if failed_items:
    with st.expander(
        f"失败产品（{len(failed_items)}）— 点击查看真实错误",
        expanded=True,
    ):
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
            "status": current_status,
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
if profiles:


    st.success(
        f"已完成 {len(profiles)} 个产品优化"
    )


    st.subheader(
        "AI优化结果预览"
    )



    # 默认展示前3个，避免页面卡顿

    for index, profile in enumerate(
        profiles[:3]
    ):


        with st.expander(
            f"产品 {index + 1}"
        ):


            display_generated_content(
                profile
            )



    # =================================================
    # 导出 JSON
    # =================================================


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
        "status": current_status,
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



    # =================================================
    # 导出 Excel
    # =================================================


    st.subheader(
        "AI优化结果导出"
    )


    try:


       if uploaded is not None:

            optimized_export = ListingExporter.export(
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
    
                "导出 AI 优化结果",
    
    
                data=optimized_data,
    
    
                file_name=
                f"{safe_stem}_{VERSION}_AI优化结果.xlsx",
    
    
                mime=
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    
    
                type="primary",
    
            )
    


    except Exception as exc:


        st.error(
            f"生成优化文件失败：{exc}"
        )



# =====================================================
# 原文件完整性测试
# =====================================================


if uploaded is not None:


    st.subheader(
        "原文件完整性导出"
    )


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

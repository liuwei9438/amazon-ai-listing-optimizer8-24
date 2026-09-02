from __future__ import annotations

import copy
import os
import shutil
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from services.batch_processor import process_batch
from services.api_metrics import load_api_metrics, reset_api_context, set_api_context
from services.result_storage import (
    load_failed_items,
    load_profiles,
    reconcile_task_results,
    save_failed_items,
    save_profiles,
)
from services.task_control import load_control, save_control
from services.task_manager import get_task_dir, save_status
from image.image_pipeline import optimize_record_images


DEFAULT_MAX_WORKERS = 4
MAX_ALLOWED_WORKERS = 8

# Whole-product recovery layer.
#
# AI Runtime already retries an individual API request.  This additional
# layer retries the complete one-product pipeline only when that product
# produced no success profile.  It protects the batch against transient
# JSON/schema/storage/network failures that escape a single stage.
DEFAULT_PRODUCT_MAX_ATTEMPTS = 2
MAX_PRODUCT_MAX_ATTEMPTS = 3
PRODUCT_RETRY_DELAY_SECONDS = 0.8


def _resolve_max_workers(options: dict | None) -> int:
    raw = options.get("max_workers") if isinstance(options, dict) else None
    if raw in (None, ""):
        raw = os.getenv("AI_MAX_WORKERS", DEFAULT_MAX_WORKERS)
    try:
        workers = int(raw)
    except (TypeError, ValueError):
        workers = DEFAULT_MAX_WORKERS
    return max(1, min(workers, MAX_ALLOWED_WORKERS))


def _resolve_product_max_attempts(options: dict | None) -> int:
    raw = (
        options.get("product_max_attempts")
        if isinstance(options, dict)
        else None
    )
    if raw in (None, ""):
        raw = DEFAULT_PRODUCT_MAX_ATTEMPTS
    try:
        attempts = int(raw)
    except (TypeError, ValueError):
        attempts = DEFAULT_PRODUCT_MAX_ATTEMPTS
    return max(1, min(attempts, MAX_PRODUCT_MAX_ATTEMPTS))


def _child_task_id(task_id: str, index: int, attempt: int = 1) -> str:
    return f"{task_id}/workers/item_{index:05d}/attempt_{attempt:02d}"


def _run_one_record(*, record, index, parent_task_id, api_key, model, options):
    """Run one product with an explicit terminal-outcome guarantee.

    Each product receives a small whole-pipeline retry budget.  A retry is
    used only when the previous attempt produced no success profile.  This is
    intentionally above the per-API retry layer: malformed AI JSON, transient
    schema output, child-result read/write issues, or an escaped stage error
    can otherwise cause an otherwise valid product to fail the whole pipeline.

    The function always returns exactly one terminal result:
    - profile != None  -> success
    - failed != None   -> explicit failure
    It never silently returns an unresolved item.
    """
    started = time.time()
    context_tokens = set_api_context(parent_task_id, index)
    max_attempts = _resolve_product_max_attempts(options)

    last_failed = None

    try:
        for attempt in range(1, max_attempts + 1):
            child_id = _child_task_id(parent_task_id, index, attempt)
            save_control(child_id, "running")

            try:
                process_batch([record], child_id, api_key, model, options)

                profiles = load_profiles(child_id)
                failed_items = load_failed_items(child_id)

                profile = profiles[0] if profiles else None
                failed = failed_items[0] if failed_items else None

                if profile is not None:
                    if bool(options.get("optimize_images", False)):
                        profile = dict(profile)
                        profile["image_result"] = optimize_record_images(record, profile)
                    return {
                        "index": index,
                        "profile": profile,
                        "failed": None,
                        "attempts": attempt,
                        "recovered_after_retry": attempt > 1,
                        "elapsed": round(time.time() - started, 2),
                    }

                if failed is None:
                    failed = {
                        "index": index,
                        "source_row_index": getattr(record, "row_number", None),
                        "sku": getattr(record, "sku", ""),
                        "title": getattr(record, "title", ""),
                        "error": "并发子任务结束但没有产生成功或失败结果",
                        "error_type": "unresolved_concurrent_item",
                    }

                if isinstance(failed, dict):
                    failed = dict(failed)
                    failed["index"] = index
                    failed["source_row_index"] = getattr(
                        record, "row_number", None
                    )
                    failed["attempt"] = attempt
                    failed["max_attempts"] = max_attempts

                last_failed = failed

            except Exception as exc:
                last_failed = {
                    "index": index,
                    "source_row_index": getattr(record, "row_number", None),
                    "sku": getattr(record, "sku", ""),
                    "title": getattr(record, "title", ""),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                }

            finally:
                _cleanup_child(parent_task_id, index, attempt)

            # Retry only after a complete product attempt failed.
            if attempt < max_attempts:
                time.sleep(PRODUCT_RETRY_DELAY_SECONDS)

        if not isinstance(last_failed, dict):
            last_failed = {
                "index": index,
                "source_row_index": getattr(record, "row_number", None),
                "sku": getattr(record, "sku", ""),
                "title": getattr(record, "title", ""),
                "error": "商品在所有并发重试后仍未产生终态结果",
                "error_type": "unresolved_after_product_retry",
            }

        last_failed = dict(last_failed)
        last_failed["attempts"] = max_attempts
        last_failed["product_retry_exhausted"] = True

        return {
            "index": index,
            "profile": None,
            "failed": last_failed,
            "attempts": max_attempts,
            "recovered_after_retry": False,
            "elapsed": round(time.time() - started, 2),
        }

    finally:
        reset_api_context(context_tokens)


def _cleanup_child(parent_task_id: str, index: int, attempt: int = 1):
    try:
        shutil.rmtree(
            get_task_dir(_child_task_id(parent_task_id, index, attempt)),
            ignore_errors=True,
        )
    except OSError:
        pass


# =====================================================
# 同源变体合并（V2.6.2）
#
# 采集表里多变体产品每个变体各占一行，但标题/五点/简介完全相同
#（只有颜色、图片、SKU 不同）。AI 的输入不含图片和颜色，这些行
# 跑完整 3 步 AI 只会得到一模一样的结果——15 个变体就花 15 倍
# token 和时间。
#
# 方案：按「标题+五点+简介+短标题+语言」计算内容指纹，指纹相同的
# 行只对第一行（代表行）跑 AI，其余行克隆代表行的结果，仅替换
# 各自行份字段（SKU/父SKU/行号）。
# =====================================================


def _source_content_key(record) -> tuple:
    """喂给 AI 的全部文本源字段构成的内容指纹。"""
    return (
        getattr(record, "title", "") or "",
        tuple(getattr(record, "bullets", ()) or ()),
        getattr(record, "description", "") or "",
        getattr(record, "short_title", "") or "",
        getattr(record, "language", "") or "",
    )


def _build_variant_groups(records) -> tuple[dict, set]:
    """返回 (members, duplicate_indices)。

    members: 代表行 index -> 同指纹成员行 index 列表（不含代表行自身）
    duplicate_indices: 所有非代表行的 index 集合（这些行不提交 AI）
    """
    representative_of: dict[tuple, int] = {}
    members: dict[int, list[int]] = {}
    duplicate_indices: set[int] = set()

    for index, record in enumerate(records):
        key = _source_content_key(record)
        rep = representative_of.get(key)
        if rep is None:
            representative_of[key] = index
        else:
            members.setdefault(rep, []).append(index)
            duplicate_indices.add(index)

    return members, duplicate_indices


def _clone_profile_for_record(profile: dict, record, cloned_from_index: int) -> dict:
    """深拷贝代表行结果并替换为成员行自己的身份字段。"""
    clone = copy.deepcopy(profile)
    source_identity = clone.get("source_identity")
    if not isinstance(source_identity, dict):
        source_identity = {}
    source_identity["sku"] = getattr(record, "sku", "")
    source_identity["parent_sku"] = getattr(record, "parent_sku", "")
    source_identity["source_row_index"] = getattr(record, "row_number", None)
    clone["source_identity"] = source_identity
    clone["variant_dedupe"] = {"cloned_from_index": cloned_from_index}
    return clone


def _clone_failed_for_record(failed: dict, record, cloned_from_index: int) -> dict:
    """克隆失败结果并替换为成员行自己的身份字段（保留真实错误信息）。"""
    clone = dict(failed)
    clone["index"] = None  # 由调用方按成员 index 覆盖
    clone["source_row_index"] = getattr(record, "row_number", None)
    clone["sku"] = getattr(record, "sku", "")
    clone["title"] = getattr(record, "title", "")
    clone["variant_dedupe"] = {"cloned_from_index": cloned_from_index}
    return clone


def process_batch_concurrent(
    records,
    task_id,
    api_key,
    model="gpt-4.1-mini",
    options=None,
):
    """Bounded product-level concurrency while preserving per-product ordering."""
    if options is None:
        options = {}

    total = len(records)
    max_workers = min(_resolve_max_workers(options), max(total, 1))

    # V2.6.2 同源变体合并：默认开启，可用 options["variant_dedupe"]=False 关闭。
    variant_dedupe_enabled = options.get("variant_dedupe", True)
    if variant_dedupe_enabled:
        variant_members, duplicate_indices = _build_variant_groups(records)
    else:
        variant_members, duplicate_indices = {}, set()
    ai_rows = total - len(duplicate_indices)

    if total == 0:
        save_status(
            task_id,
            {
                "task_id": task_id,
                "status": "completed",
                "message": "没有需要处理的商品",
                "completed": 0,
                "total": 0,
                "success": 0,
                "failed": 0,
                "max_workers": 0,
            },
        )
        return []

    profiles_by_index = {}
    failed_by_index = {}
    next_index = 0
    futures = {}
    cancelled = False

    save_status(
        task_id,
        {
            "task_id": task_id,
            "status": "processing",
            "message": (
                f"并发优化启动：{max_workers} 个产品 Worker"
                + (
                    f"；同源变体合并：{total} 行只跑 {ai_rows} 次 AI"
                    if duplicate_indices
                    else ""
                )
            ),
            "completed": 0,
            "total": total,
            "success": 0,
            "failed": 0,
            "max_workers": max_workers,
        },
    )

    def persist_parent_results():
        ordered_profiles = [
            profiles_by_index[i] for i in sorted(profiles_by_index)
        ]
        ordered_failed = [
            failed_by_index[i] for i in sorted(failed_by_index)
        ]
        save_profiles(task_id, ordered_profiles)
        save_failed_items(task_id, ordered_failed)
        return ordered_profiles, ordered_failed

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="listing-product",
    ) as executor:
        while next_index < total or futures:
            action = load_control(task_id)

            if action == "cancel":
                cancelled = True

            if action == "pause" and not cancelled:
                save_status(
                    task_id,
                    {
                        "status": "paused",
                        "message": "任务已暂停；运行中的商品会先安全结束，不再提交新商品",
                        "completed": len(profiles_by_index) + len(failed_by_index),
                        "total": total,
                        "success": len(profiles_by_index),
                        "failed": len(failed_by_index),
                        "in_flight": len(futures),
                    },
                )

            while (
                not cancelled
                and action != "pause"
                and next_index < total
                and len(futures) < max_workers
            ):
                # V2.6.2 同源变体合并：非代表行不提交 AI，
                # 等代表行完成后直接克隆结果。
                if next_index in duplicate_indices:
                    next_index += 1
                    continue

                record = records[next_index]
                future = executor.submit(
                    _run_one_record,
                    record=record,
                    index=next_index,
                    parent_task_id=task_id,
                    api_key=api_key,
                    model=model,
                    options=options,
                )
                futures[future] = next_index
                next_index += 1

            if not futures:
                if cancelled:
                    break
                if action == "pause":
                    time.sleep(0.4)
                    continue
                if next_index >= total:
                    break
                continue

            done, _ = wait(
                list(futures),
                timeout=0.5,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue

            for future in done:
                index = futures.pop(future)

                try:
                    result = future.result()
                except Exception as future_exc:
                    # Last-resort parent-side guard.  Even a catastrophic
                    # Future exception becomes an explicit failed terminal
                    # result instead of disappearing from the batch.
                    record = records[index]
                    result = {
                        "index": index,
                        "profile": None,
                        "failed": {
                            "index": index,
                            "source_row_index": getattr(
                                record, "row_number", None
                            ),
                            "sku": getattr(record, "sku", ""),
                            "title": getattr(record, "title", ""),
                            "error": str(future_exc),
                            "error_type": type(future_exc).__name__,
                            "future_exception": True,
                        },
                    }

                if result.get("profile") is not None:
                    profiles_by_index[index] = result["profile"]
                    failed_by_index.pop(index, None)

                    # V2.6.2 同源变体合并：把代表行结果克隆给同指纹成员行。
                    for member_index in variant_members.get(index, []):
                        member_record = records[member_index]
                        clone = _clone_profile_for_record(
                            result["profile"],
                            member_record,
                            cloned_from_index=index,
                        )
                        if bool(options.get("optimize_images", False)):
                            clone["image_result"] = optimize_record_images(
                                member_record, clone
                            )
                        profiles_by_index[member_index] = clone
                        failed_by_index.pop(member_index, None)
                else:
                    failed = result.get("failed")
                    if not isinstance(failed, dict):
                        record = records[index]
                        failed = {
                            "index": index,
                            "source_row_index": getattr(
                                record, "row_number", None
                            ),
                            "sku": getattr(record, "sku", ""),
                            "title": getattr(record, "title", ""),
                            "error": "Future完成但未返回有效成功或失败结果",
                            "error_type": "invalid_future_terminal_result",
                        }
                    failed_by_index[index] = failed

                    # V2.6.2 同源变体合并：代表行失败时，同指纹成员行
                    # 记录同一失败（保留真实错误信息，重试时可再跑）。
                    for member_index in variant_members.get(index, []):
                        member_record = records[member_index]
                        member_failed = _clone_failed_for_record(
                            failed,
                            member_record,
                            cloned_from_index=index,
                        )
                        member_failed["index"] = member_index
                        failed_by_index[member_index] = member_failed
            ordered_profiles, ordered_failed = persist_parent_results()
            completed = len(ordered_profiles) + len(ordered_failed)

            api_metrics = load_api_metrics(task_id)
            save_status(
                task_id,
                {
                    "task_id": task_id,
                    "status": "processing" if not cancelled else "cancelling",
                    "message": (
                        f"并发处理中：{completed}/{total}，"
                        f"运行中 {len(futures)}，并发 {max_workers}"
                    ),
                    "completed": completed,
                    "total": total,
                    "success": len(ordered_profiles),
                    "failed": len(ordered_failed),
                    "in_flight": len(futures),
                    "max_workers": max_workers,
                    "api_calls": api_metrics.get("total_calls", 0),
                    "api_attempts": api_metrics.get("total_attempts", 0),
                    "api_retries": api_metrics.get("retry_attempts", 0),
                },
            )

    persist_parent_results()

    if cancelled:
        reconciliation = reconcile_task_results(
            task_id,
            records,
            unresolved_error="任务已取消，该商品尚未开始处理",
        )
        api_metrics = load_api_metrics(task_id)
        save_status(
            task_id,
            {
                "task_id": task_id,
                "status": "cancelled",
                "message": "任务已取消",
                "completed": reconciliation["completed"],
                "total": total,
                "success": reconciliation["success"],
                "failed": reconciliation["failed"],
                "in_flight": 0,
                "max_workers": max_workers,
                "api_calls": api_metrics.get("total_calls", 0),
                "api_attempts": api_metrics.get("total_attempts", 0),
                "api_retries": api_metrics.get("retry_attempts", 0),
            },
        )
        return reconciliation["profiles"]

    reconciliation = reconcile_task_results(
        task_id,
        records,
        unresolved_error="并发任务未产生成功或失败结果，已由闭环检查记录为失败",
    )
    api_metrics = load_api_metrics(task_id)
    save_status(
        task_id,
        {
            "task_id": task_id,
            "status": "completed",
            "message": "任务完成",
            "completed": reconciliation["completed"],
            "total": total,
            "success": reconciliation["success"],
            "failed": reconciliation["failed"],
            "in_flight": 0,
            "max_workers": max_workers,
            "api_calls": api_metrics.get("total_calls", 0),
            "api_attempts": api_metrics.get("total_attempts", 0),
            "api_retries": api_metrics.get("retry_attempts", 0),
        },
    )
    return reconciliation["profiles"]

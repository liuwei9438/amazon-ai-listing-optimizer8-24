from __future__ import annotations

from services.task_manager import get_task_dir
from services.json_storage import load_json, save_json


def get_result_path(task_id: str):
    """获取成功结果文件路径。"""
    return get_task_dir(task_id) / "profiles.json"


def get_failed_path(task_id: str):
    """获取失败结果文件路径。"""
    return get_task_dir(task_id) / "failed.json"


def save_profiles(task_id: str, profiles: list):
    """原子保存已经成功生成的产品结果。"""
    save_json(get_result_path(task_id), profiles)


def load_profiles(task_id: str):
    """安全读取任务结果；文件不存在或暂时不可读时返回空列表。"""
    if not task_id:
        return []

    data = load_json(get_result_path(task_id), default=[])
    return data if isinstance(data, list) else []


def save_failed_items(task_id: str, failed_items: list):
    """原子保存失败产品。"""
    save_json(get_failed_path(task_id), failed_items)


def load_failed_items(task_id: str):
    """安全读取失败产品；文件不存在或暂时不可读时返回空列表。"""
    if not task_id:
        return []

    data = load_json(get_failed_path(task_id), default=[])
    return data if isinstance(data, list) else []



def _profile_source_row(profile: dict):
    if not isinstance(profile, dict):
        return None
    source_identity = profile.get("source_identity", {})
    if not isinstance(source_identity, dict):
        return None
    return source_identity.get("source_row_index")


def _failed_source_row(item: dict):
    if not isinstance(item, dict):
        return None
    return item.get("source_row_index")


def reconcile_task_results(
    task_id: str,
    records,
    *,
    unresolved_error: str = "任务在商品完成结算前中断",
):
    """Ensure every input record has one terminal outcome.

    Successful profiles are matched by ``source_identity.source_row_index``.
    Failed records are matched by ``source_row_index``.  Any record that is in
    neither set is persisted as an explicit failed item instead of silently
    disappearing (the historical 49/50 failure mode).

    The function is intentionally idempotent so a worker can safely call it
    during fatal-error cleanup or final task reconciliation.
    """
    profiles = load_profiles(task_id)
    failed_items = load_failed_items(task_id)

    success_rows = {
        _profile_source_row(profile)
        for profile in profiles
        if _profile_source_row(profile) is not None
    }
    failed_rows = {
        _failed_source_row(item)
        for item in failed_items
        if _failed_source_row(item) is not None
    }

    # Backward compatibility for failed records created before source_row_index
    # was added: their loop index is still reliable within the same task.
    failed_indexes = {
        item.get("index")
        for item in failed_items
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }

    added = 0
    for index, record in enumerate(records):
        row_number = getattr(record, "row_number", None)
        if row_number in success_rows:
            continue
        if row_number in failed_rows or index in failed_indexes:
            continue

        failed_items.append(
            {
                "index": index,
                "source_row_index": row_number,
                "sku": getattr(record, "sku", ""),
                "title": getattr(record, "title", ""),
                "error": unresolved_error,
                "error_type": "unresolved_task_item",
            }
        )
        failed_rows.add(row_number)
        failed_indexes.add(index)
        added += 1

    if added:
        save_failed_items(task_id, failed_items)

    return {
        "profiles": profiles,
        "failed_items": failed_items,
        "success": len(profiles),
        "failed": len(failed_items),
        "completed": len(profiles) + len(failed_items),
        "total": len(records),
        "added_unresolved": added,
    }

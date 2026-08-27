from __future__ import annotations

import contextvars
import threading
from datetime import datetime

from services.json_storage import load_json, save_json
from services.task_manager import get_task_dir

_task_id_var = contextvars.ContextVar("ai_task_id", default=None)
_product_index_var = contextvars.ContextVar("ai_product_index", default=None)
_METRICS_LOCK = threading.RLock()


def set_api_context(task_id: str | None, product_index: int | None = None):
    token_task = _task_id_var.set(task_id)
    token_index = _product_index_var.set(product_index)
    return token_task, token_index


def reset_api_context(tokens):
    token_task, token_index = tokens
    _task_id_var.reset(token_task)
    _product_index_var.reset(token_index)


def _path(task_id: str):
    return get_task_dir(task_id) / "api_metrics.json"


def record_logical_call(
    stage: str,
    *,
    success: bool,
    elapsed: float,
    attempts: int,
    error: str = "",
):
    task_id = _task_id_var.get()
    if not task_id:
        return

    stage = stage or "unknown"
    with _METRICS_LOCK:
        data = load_json(_path(task_id), default={})
        if not isinstance(data, dict):
            data = {}

        data.setdefault("task_id", task_id)
        data.setdefault("total_calls", 0)
        data.setdefault("total_attempts", 0)
        data.setdefault("success_calls", 0)
        data.setdefault("failed_calls", 0)
        data.setdefault("retry_attempts", 0)
        data.setdefault("total_elapsed_seconds", 0.0)
        data.setdefault("stages", {})

        data["total_calls"] += 1
        data["total_attempts"] += max(int(attempts), 1)
        data["retry_attempts"] += max(int(attempts) - 1, 0)
        data["total_elapsed_seconds"] = round(
            float(data.get("total_elapsed_seconds", 0.0)) + float(elapsed), 2
        )
        data["updated_at"] = datetime.now().isoformat()

        if success:
            data["success_calls"] += 1
        else:
            data["failed_calls"] += 1

        stages = data["stages"]
        stats = stages.setdefault(stage, {
            "calls": 0,
            "attempts": 0,
            "success": 0,
            "failed": 0,
            "retries": 0,
            "elapsed_seconds": 0.0,
        })
        stats["calls"] += 1
        stats["attempts"] += max(int(attempts), 1)
        stats["retries"] += max(int(attempts) - 1, 0)
        stats["elapsed_seconds"] = round(
            float(stats.get("elapsed_seconds", 0.0)) + float(elapsed), 2
        )
        if success:
            stats["success"] += 1
        else:
            stats["failed"] += 1
            stats["last_error"] = str(error)[:500]

        product_index = _product_index_var.get()
        if product_index is not None:
            data["last_product_index"] = product_index

        save_json(_path(task_id), data)


def load_api_metrics(task_id: str) -> dict:
    data = load_json(_path(task_id), default={})
    return data if isinstance(data, dict) else {}

from pathlib import Path

from services.json_storage import load_json, save_json


TASK_FILE = Path("current_task.json")


def save_current_task(task_id):
    save_json(TASK_FILE, {"task_id": task_id})


def load_current_task():
    data = load_json(TASK_FILE, default={})
    if not isinstance(data, dict):
        return ""
    return data.get("task_id", "")


def clear_current_task():
    """Remove the persisted current-task pointer without deleting task results."""
    try:
        TASK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

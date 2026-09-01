from pathlib import Path

from services.json_storage import load_json, save_json


def control_path(task_id):
    return Path("tasks") / task_id / "control.json"


def save_control(task_id, action):
    save_json(control_path(task_id), {"action": action})


def load_control(task_id):
    data = load_json(control_path(task_id), default={})
    if not isinstance(data, dict):
        return "running"
    return data.get("action", "running")

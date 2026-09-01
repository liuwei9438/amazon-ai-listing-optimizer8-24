from __future__ import annotations


import uuid
import threading

from datetime import datetime
from pathlib import Path

from services.task_control import save_control
from services.json_storage import load_json, save_json


TASK_ROOT = Path("tasks")

# Protect the read-modify-write status cycle inside this app process.
# Atomic file replacement prevents torn JSON; this lock prevents lost updates
# once product-level concurrency is introduced in the next performance phase.
_STATUS_LOCK = threading.RLock()



def ensure_task_root():

    TASK_ROOT.mkdir(
        parents=True,
        exist_ok=True
    )



def create_task(
    total_products: int,
    filename: str,
):


    ensure_task_root()


    task_id = (
        datetime.now()
        .strftime("%Y%m%d_%H%M%S")
        +
        "_"
        +
        uuid.uuid4()
        .hex[:6]
    )


    task_dir = TASK_ROOT / task_id


    task_dir.mkdir(
        parents=True,
        exist_ok=True
    )



    status = {

        "task_id":
            task_id,


        "filename":
            filename,


        # 统一字段
        "total":
            total_products,


        "completed":
            0,


        "success":
            0,


        "failed":
            0,


        "status":
            "created",


        "message":
            "任务已创建",


        "created_at":
            datetime.now()
            .isoformat(),

    }


    save_json(
        task_dir / "status.json",
        status
    )
    save_control(
        task_id,
        "running"
    )

    return task_id





def get_task_dir(
    task_id: str
):


    return (
        TASK_ROOT
        /
        task_id
    )





def save_status(
    task_id: str,
    status: dict
):


    task_dir = get_task_dir(
        task_id
    )


    with _STATUS_LOCK:

        old_status = load_status(
            task_id
        )


        if old_status:

            old_status.update(
                status
            )

            status = old_status



        save_json(
            task_dir / "status.json",
            status
        )





def load_status(
    task_id: str
):
    path = get_task_dir(task_id) / "status.json"
    data = load_json(path, default={})
    return data if isinstance(data, dict) else {}




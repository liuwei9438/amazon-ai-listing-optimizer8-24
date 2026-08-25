from __future__ import annotations

import threading
import traceback


from services.concurrent_batch_processor import process_batch_concurrent
from services.task_manager import load_status, save_status
from services.result_storage import reconcile_task_results



def run_task(
    records,
    task_id,
    api_key,
    model,
    options,
):
    """
    后台执行任务
    """

    try:

        save_status(
            task_id,
            {
                "task_id": task_id,
                "status": "running",
                "message": "AI任务开始",
                "completed":0,
                "total":len(records),
            }
        )
        save_status(
            task_id,
            {
                "task_id": task_id,
                "status": "processing",
                "message": "进入批量处理阶段",
                "completed": 0,
                "total": len(records),
            }
        )
        save_status(
            task_id,
            {
                "status": "processing",
                "message": "进入batch_processor",
                "completed":0,
                "total":len(records),
            }
        )
        
        profiles = process_batch_concurrent(
            records,
            task_id,
            api_key,
            model,
            options,
        )
        # process_batch owns the final batch state.  Do not overwrite
        # cancelled/paused/failed states after it returns.
        final_status = load_status(task_id)
        if final_status.get("status") not in {
            "completed",
            "cancelled",
            "paused",
            "failed",
        }:
            save_status(
                task_id,
                {
                    "task_id": task_id,
                    "status": "completed",
                    "message": "任务完成",
                    "completed": len(profiles),
                    "total": len(records),
                }
            )


    except Exception as e:


        # A fatal worker-level exception must not leave an invisible product.
        # Reconcile all records so every input has either a success profile or
        # an explicit failure entry before the task is marked failed.
        reconciliation = reconcile_task_results(
            task_id,
            records,
            unresolved_error=f"后台任务异常中断：{e}",
        )


        save_status(
            task_id,
            {
                "task_id": task_id,
                "status": "failed",
                "message": str(e),
                "traceback": traceback.format_exc(),
                "completed": reconciliation["completed"],
                "total": reconciliation["total"],
                "success": reconciliation["success"],
                "failed": reconciliation["failed"],
            }
        )



def start_worker(
    records,
    task_id,
    api_key,
    model,
    options,
):

    thread = threading.Thread(

        target=run_task,

        args=(
            records,
            task_id,
            api_key,
            model,
            options,
        ),

        daemon=True,

    )


    thread.start()


    return thread

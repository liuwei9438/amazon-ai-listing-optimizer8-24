from dataclasses import dataclass
from pathlib import Path

import services.task_manager as task_manager
from services.result_storage import (
    load_failed_items,
    reconcile_task_results,
    save_failed_items,
    save_profiles,
)


@dataclass
class FakeRecord:
    row_number: int
    sku: str
    title: str


def test_reconcile_closes_missing_item(tmp_path):
    original_root = task_manager.TASK_ROOT
    task_manager.TASK_ROOT = Path(tmp_path) / "tasks"
    try:
        task_id = task_manager.create_task(5, "test.xlsx")
        records = [
            FakeRecord(i + 2, f"SKU-{i + 1}", f"Product {i + 1}")
            for i in range(5)
        ]

        save_profiles(
            task_id,
            [
                {"source_identity": {"source_row_index": 2}},
                {"source_identity": {"source_row_index": 3}},
                {"source_identity": {"source_row_index": 4}},
            ],
        )
        save_failed_items(
            task_id,
            [
                {
                    "index": 3,
                    "source_row_index": 5,
                    "sku": "SKU-4",
                    "error": "known failure",
                }
            ],
        )

        result = reconcile_task_results(task_id, records)

        assert result["success"] == 3
        assert result["failed"] == 2
        assert result["completed"] == 5
        assert result["total"] == 5
        assert result["added_unresolved"] == 1
        assert load_failed_items(task_id)[-1]["sku"] == "SKU-5"
    finally:
        task_manager.TASK_ROOT = original_root


def test_reconcile_is_idempotent(tmp_path):
    original_root = task_manager.TASK_ROOT
    task_manager.TASK_ROOT = Path(tmp_path) / "tasks"
    try:
        task_id = task_manager.create_task(1, "test.xlsx")
        records = [FakeRecord(2, "SKU-1", "Product 1")]

        first = reconcile_task_results(task_id, records)
        second = reconcile_task_results(task_id, records)

        assert first["added_unresolved"] == 1
        assert second["added_unresolved"] == 0
        assert len(load_failed_items(task_id)) == 1
    finally:
        task_manager.TASK_ROOT = original_root

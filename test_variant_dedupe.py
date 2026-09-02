# -*- coding: utf-8 -*-
"""V2.6.2 同源变体合并优化 测试"""
import io
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


# ============================================================
print("== A. 内容指纹分组 ==")
from services.concurrent_batch_processor import (
    _source_content_key,
    _build_variant_groups,
    _clone_profile_for_record,
)
from core.models import ProductRecord


def make_record(row, sku, title, bullets, desc, parent="P1"):
    return ProductRecord(
        row_number=row,
        sku=sku,
        parent_sku=parent,
        title=title,
        bullets=tuple(bullets),
        description=desc,
    )


rec_a1 = make_record(2, "P1-Red", "Motor for Printer X", ["b1", "b2"], "Same desc")
rec_a2 = make_record(3, "P1-Blue", "Motor for Printer X", ["b1", "b2"], "Same desc")
rec_a3 = make_record(4, "P1-Green", "Motor for Printer X", ["b1", "b2"], "Same desc")
rec_b = make_record(5, "P2", "Filter Kit Y", ["c1"], "Other desc", parent="P2")
rec_c = make_record(6, "P3", "Nozzle Z", [], "", parent="P3")

members, dup = _build_variant_groups([rec_a1, rec_a2, rec_a3, rec_b, rec_c])
check("A1 三个同源变体归为一组", members.get(0) == [1, 2], members)
check("A2 非代表行集合正确", dup == {1, 2}, dup)
check("A3 不同内容不合并", 3 not in dup and 4 not in dup, dup)
check("A4 指纹忽略图片/SKU差异",
      _source_content_key(rec_a1) == _source_content_key(
          ProductRecord(row_number=99, sku="OTHER", title="Motor for Printer X",
                        bullets=("b1", "b2"), description="Same desc",
                        image_urls=("https://x/1.jpg",))))

# ============================================================
print("== B. 克隆替换身份字段 ==")
profile = {
    "source_identity": {"sku": "P1-Red", "parent_sku": "P1", "source_row_index": 2},
    "generated_title": {"title": "Optimized Motor Title"},
    "bullet_result": {"bullets": ["F1", "F2"]},
}
clone = _clone_profile_for_record(profile, rec_a2, cloned_from_index=0)
check("B1 克隆换 SKU", clone["source_identity"]["sku"] == "P1-Blue", clone["source_identity"])
check("B2 克隆换行号", clone["source_identity"]["source_row_index"] == 3, clone["source_identity"])
check("B3 克隆保留父SKU", clone["source_identity"]["parent_sku"] == "P1")
check("B4 克隆保留生成内容", clone["generated_title"]["title"] == "Optimized Motor Title")
check("B5 克隆是深拷贝", clone["bullet_result"] is not profile["bullet_result"])
check("B6 克隆带来源标记", clone.get("variant_dedupe", {}).get("cloned_from_index") == 0)
check("B7 原profile不被改", profile["source_identity"]["sku"] == "P1-Red")

# ============================================================
print("== C. 端到端：并发批处理只对代表行跑 AI ==")
import services.concurrent_batch_processor as cbp
from services.result_storage import save_profiles
from services.task_manager import get_task_dir

calls = []


def fake_process_batch(records, task_id, api_key, model="gpt-4.1-mini", options=None):
    """假 AI：不联网，生成一个以源标题为种子的 profile。"""
    rec = records[0]
    calls.append(rec.sku)
    profile = {
        "source_identity": {
            "sku": rec.sku,
            "parent_sku": rec.parent_sku,
            "source_row_index": rec.row_number,
        },
        "generated_title": {"title": f"AI Result for {rec.title}"},
        "bullet_result": {"bullets": ["Bullet A", "Bullet B"]},
        "short_title_result": {"short_title": "AI Short"},
        "highlight_result": {"highlights": []},
        "description_result": {"description": "AI description."},
    }
    save_profiles(task_id, [profile])
    return [profile]


cbp.process_batch = fake_process_batch

records = [rec_a1, rec_a2, rec_a3, rec_b, rec_c]
task_id = "test-variant-dedupe-001"
task_dir = get_task_dir(task_id)

try:
    result_profiles = cbp.process_batch_concurrent(
        records, task_id, "sk-test-key", model="gpt-4.1-mini",
        options={"max_workers": 2},
    )

    check("C1 AI 只跑 3 次(代表行)", len(calls) == 3, calls)
    check("C2 跑的是三个代表行", set(calls) == {"P1-Red", "P2", "P3"}, calls)
    check("C3 返回全部 5 行结果", len(result_profiles) == 5, len(result_profiles))

    by_row = {p["source_identity"]["source_row_index"]: p for p in result_profiles}
    check("C4 每行都有结果", set(by_row) == {2, 3, 4, 5, 6}, sorted(by_row))
    check("C5 变体行共用代表行标题",
          by_row[2]["generated_title"]["title"]
          == by_row[3]["generated_title"]["title"]
          == by_row[4]["generated_title"]["title"]
          == "AI Result for Motor for Printer X")
    check("C6 各行 SKU 正确",
          [by_row[i]["source_identity"]["sku"] for i in (2, 3, 4, 5, 6)]
          == ["P1-Red", "P1-Blue", "P1-Green", "P2", "P3"])
    check("C7 克隆行带标记", "variant_dedupe" in by_row[3] and "variant_dedupe" in by_row[4])
    check("C8 代表行不带克隆标记", "variant_dedupe" not in by_row[2])
    check("C9 不同产品结果独立",
          by_row[5]["generated_title"]["title"] == "AI Result for Filter Kit Y")

    # 关闭合并时：每行都跑 AI（旧行为）
    calls.clear()
    task_id2 = "test-variant-dedupe-002"
    cbp.process_batch_concurrent(
        records, task_id2, "sk-test-key", model="gpt-4.1-mini",
        options={"max_workers": 2, "variant_dedupe": False},
    )
    check("C10 关闭合并时恢复逐行跑", len(calls) == 5, calls)

finally:
    for t in ("test-variant-dedupe-001", "test-variant-dedupe-002"):
        shutil.rmtree(get_task_dir(t), ignore_errors=True)

# ============================================================
print()
print(f"TOTAL: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL TESTS PASSED")

# -*- coding: utf-8 -*-
"""P5 回归：needs_review 人工复核证据链（pt.store v5 + pt.tasks，
2026-10-16 批准令任务二）。

根因：needs_review 分支丢弃 MatchResult——GPT chosen/置信度/候选行只有
error 文本幸存，复核界面拿不到结构化建议。修复：pt_review_evidence 表
（v5）+ 六个 needs_review 分支落建议快照。覆盖——
- store：写入/读取回环、JSON 字段解析、跨店/站点/env 隔离、
  v4→v5 迁移（幂等建表）；
- tasks 集成：GPT 推荐被门槛拦（ai_chosen 留档=AI 建议≠已确认分类，
  assignment 绝不写）、官方验证阶段 needs_review（官方状态快照随行）、
  completed（绝不写复核证据、assignment 照写）；
- 人工确认口径不变：VALID_CHOSEN_BY={ai,human}，human 确认链路可用。
"""

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

import pt.tasks as T  # noqa: E402
from pt.candidates import Candidate, CandidatesResult  # noqa: E402
from pt.material import ProductMaterial  # noqa: E402
from pt.store import PtStore, VALID_CHOSEN_BY  # noqa: E402
from pt.verifier import (  # noqa: E402
    BrowseNodeVerification, CheckResult, PtVerification)

_PASSED = []
SID, MID = "store-A", "AE08W4A3P2T0BE"


def check(name, ok, detail=""):
    if not ok:
        raise AssertionError(f"{name} 失败 {detail}")
    _PASSED.append(name)
    print(f"PASS {name}" + (f"  {detail}" if detail else ""))


def _s2(kw):
    return {"s2_keywords": {"query": {"keywords": [kw]}, "keywords": [kw]}}


def _s3(n, nodes=("1373330031",)):
    asins = [f"B0000000{i:02d}" for i in range(n)]
    return {"s3_catalog_similar": {
        "query": {"keywords": ["k"]}, "asins": asins,
        "classification_ids": list(nodes) * n,
        "overlap_examples": [["x", "y"]] * n}}


# ---- 一、store 层 ----

def _new_store():
    d = tempfile.mkdtemp(prefix="pt_rev_")
    return PtStore(str(Path(d) / "pt.db"))


def test_store_roundtrip_and_bounds():
    st = _new_store()
    st.add_review_evidence(
        store_id=SID, marketplace_id=MID, pid="P1", task_id="T1",
        stage="ai_matching", error_kind="review",
        review_reasons=[f"理由{i}" for i in range(20)],
        ai_chosen="PRESSURE_COOKER", ai_confidence=0.512,
        ai_detail={"status": "review", "candidates": [
            {"pt_name": "PRESSURE_COOKER", "ai_picked": True}]},
        official_status=[{"check_kind": "pt_exists", "status": "passed",
                          "official": True}])
    d = st.latest_review_evidence(SID, MID, "P1")
    check("回环：建议字段齐全", d["ai_chosen"] == "PRESSURE_COOKER"
          and d["ai_confidence"] == 0.512 and d["stage"] == "ai_matching"
          and d["error_kind"] == "review" and d["task_id"] == "T1")
    check("回环：reasons 截 12 条", len(d["review_reasons"]) == 12)
    check("回环：ai_detail/official JSON 已解析",
          d["ai_detail"]["candidates"][0]["ai_picked"] is True
          and d["official_status"][0]["check_kind"] == "pt_exists")
    check("无记录返回 None", st.latest_review_evidence(SID, MID, "PX") is None)


def test_store_isolation():
    st = _new_store()
    st.add_review_evidence(store_id=SID, marketplace_id=MID, pid="P1",
                           stage="s", error_kind="k", review_reasons=["r"])
    check("跨店隔离", st.latest_review_evidence("store-B", MID, "P1") is None)
    check("跨站点隔离",
          st.latest_review_evidence(SID, "ATVPDKIKX0DER", "P1") is None)
    check("mock 不混 live",
          st.latest_review_evidence(SID, MID, "P1", env="mock") is None)


def test_migration_v4_to_v5():
    """v4 库升级：幂等建表，版本=5，旧行不丢。"""
    st = _new_store()
    st.add_assignment(store_id=SID, marketplace_id=MID, pid="P0",
                      pt_name="X", chosen_by="ai", env="live")
    st._db.execute("PRAGMA user_version=4")     # 降回 v4 再重开
    st._db.execute("DROP TABLE pt_review_evidence")
    db_path = st._db.execute("PRAGMA database_list").fetchone()[2]
    st2 = PtStore(db_path)
    v = st2._db.execute("PRAGMA user_version").fetchone()[0]
    has = st2._db.execute(
        "SELECT name FROM sqlite_master WHERE name='pt_review_evidence'"
    ).fetchone()
    check("v4→v5：版本=5 且表已建", v == 5 and has is not None)
    check("v4→v5：旧分配行不丢",
          (st2.current_assignment(SID, MID, "P0") or {}).get("pt_name") == "X")
    st2.add_review_evidence(store_id=SID, marketplace_id=MID, pid="P1",
                            stage="s", error_kind="k", review_reasons=["r"])
    check("v4→v5：新表可写可读",
          st2.latest_review_evidence(SID, MID, "P1") is not None)


# ---- 二、引擎集成（模块级替身：检索/验证/Schema 全假，零联网） ----

class _Client:
    env_mode = "live"


_ORIG: dict = {}


def _patch(**kw):
    for k, v in kw.items():
        _ORIG[k] = getattr(T, k)
        setattr(T, k, v)


def _restore():
    for k, v in _ORIG.items():
        setattr(T, k, v)
    _ORIG.clear()


def _drive(st, materials, cands_by_pid, ai_decide, pids):
    """端到端跑一个任务（模块 5/6 用通过型假件；needs_review 场景在
    更早阶段返回，不会走到）。"""
    t0 = int(time.time())
    _patch(
        extract_material=lambda pid, rec, und: materials[pid],
        retrieve_candidates=lambda client, material, marketplace_id:
            cands_by_pid[material.pid],
        verify_product_type=lambda client, material, pt, **kw:
            PtVerification(
                pt_name=pt, marketplace_id=MID, env="live", checked_at=t0,
                exists=CheckResult("pt_exists", "passed", "spapi:a", True,
                                   {"note": "PTD 官方确认"}),
                semantic=CheckResult("semantic", "passed", "spapi:b", True,
                                     {"note": "语义佐证通过"})),
        verify_browse_node=lambda client, material, nid, **kw:
            BrowseNodeVerification(
                node_id=nid, marketplace_id=MID, env="live", checked_at=t0,
                existence=CheckResult("browse_node", "passed", "spapi:c",
                                      True, {"kind": "by_reference"}),
                catalog=CheckResult("browse_node", "passed", "spapi:c", True,
                                    {"referencing": [], "similar_seen": 1,
                                     "corroboration_share": 1.0}),
                fit=CheckResult("browse_node", "passed", "spapi:c", True,
                                {"note": "适配"}),
                usability=CheckResult("browse_node", "skipped", "internal",
                                      False, {"note": "恒不可评估"})),
        ensure_schema=lambda client, store, cache_root, **kw: SimpleNamespace(
            status="ok", detail={}, schema={"pt": kw.get("pt_name")},
            cache_key="ck1"),
        analyze_schema=lambda schema: SimpleNamespace(properties=[]),
        attribute_gap_report=lambda material, analysis, **kw: SimpleNamespace(
            summary="complete", counts={"ok": 1}, ai_semantic_pt=None,
            entries=[]),
    )
    try:
        eng = T.TaskEngine(
            store=st, client=_Client(), ai_decide=ai_decide,
            load_product=lambda pid: ({"pid": pid}, {}),
            cache_root=tempfile.mkdtemp(prefix="pt_cache_"))
        task = eng.create_task(kind="batch", store_id=SID,
                               marketplace_id=MID, pids=list(pids),
                               env="live")
        eng.run(task["id"])
        return task["id"]
    finally:
        _restore()


def test_engine_gpt_suggestion_preserved_not_confirmed():
    """P5 主场景（r1 真实形态：GPT 过双门槛但置信度 0.49<0.55）→
    needs_review 时 ai_chosen/置信度/候选行/复核原因全部留档；
    assignment 绝不写（AI 建议≠已确认分类，任务二.3）。"""
    st = _new_store()
    m = ProductMaterial(pid="PA", title="Silicone Sealing Ring",
                        product_identity="Silicone Sealing Ring")
    cands = CandidatesResult(pid="PA", marketplace_id=MID, env_mode="live",
                             candidates=[Candidate(
                                 pt_name="PRESSURE_COOKER",
                                 display_name="Pressure Cooker",
                                 sources={**_s2("pressure cooker gasket"),
                                          **_s3(2)}, score=3.2)])

    def ai_decide(payload):
        return {"chosen": "PRESSURE_COOKER",
                "reasons": {"PRESSURE_COOKER": "密封圈用于压力锅"},
                "self_confidence": 0.4}

    _drive(st, {"PA": m}, {"PA": cands}, ai_decide, ["PA"])
    items = st.task_items(_last_task(st), states=None)
    check("任务条目=needs_review",
          items[0]["state"] == "needs_review", str(items[0]["state"]))
    ev = st.latest_review_evidence(SID, MID, "PA")
    check("证据已落：stage/kind", ev["stage"] == "ai_matching"
          and ev["error_kind"] == "review",
          f'{ev and ev["stage"]}/{ev and ev["error_kind"]}')
    check("GPT 推荐留档（ai_chosen）", ev["ai_chosen"] == "PRESSURE_COOKER")
    check("GPT 置信度留档（0.4x<0.55 原值）",
          0.4 <= (ev["ai_confidence"] or 0) < 0.55,
          str(ev["ai_confidence"]))
    picked = [r for r in ev["ai_detail"]["candidates"]
              if r.get("ai_picked")]
    check("候选行留档（ai_picked 行带 GPT 理由）",
          len(picked) == 1 and "压力锅" in picked[0]["ai_reason"])
    check("复核原因留档（置信度低于复核阈值原文）",
          any("低于复核阈值" in r for r in ev["review_reasons"]))
    check("AI 建议绝不写成 assignment",
          st.current_assignment(SID, MID, "PA") is None)


def test_engine_official_stage_snapshot():
    """官方验证阶段 needs_review（semantic_failed）：pt_exists/semantic
    官方结论快照随复核证据留档（复核不必另查验证表）。"""
    st = _new_store()
    m = ProductMaterial(pid="PB", title="Water Hose Pipe Fitting",
                        product_identity="Water Hose Pipe Fitting")
    cands = CandidatesResult(pid="PB", marketplace_id=MID, env_mode="live",
                             candidates=[Candidate(
                                 pt_name="HOSE_PIPE_FITTING",
                                 display_name="Hose Pipe Fitting",
                                 sources={**_s2("hose pipe fitting"),
                                          **_s3(4)}, score=3.4)])
    t0 = int(time.time())
    _patch(
        extract_material=lambda pid, rec, und: m,
        retrieve_candidates=lambda client, material, marketplace_id: cands,
        verify_product_type=lambda client, material, pt, **kw:
            PtVerification(
                pt_name=pt, marketplace_id=MID, env="live", checked_at=t0,
                exists=CheckResult("pt_exists", "passed", "spapi:a", True,
                                   {"note": "PTD 官方确认"}),
                semantic=CheckResult("semantic", "failed", "spapi:b", True,
                                     {"note": "官方名与目录同类双向否定"})),
    )
    try:
        eng = T.TaskEngine(store=st, client=_Client(),
                           ai_decide=lambda p: {
                               "chosen": "HOSE_PIPE_FITTING", "reasons": {},
                               "self_confidence": 0.95},
                           load_product=lambda pid: ({"pid": pid}, {}),
                           cache_root=tempfile.mkdtemp(prefix="pt_c_"))
        task = eng.create_task(kind="batch", store_id=SID,
                               marketplace_id=MID, pids=["PB"], env="live")
        eng.run(task["id"])
    finally:
        _restore()
    ev = st.latest_review_evidence(SID, MID, "PB")
    check("官方阶段 needs_review 落证据", ev is not None
          and ev["stage"] == "pt_verification"
          and ev["error_kind"] == "semantic_failed")
    kinds = {o["check_kind"]: o["status"] for o in ev["official_status"]}
    check("官方状态快照随行（pt_exists=passed + semantic=failed）",
          kinds.get("pt_exists") == "passed"
          and kinds.get("semantic") == "failed", str(kinds))
    check("AI 推荐同样留档（跨阶段不丢）",
          ev["ai_chosen"] == "HOSE_PIPE_FITTING")
    check("验证失败绝不写 assignment",
          st.current_assignment(SID, MID, "PB") is None)


def test_engine_completed_writes_no_review_evidence():
    """completed：assignment 照写（chosen_by=ai），复核证据绝不写。"""
    st = _new_store()
    m = ProductMaterial(pid="PC", title="Water Hose Pipe Fitting",
                        product_identity="Water Hose Pipe Fitting")
    cands = CandidatesResult(pid="PC", marketplace_id=MID, env_mode="live",
                             candidates=[Candidate(
                                 pt_name="HOSE_PIPE_FITTING",
                                 display_name="Hose Pipe Fitting",
                                 sources={**_s2("hose pipe fitting"),
                                          **_s3(4)}, score=3.4)])
    _drive(st, {"PC": m}, {"PC": cands}, lambda p: {
        "chosen": "HOSE_PIPE_FITTING", "reasons": {},
        "self_confidence": 0.95}, ["PC"])
    items = st.task_items(_last_task(st), states=None)
    check("全链路通过→completed", items[0]["state"] == "completed",
          str(items[0]["state"]))
    asg = st.current_assignment(SID, MID, "PC")
    check("assignment 照写（chosen_by=ai）",
          (asg or {}).get("pt_name") == "HOSE_PIPE_FITTING"
          and asg.get("chosen_by") == "ai")
    check("completed 绝不写复核证据",
          st.latest_review_evidence(SID, MID, "PC") is None)


def _last_task(st):
    rows = st._db.execute(
        "SELECT id FROM pt_task ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return str(rows[0]["id"])


def test_human_confirm_path_untouched():
    """任务二.3：正式 assignment 口径不变——human 确认仍只经人工链路。"""
    check("VALID_CHOSEN_BY 仍={ai,human}", VALID_CHOSEN_BY == {"ai", "human"})
    st = _new_store()
    st.add_assignment(store_id=SID, marketplace_id=MID, pid="PH",
                      pt_name="HUMAN_PICK", chosen_by="human",
                      reason="人工确认")
    a = st.current_assignment(SID, MID, "PH")
    check("human 确认可写当前分配",
          (a or {}).get("pt_name") == "HUMAN_PICK"
          and a.get("chosen_by") == "human")
    try:
        st.add_assignment(store_id=SID, marketplace_id=MID, pid="PH",
                          pt_name="X", chosen_by="gpt")
        ok = False
    except ValueError:
        ok = True
    check("非法 chosen_by（如 gpt）仍被拒", ok)


def test_retention_prune():
    """P5 集成验收（任务二.7）：同键只保留最近 REVIEW_EVIDENCE_KEEP 条，
    其他商品/店铺的行不受影响——反复重跑不无限累积。"""
    st = _new_store()
    keep = st.REVIEW_EVIDENCE_KEEP
    for i in range(keep + 5):
        st.add_review_evidence(store_id=SID, marketplace_id=MID, pid="PR",
                               task_id=f"T{i}", stage="ai_matching",
                               error_kind="review",
                               review_reasons=[f"第{i}次"])
    n = st._db.execute(
        "SELECT COUNT(*) c FROM pt_review_evidence WHERE pid='PR'"
    ).fetchone()["c"]
    latest = st.latest_review_evidence(SID, MID, "PR")
    check(f"追加 {keep + 5} 条后裁剪到 {keep}", n == keep, f"n={n}")
    check("最新一条正是最后写入（T%d）" % (keep + 4),
          latest["task_id"] == f"T{keep + 4}", latest["task_id"])
    # 其他键不受影响
    st.add_review_evidence(store_id=SID, marketplace_id=MID, pid="PR2",
                           stage="s", error_kind="k", review_reasons=["r"])
    n2 = st._db.execute(
        "SELECT COUNT(*) c FROM pt_review_evidence WHERE pid='PR2'"
    ).fetchone()["c"]
    check("相邻商品的行不被误删", n2 == 1)


def test_review_read_resilience():
    """任务二.6：复核证据行 JSON 损坏时读取降级（字段 None）不抛异常
    ——复核界面/商品详情主流程不崩。"""
    st = _new_store()
    st.add_review_evidence(store_id=SID, marketplace_id=MID, pid="PJ",
                           stage="ai_matching", error_kind="review",
                           review_reasons=["r"], ai_chosen="X",
                           ai_detail={"a": 1},
                           official_status=[{"check_kind": "pt_exists"}])
    st._db.execute(
        "UPDATE pt_review_evidence SET ai_detail_json='{broken',"
        " review_reasons_json='@@@' WHERE pid='PJ'")
    try:
        d = st.latest_review_evidence(SID, MID, "PJ")
        ok = d is not None and d["ai_detail"] is None \
            and d["review_reasons"] is None \
            and d["ai_chosen"] == "X"
        check("坏 JSON 行：读取降级不抛异常、非 JSON 字段照常", ok)
    except Exception as e:  # noqa: BLE001
        check("坏 JSON 行：读取降级不抛异常", False, type(e).__name__)


def main():
    test_store_roundtrip_and_bounds()
    test_store_isolation()
    test_migration_v4_to_v5()
    test_engine_gpt_suggestion_preserved_not_confirmed()
    test_engine_official_stage_snapshot()
    test_engine_completed_writes_no_review_evidence()
    test_human_confirm_path_untouched()
    test_retention_prune()
    test_review_read_resilience()
    print(f"\n==== test_pt_review_evidence：{len(_PASSED)} 项全过 ====")


if __name__ == "__main__":
    main()

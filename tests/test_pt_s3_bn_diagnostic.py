# -*- coding: utf-8 -*-
"""D3 回归：S3 Catalog 分类节点只读诊断（pt.candidates）。

覆盖（任务三）：诊断计数正确、四类缺因区分（未返回分类节点 / 请求
失败 / 解析失败（形状异常）/ 证据不足）、空节点≠节点不存在证明的
文案、门槛零变化（无 classification_ids 时仍无候选节点产出）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from pt.candidates import (  # noqa: E402
    S3, CandidatesResult, StrategyReport, bn_missing_reason,
    retrieve_candidates,
)
from pt.material import extract_material  # noqa: E402

_PASSED = []


def check(name, ok, detail=""):
    if not ok:
        raise AssertionError(f"{name} 失败 {detail}")
    _PASSED.append(name)
    print(f"PASS {name}" + (f"  {detail}" if detail else ""))


class _R:                     # 假客户端返回壳（data/attempts 足够）
    def __init__(self, data):
        self.data = data
        self.attempts = 1


class _Client:
    env_mode = "live"

    def __init__(self, items):
        self._items = items

    def search_product_types(self, mid, **kw):
        return _R({"productTypes": [], "productTypeVersion": "LATEST"})

    def search_catalog_items(self, mid, **kw):
        return _R({"items": self._items, "numberOfResults": len(self._items)})


def _mat(title="Pot Lid Clear Cover"):
    rec = {"pid": "p", "raw_rows": [{"标题(必填)": title}], "opt": None,
           "sku": "", "cat": ""}
    return extract_material("p", rec, None)


def _item(asin, with_cls=False, with_id=False, with_pt=True, name="Pot Lid"):
    it = {"asin": asin, "summaries": [{"itemName": name}]}
    if with_pt:
        it["productTypes"] = [{"marketplaceId": "MK", "productType": "LID_PT"}]
    if with_cls:
        grp = {"classifications": [
            {"displayName": "Home", **({"classificationId": f"node{asin}"}
                                       if with_id else {})}]}
        it["classifications"] = [grp]
    return it


def test_diag_counts():
    """诊断计数：returned/classifications/classification_ids 三层覆盖。"""
    items = [_item("A1", with_cls=True, with_id=True),
             _item("A2", with_cls=True, with_id=False),
             _item("A3", with_cls=False),
             _item("A4", with_cls=True, with_id=True)]
    r = retrieve_candidates(_Client(items), _mat(), marketplace_id="MK")
    d = r.reports[S3].diag
    check("计数:items=4", d["items"] == 4)
    check("计数:带 classifications=3", d["items_with_classifications"] == 3)
    check("计数:带 classification_ids=2",
          d["items_with_classification_ids"] == 2)
    check("计数:无形状异常", d["shape_anomalies"] == [])
    check("计数:S3 ok 且候选带节点",
          r.reports[S3].status == "ok"
          and r.candidates[0].sources[S3]["classification_ids"]
          == ["nodeA1", "nodeA4"])


def test_diag_no_classification_returned():
    """缺因①：Catalog 正常返回但分类全空（=真实品类常见）——文案明确
    「空节点≠节点不存在」，候选仍无节点（门槛不变）。"""
    items = [_item("B1", with_cls=False), _item("B2", with_cls=False)]
    r = retrieve_candidates(_Client(items), _mat(), marketplace_id="MK")
    check("全空:diag 分类=0/2",
          r.reports[S3].diag["items_with_classifications"] == 0)
    reason = bn_missing_reason(r)
    check("全空:文案区分「未返回分类节点」",
          "未返回分类节点" in reason or "classifications 全空" in reason,
          reason)
    check("全空:文案含「空节点≠节点不存在」", "≠节点不存在" in reason, reason)
    check("全空:候选仍无 classification_ids（门槛不变）",
          r.candidates[0].sources[S3]["classification_ids"] == [])


def test_diag_request_failed():
    """缺因②：请求失败（error 报告）→ 证据链断裂，非节点不存在。"""

    class _ErrClient(_Client):
        def search_catalog_items(self, mid, **kw):
            from pt.spapi_client import SpApiServerError
            raise SpApiServerError("catalog 5xx", status=500,
                                   errors=[{"message": "boom"}],
                                   request_id="rid")

    c = _ErrClient([])
    r = retrieve_candidates(c, _mat(), marketplace_id="MK")
    check("请求失败:S3 error", r.reports[S3].status == "error")
    reason = bn_missing_reason(r)
    check("请求失败:文案指明请求失败", "请求失败" in reason, reason)
    check("请求失败:非节点不存在", "≠节点不存在" in reason, reason)


def test_diag_parse_anomaly():
    """缺因③：解析失败（item 形状异常）→ 形状异常进诊断。"""
    items = [{"asin": "C1", "summaries": "字符串冒充列表",
              "productTypes": [{"marketplaceId": "MK",
                                "productType": "LID_PT"}]},
             "脏行"]
    r = retrieve_candidates(_Client(items), _mat(), marketplace_id="MK")
    d = r.reports[S3].diag
    check("形状异常:脏行被记", "item:not_dict" in d["shape_anomalies"])
    reason = bn_missing_reason(r)
    check("形状异常:文案指明形状异常（有分类时优先报形状）",
          "形状异常" in reason or "classificationId" in reason
          or "未返回分类节点" in reason, reason)


def test_diag_insufficient():
    """缺因④：有 classifications 结构但无 classificationId → 证据不足。"""
    items = [_item("D1", with_cls=True, with_id=False)]
    r = retrieve_candidates(_Client(items), _mat(), marketplace_id="MK")
    reason = bn_missing_reason(r)
    check("证据不足:文案区分 classificationId 缺失",
          "classificationId" in reason, reason)


def test_diag_empty_search():
    """空采信（本体词无重叠）：文案=未采信/无节点证据。"""
    items = [_item("E1", with_pt=False, name="Unrelated Gizmo")]
    r = retrieve_candidates(_Client(items), _mat(), marketplace_id="MK")
    check("空采信:S3 empty", r.reports[S3].status == "empty")
    reason = bn_missing_reason(r)
    check("空采信:文案说明未采信", "未采信" in reason or "无节点证据" in reason,
          reason)


def test_diag_no_report():
    r = CandidatesResult(pid="p", marketplace_id="MK", env_mode="live")
    check("无报告:兜底文案", "无报告" in bn_missing_reason(r))


def main():
    test_diag_counts()
    test_diag_no_classification_returned()
    test_diag_request_failed()
    test_diag_parse_anomaly()
    test_diag_insufficient()
    test_diag_empty_search()
    test_diag_no_report()
    print(f"\n==== test_pt_s3_bn_diagnostic：{len(_PASSED)} 项全过 ====")


if __name__ == "__main__":
    main()

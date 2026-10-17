# -*- coding: utf-8 -*-
"""D1 回归：商品行标准化（pt.normalize）。

覆盖（任务一.7）：三形状（真实服务器英文键+raw_data / 历史中文顶层 /
英文 only）、字段冲突优先级、缺失、空值、类型异常、父子 SKU、原始
数据保留（_orig/_raw_original）、record 顶层业务字段不丢、幂等、
生产接线（pt_api._product_record 调用同一函数）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from pt.normalize import (  # noqa: E402
    COL_PARENT_SKU, COL_SKU, COL_TITLE, normalize_product_record,
    normalize_row,
)

_PASSED = []


def check(name, ok, detail=""):
    if not ok:
        raise AssertionError(f"{name} 失败 {detail}")
    _PASSED.append(name)
    print(f"PASS {name}" + (f"  {detail}" if detail else ""))


def test_real_server_shape():
    """真实服务器形状：英文顶层 + raw_data 嵌套中文 20 列。"""
    row = {"row_number": 1, "sku": "SKU-EN-1", "parent_sku": "PAR-EN",
           "title": "EN Title", "bullets": ["B1", "B2"],
           "description": "EN desc",
           "image_urls": ["https://a/1.jpg", "https://a/2.jpg"],
           "detail_image_urls": [], "language": "英语",
           "raw_data": {"标题(必填)": "中文原始标题", "SKU": "SKU-ZH-1",
                        "父SKU(必填)": "PAR-ZH",
                        "要点1": "要点一", "产品图": "https://a/0.jpg",
                        "库存": "99", "成本价(必填)": "1.5"}}
    n = normalize_row(row)
    check("真实形状:raw_data 原始层赢", n[COL_TITLE] == "中文原始标题")
    check("真实形状:SKU 冲突 raw_data 赢", n[COL_SKU] == "SKU-ZH-1")
    check("真实形状:父SKU 冲突 raw_data 赢", n[COL_PARENT_SKU] == "PAR-ZH")
    check("真实形状:raw_data 非采集器业务列保留", n.get("库存") == "99")
    check("真实形状:英文 image_urls 追加不覆盖",
          "https://a/0.jpg" in n["产品图"]
          and "https://a/1.jpg" in n["产品图"])
    check("真实形状:_shape=en_rawdata", n["_shape"] == "en_rawdata")
    check("真实形状:_orig 保留原始行(含英文业务字段)",
          n["_orig"].get("language") == "英语"
          and n["_orig"].get("row_number") == 1)
    check("真实形状:_orig.raw_data 保留",
          n["_orig"].get("raw_data", {}).get(COL_TITLE) == "中文原始标题")


def test_zh_toplevel_shape():
    """历史测试/桩形状：中文键已在顶层 → 原样通过。"""
    row = {"标题(必填)": "历史标题", "SKU": "HIST-1", "要点1": "老要点"}
    n = normalize_row(row)
    check("历史形状:标题通过", n[COL_TITLE] == "历史标题")
    check("历史形状:_shape=zh_toplevel", n["_shape"] == "zh_toplevel")


def test_en_only_shape():
    """英文 only（无 raw_data）：英文键映射到采集器列。"""
    row = {"sku": 12345, "parent_sku": None, "title": "Only EN",
           "bullets": "单串五点", "description": None,
           "image_urls": "https://a/x.jpg", "detail_image_urls": None}
    n = normalize_row(row)
    check("英文only:title 映射", n[COL_TITLE] == "Only EN")
    check("英文only:数字 SKU 转串", n[COL_SKU] == "12345")
    check("英文only:parent_sku None=空不抛", n[COL_PARENT_SKU] == "")
    check("英文only:bullets 单串→要点1", n["要点1"] == "单串五点")
    check("英文only:image_urls 字符串直通", n["产品图"] == "https://a/x.jpg")
    check("英文only:_shape=en_only", n["_shape"] == "en_only")


def test_missing_empty_and_anomalies():
    """缺失/空值/类型异常：绝不抛、空即空。"""
    n1 = normalize_row({})
    check("空行不抛且标题为空", n1.get(COL_TITLE, "") == "")
    n2 = normalize_row({"raw_data": "json字符串冒充", "title": "EN兜底"})
    check("raw_data 类型异常回退英文层", n2[COL_TITLE] == "EN兜底")
    check("raw_data 异常被记录", n2["_anomalies"]
          and n2["_anomalies"][0].startswith("raw_data:"))
    n3 = normalize_row({"title": None, "bullets": [None, " ", "ok"]})
    check("None 标题=空串", n3[COL_TITLE] == "")
    check("bullets 脏元素剔除", n3.get("要点1") == "ok")


def test_conflict_priority():
    """字段冲突：raw_data > 英文层；英文层 > 缺。"""
    row = {"title": "EN", "description": "EN d",
           "raw_data": {"标题(必填)": "ZH", "简介": ""}}
    n = normalize_row(row)
    check("冲突:标题 raw_data 赢", n[COL_TITLE] == "ZH")
    check("冲突:raw_data 空列由英文层补", n["简介"] == "EN d")


def test_idempotent():
    row = {"title": "T", "raw_data": {"标题(必填)": "Z"}}
    once = normalize_row(row)
    twice = normalize_row(once)
    check("幂等:二次标准化值不变",
          twice[COL_TITLE] == once[COL_TITLE] == "Z")
    check("幂等:_orig 不嵌套",
          "_orig" not in twice["_orig"]
          and twice["_orig"].get("title") == "T")


def test_record_level():
    """record 级：顶层身份字段保留 + 非法行剔除 + 原始数组保留。"""
    prod = {"pid": "p1", "sku": "TOP-SKU", "cat": "类目/子类",
            "opt": {"title": "OPT"}, "title": "顶层标题",
            "raw": [{"title": "R1", "raw_data": {"标题(必填)": "行一"}},
                    "脏字符串行", {"title": "R2",
                                   "raw_data": {"标题(必填)": "行二"}}]}
    rec = normalize_product_record(prod, "p1")
    check("record:pid 保留", rec["pid"] == "p1")
    check("record:sku/cat/opt 顶层保留",
          rec["sku"] == "TOP-SKU" and rec["cat"] == "类目/子类"
          and rec["opt"] == {"title": "OPT"})
    check("record:非法行剔除后 2 行", len(rec["raw_rows"]) == 2)
    check("record:_raw_original 保留原始行数组",
          len(rec["_raw_original"]) == 2
          and rec["_raw_original"][0]["title"] == "R1")
    check("record:_row_shapes 溯源", rec["_row_shapes"] ==
          ["en_rawdata", "en_rawdata"])
    rec2 = normalize_product_record(None, "x")
    check("record:prod=None 不抛且空行", rec2["raw_rows"] == []
          and rec2["pid"] == "x")


def test_production_wiring():
    """生产接线：pt_api._product_record 必须走统一标准化函数（任务一.6）。"""
    import inspect

    import pt_api
    src = inspect.getsource(pt_api.PtApi._product_record)
    check("生产路径调用 normalize_product_record",
          "normalize_product_record(prod, pid)" in src)
    sys.modules.setdefault("pt_api_mod", pt_api)


def main():
    test_real_server_shape()
    test_zh_toplevel_shape()
    test_en_only_shape()
    test_missing_empty_and_anomalies()
    test_conflict_priority()
    test_idempotent()
    test_record_level()
    test_production_wiring()
    print(f"\n==== test_pt_row_normalize：{_PASSED.__len__()} 项全过 ====")


if __name__ == "__main__":
    main()

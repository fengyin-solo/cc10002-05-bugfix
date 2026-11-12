"""清单导出取数验收：多页匹配、零匹配、重名异号、模块隔离与列表/导出同口径。"""
from __future__ import annotations

from tests.conftest import seed_module


def test_export_returns_full_filtered_set_not_only_current_page(client):
    """页面 size=2 只能看一页，但导出必须拿满过滤范围的全部结果。"""
    seed_module("report", [
        ("REPO-4001", "多页报告甲", "草稿"),
        ("REPO-4002", "多页报告乙", "草稿"),
        ("REPO-4003", "多页报告丙", "草稿"),
        ("REPO-4004", "多页报告丁", "草稿"),
        ("REPO-4005", "多页报告戊", "草稿"),
    ])
    page_one = client.get("/api/report", params={"模板名称": "多页报告", "page": 1, "size": 2}).json()
    assert page_one["total"] == 5
    assert len(page_one["items"]) == 2  # 列表只给当前页

    exported = client.get("/api/report/export", params={"模板名称": "多页报告"}).json()
    assert exported["total"] == 5
    assert len(exported["items"]) == 5  # 导出是全量
    assert {row["模板编号"] for row in exported["items"]} == {
        f"REPO-400{i}" for i in range(1, 6)
    }
    # 明细、总数口径一致
    assert exported["total"] == len(exported["items"])


def test_export_multi_page_matches_reassembled_list_pages(client):
    """逐页拼回的列表结果与一次导出的结果，应解释同一批数据。"""
    seed_module("export", [
        (f"EXPO-500{i}", f"分页任务{i}", "待执行") for i in range(1, 7)
    ])
    seen: list[int] = []
    page, total = 1, None
    while True:
        body = client.get("/api/export", params={"status": "待执行", "page": page, "size": 3}).json()
        total = body["total"]
        if not body["items"]:
            break
        seen.extend(row["id"] for row in body["items"])
        page += 1
    assert total == len(seen)

    exported = client.get("/api/export/export", params={"status": "待执行"}).json()
    assert exported["total"] == total
    assert sorted(row["id"] for row in exported["items"]) == sorted(seen)


def test_zero_match_export_is_empty_but_consistent(client):
    exported = client.get("/api/report/export", params={"模板编号": "REPO-NOPE"})
    assert exported.status_code == 200
    body = exported.json()
    assert body["total"] == 0
    assert body["items"] == []

    listed = client.get("/api/report", params={"模板编号": "REPO-NOPE"}).json()
    assert listed["total"] == 0 and listed["items"] == []


def test_duplicate_name_with_different_codes_both_exported(client):
    """重名异号：名称包含匹配要同时命中两条，编号精确匹配各自只命中一条。"""
    seed_module("report", [
        ("REPO-6001", "同名模板", "草稿"),
        ("REPO-6002", "同名模板", "已生效"),
    ])
    by_name = client.get("/api/report/export", params={"模板名称": "同名模板"}).json()
    assert by_name["total"] == 2
    assert {row["模板编号"] for row in by_name["items"]} == {"REPO-6001", "REPO-6002"}

    for code in ("REPO-6001", "REPO-6002"):
        by_code = client.get("/api/report/export", params={"模板编号": code}).json()
        assert by_code["total"] == 1
        assert by_code["items"][0]["模板编号"] == code


def test_export_filters_do_not_leak_between_modules(client):
    """报告模块的筛选条件不能影响导出任务模块，反之亦然。"""
    seed_module("report", [("REPO-8001", "跨模块隔离报告", "草稿")])
    seed_module("export", [("EXPO-8001", "跨模块隔离任务", "草稿")])

    report_body = client.get("/api/report/export", params={"模板名称": "跨模块隔离"}).json()
    assert report_body["total"] == 1
    assert all("模板编号" in row for row in report_body["items"])

    export_body = client.get("/api/export/export", params={"查询SQL": "跨模块隔离"}).json()
    assert export_body["total"] == 1
    assert all("任务编号" in row for row in export_body["items"])

    # 用 A 模块的字段名请求 B 模块必须被拒绝，而不是悄悄放行
    assert client.get("/api/export/export", params={"模板名称": "跨模块隔离"}).status_code == 400
    assert client.get("/api/report/export", params={"任务编号": "EXPO-8001"}).status_code == 400


def test_detail_list_and_export_explain_same_batch(client):
    """列表、详情、下载内容指向同一批快照结果，来源可互相解释。"""
    seed_module("report", [("REPO-9001", "对账模板", "待审核")])
    listed = client.get("/api/report", params={"模板编号": "REPO-9001"}).json()
    exported = client.get("/api/report/export", params={"模板编号": "REPO-9001"}).json()
    entry_id = listed["items"][0]["id"]
    detail = client.get(f"/api/report/{entry_id}").json()

    assert listed["total"] == exported["total"] == 1
    assert exported["items"][0]["id"] == entry_id
    assert detail["模板编号"] == exported["items"][0]["模板编号"]
    assert detail["status"] == exported["items"][0]["status"]
    assert exported["snapshot_id"].startswith("report-v")

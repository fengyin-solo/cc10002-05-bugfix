"""路由兼容与统一过滤口径验收：422 根因、数字明细、别名、字段语义与边界参数。"""
from __future__ import annotations

from tests.conftest import seed_module


def test_export_route_no_longer_shadowed_by_integer_detail(client):
    """回归：/export 曾被 /{entry_id} 吞掉返回 422。"""
    for module in ("report", "export"):
        resp = client.get(f"/api/{module}/export")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["module"] == module
        assert body["total"] == len(body["items"])


def test_numeric_detail_access_still_works_and_unknown_id_404(client):
    """数字明细访问形态保持兼容；不存在的 id 仍是 404 而不是被导出路由吞掉。"""
    ok = client.get("/api/report/1")
    assert ok.status_code == 200
    assert ok.json()["模板编号"] == "REPO-0001"

    missing = client.get("/api/report/999")
    assert missing.status_code == 404

    ok_export = client.get("/api/export/2")
    assert ok_export.status_code == 200
    assert ok_export.json()["任务编号"] == "EXPO-0002"


def test_keyword_alias_equals_code_field_filter(client):
    """历史 keyword 参数与编号字段是同一口径，且编号走精确匹配。"""
    seed_module("report", [
        ("REPO-1001", "季度复盘", "草稿"),
        ("REPO-1002", "季度复盘副本", "草稿"),
    ])
    by_alias = client.get("/api/report", params={"keyword": "REPO-1001"}).json()
    by_field = client.get("/api/report", params={"模板编号": "REPO-1001"}).json()
    assert by_alias["total"] == by_field["total"] == 1
    assert by_alias["items"][0]["模板编号"] == "REPO-1001"

    # 精确匹配：编号片段不能命中别的编号
    partial = client.get("/api/report", params={"模板编号": "REPO-100"})
    assert partial.json()["total"] == 0


def test_name_field_uses_contains_and_differs_from_code(client):
    """名称字段是包含匹配，且不会串到编号字段的语义上。"""
    seed_module("report", [
        ("REPO-2001", "季度经营复盘报告", "草稿"),
        ("REPO-2002", "月度经营简报", "草稿"),
        ("REPO-2003", "季度经营复盘报告-补", "待审核"),
    ])
    body = client.get("/api/report", params={"模板名称": "季度经营复盘报告"}).json()
    assert body["total"] == 2
    assert {row["模板编号"] for row in body["items"]} == {"REPO-2001", "REPO-2003"}

    # 名称条件不会被拿去匹配编号
    body_code = client.get("/api/report", params={"模板名称": "REPO-2001"}).json()
    assert body_code["total"] == 0


def test_empty_string_and_whitespace_conditions_are_ignored(client):
    """空字符串/纯空白不能偷偷把筛选语义变成零匹配。"""
    seed_module("report", [("REPO-3001", "空白测试", "草稿")])
    baseline = client.get("/api/report").json()["total"]
    for value in ("", "   "):
        body = client.get("/api/report", params={"模板名称": value, "status": value}).json()
        assert body["total"] == baseline


def test_chinese_conditions_work(client):
    seed_module("export", [
        ("EXPO-7001", "select * from 订单明细", "草稿"),
        ("EXPO-7002", "select * from 用户画像", "执行中"),
    ])
    body = client.get("/api/export", params={"查询SQL": "订单明细"}).json()
    assert body["total"] == 1
    assert body["items"][0]["任务编号"] == "EXPO-7001"

    body_status = client.get("/api/export", params={"status": "执行中"}).json()
    assert all(row["status"] == "执行中" for row in body_status["items"])
    assert body_status["total"] >= 1


def test_illegal_page_and_size_do_not_change_filter_semantics(client):
    """非法页码/边界条数直接 400，而不是被钳制后悄悄返回别的结果。"""
    assert client.get("/api/report", params={"page": 0}).status_code == 400
    assert client.get("/api/report", params={"page": -3}).status_code == 400
    assert client.get("/api/report", params={"size": 0}).status_code == 400
    assert client.get("/api/report", params={"size": 201}).status_code == 400
    # 非整数页码维持 FastAPI 的 422 形态
    assert client.get("/api/report", params={"page": "abc"}).status_code == 422
    # 合法但越界的页码返回空页，total 仍然反映过滤后的真实总数
    out_of_range = client.get("/api/report", params={"page": 999, "size": 2}).json()
    assert out_of_range["items"] == []
    assert out_of_range["total"] == 3


def test_unknown_status_and_unknown_field_rejected(client):
    assert client.get("/api/report", params={"status": "已下架"}).status_code == 400
    assert client.get("/api/export", params={"not_a_field": "x"}).status_code == 400


def test_conflicting_keyword_and_code_rejected(client):
    resp = client.get("/api/report", params={"keyword": "REPO-0001", "模板编号": "REPO-0002"})
    assert resp.status_code == 400

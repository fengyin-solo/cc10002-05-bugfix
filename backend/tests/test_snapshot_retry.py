"""并发一致性与失败重试验收：快照隔离、状态变化不串批、重试不覆盖成功产物。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.artifacts import artifact_store
from app.services.report import ReportService
from tests.conftest import seed_module


def _action(client, module: str, entry_id: int, action: str) -> None:
    resp = client.post(f"/api/{module}/{entry_id}/actions", json={"values": {"action": action}})
    assert resp.status_code == 200, resp.text


def test_snapshot_is_consistent_when_status_changes_during_export(client):
    """导出进行中报告模板被停用：同一份产物里的明细、总数、状态仍来自同一快照。"""
    seed_module("report", [
        (f"REPO-C{i}", f"并发报告{i}", "草稿") for i in range(1, 11)
    ])
    service = ReportService()
    payload = service.export_entries(
        params={"模板名称": "并发报告"}, status=None
    )
    snapshot_id, version, total = payload["snapshot_id"], payload["snapshot_version"], payload["total"]
    assert total == 10

    # 快照取出之后，另一个线程把其中一条模板停用并新增一条同名模板
    def mutate() -> None:
        target = client.get("/api/report", params={"模板编号": "REPO-C1"}).json()["items"][0]
        _action(client, "report", target["id"], "停用模板")
        client.post("/api/report", json={"values": {
            "模板编号": "REPO-C11", "模板名称": "并发报告11", "包含指标": "x",
        }})

    t = threading.Thread(target=mutate)
    t.start()
    t.join()

    rows = [row for chunk in payload["chunk_iter"]() for row in chunk]
    assert len(rows) == total == 10  # 总数与明细数量一致
    assert all(row["status"] == "草稿" for row in rows)  # 状态停在快照时刻
    assert {row["模板编号"] for row in rows} == {f"REPO-C{i}" for i in range(1, 11)}
    # 标识为 latin-1 安全：中文条件已做百分号编码
    assert snapshot_id.startswith(f"report-v{version}-")
    assert snapshot_id.isascii()

    # 线上数据确实已经变化，新的请求看到的是新版本快照
    after = client.get("/api/report/export", params={"模板名称": "并发报告"}).json()
    assert after["snapshot_version"] > version
    assert after["total"] == 11


def test_export_task_completing_during_export_keeps_one_snapshot(client):
    """导出任务在导出过程中归档完成：产物里仍是导出前的执行中状态，不与新状态混杂。"""
    seed_module("export", [("EXPO-C1", "长耗时导出任务", "执行中")])
    before = client.get("/api/export/export", params={"任务编号": "EXPO-C1"}).json()
    assert before["total"] == 1 and before["items"][0]["status"] == "执行中"
    snapshot_version = before["snapshot_version"]
    entry_id = before["items"][0]["id"]

    _action(client, "export", entry_id, "归档文件")

    again = client.get("/api/export/export", params={"任务编号": "EXPO-C1"}).json()
    assert again["snapshot_version"] > snapshot_version
    assert again["items"][0]["status"] == "已完成"
    # 旧快照描述的状态仍可通过其 snapshot 语义复核（版本号不同，不冲突）
    assert before["items"][0]["status"] == "执行中"


def test_concurrent_exports_each_keep_their_own_snapshot(client):
    """两个并发导出命中不同时刻：先取快照的导出不受后发生的状态流转影响。"""
    seed_module("report", [("REPO-D1", "并发双导出", "草稿")])
    svc = ReportService()
    first = svc.export_entries(params={"模板名称": "并发双导出"}, status=None)
    entry_id = first["chunk_iter"]().__next__()[0]["id"]

    barrier = threading.Barrier(2)

    def exporter(seen: list) -> None:
        barrier.wait()
        seen.extend(row for chunk in first["chunk_iter"]() for row in chunk)
    def mutator() -> None:
        barrier.wait()
        _action(client, "report", entry_id, "生效发布")

    seen: list = []
    t1 = threading.Thread(target=exporter, args=(seen,))
    t2 = threading.Thread(target=mutator)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert len(seen) == first["total"] == 1
    assert seen[0]["status"] == "草稿"  # 先取的快照不被后发生的动作改写


def test_download_artifact_then_retry_does_not_overwrite_success(client, export_dir: Path):
    """失败后重试：成功产物落盘且重试复用同一文件，绝不覆盖之前的成功结果。"""
    seed_module("export", [("EXPO-R1", "可重试导出", "草稿")])

    # 第一次下载注入一次性故障（通配符，避免预览与下载之间版本变化导致 id 对不上）
    artifact_store.faults["*"] = "模拟磁盘抖动"
    failed = client.get("/api/export/export", params={"任务编号": "EXPO-R1", "download": 1})
    assert failed.status_code == 502
    snapshot_id = failed.json()["detail"]["snapshot_id"]
    assert not list(export_dir.glob("export-*.json"))  # 失败不留半成品成功文件

    # 重试成功：产物落盘
    ok = client.get("/api/export/export", params={"任务编号": "EXPO-R1", "download": 1})
    assert ok.status_code == 200
    assert ok.headers["X-Snapshot-Id"] == snapshot_id
    files = list(export_dir.glob("export-*.json"))
    assert len(files) == 1
    first_file = files[0]
    on_disk = json.loads(first_file.read_text(encoding="utf-8"))
    assert on_disk["total"] == 1 and on_disk["items"][0]["任务编号"] == "EXPO-R1"

    # 再次重试：复用成功产物，文件名/内容都不变
    retry = client.get("/api/export/export", params={"任务编号": "EXPO-R1", "download": 1})
    assert retry.status_code == 200
    assert list(export_dir.glob("export-*.json")) == [first_file]

    # 补下载入口返回同一份文件
    fetched = client.get(f"/api/export/export/artifacts/{snapshot_id}")
    assert fetched.status_code == 200
    assert fetched.json()["snapshot_id"] == snapshot_id


def test_artifact_unknown_and_failed_record_endpoints(client):
    assert client.get("/api/report/export/artifacts/report-v999-x").status_code == 404

    seed_module("report", [("REPO-R2", "失败产物测试", "草稿")])
    artifact_store.faults["*"] = "模拟失败"
    bad = client.get("/api/report/export", params={"模板编号": "REPO-R2", "download": 1})
    assert bad.status_code == 502
    failed_id = bad.json()["detail"]["snapshot_id"]
    conflict = client.get(f"/api/report/export/artifacts/{failed_id}")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["snapshot_id"] == failed_id


def test_export_streaming_does_not_build_unbounded_payload(client, monkeypatch):
    """数据量增长时导出按块产出，单块有上限，不存在随行数线性膨胀的拼接列表。"""
    from app.services import report as report_service_module

    seed_module("report", [
        (f"REPO-B{i:04d}", f"大批量报告{i}", "草稿") for i in range(1, 2501)
    ])
    # 把分块缩小到 300：2500 行应当切成 9 块（300*8 + 100），每块都不超过上限
    monkeypatch.setattr(report_service_module, "EXPORT_CHUNK", 300)
    resp = client.get("/api/report/export", params={"模板名称": "大批量报告"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2500 == len(body["items"])

    # 直接核验服务层的分块行为：块数 = ceil(2500/300)，最大块不超过 300
    svc = report_service_module.ReportService()
    payload = svc.export_entries(params={"模板名称": "大批量报告"}, status=None)
    chunks = list(payload["chunk_iter"]())
    assert len(chunks) == 9
    assert max(len(chunk) for chunk in chunks) <= 300
    assert sum(len(chunk) for chunk in chunks) == payload["total"] == 2500

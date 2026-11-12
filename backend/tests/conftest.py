"""验收测试的公共夹具：每个用例都从干净的种子数据与临时产物目录开始。"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# 让 tests 目录无需安装即可 import app 包。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.artifacts import artifact_store  # noqa: E402
from app.main import app  # noqa: E402
from app.store import store  # noqa: E402


REPORT_FIELDS = ["模板编号", "模板名称", "包含指标", "报告周期", "图表模板", "导出格式", "推送渠道", "接收人列表"]
EXPORT_FIELDS = ["任务编号", "查询SQL", "导出格式", "字段列表", "分隔符", "编码格式", "压缩方式", "目标路径"]


def _make_row(module: str, row_id: int, code: str, name: str, status: str) -> dict[str, Any]:
    if module == "report":
        row = {field: f"{name}-{field}" for field in REPORT_FIELDS}
        row["模板编号"] = code
        row["模板名称"] = name
    else:
        row = {field: f"{name}-{field}" for field in EXPORT_FIELDS}
        row["任务编号"] = code
        row["查询SQL"] = name
    row.update({
        "id": row_id,
        "status": status,
        "pending": status not in ("已停用", "已完成", "已失败"),
        "abnormal": False,
    })
    return row


def seed_module(module: str, specs: list[tuple[str, str, str]]) -> None:
    """按 (编号, 名称, 状态) 规格向指定模块批量写入测试行。"""
    existing = store.rows(module)
    next_id = max((int(row["id"]) for row in existing), default=0) + 1
    for code, name, status in specs:
        store.insert(module, _make_row(module, next_id, code, name, status))
        next_id += 1


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    store.reset()
    artifact_store.reset(directory=str(tmp_path / "exports"))
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    return tmp_path / "exports"

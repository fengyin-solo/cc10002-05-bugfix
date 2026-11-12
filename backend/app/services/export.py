"""数据导出业务规则：状态流转、字段校验与筛选口径都收在这里。

列表与清单导出共用 query_snapshot()：同一份字段定义、同一份过滤口径、
同一份快照，区别只在分页还是全量产出，避免“页面能查到、导出查不到”。
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from app.artifacts import artifact_store
from app.config import settings
from app.filtering import FilterField, QueryError, apply_filters, normalize, paginate, validate_page
from app.store import store

MODULE = "export"
REQUIRED_FIELDS = ["任务编号", "查询SQL", "导出格式"]
STATUS_ORDER = ["草稿", "待执行", "执行中", "已完成", "已失败"]
ACTION_RULES = {"提交执行": "待执行", "启动导出": "执行中", "终止导出": "已失败", "归档文件": "已完成"}
NEGATIVE_ACTIONS = []

# 任务编号按整串精确匹配；查询SQL/导出格式等字段按包含匹配，
# 与分析报告模块各自独立，不共享过滤条件。
FILTER_FIELDS: tuple[FilterField, ...] = (
    FilterField("任务编号", exact=True),
    FilterField("查询SQL", exact=False),
    FilterField("导出格式", exact=False),
)
ALLOWED_STATUSES: tuple[str, ...] = tuple(STATUS_ORDER)
KEYWORD_ALIAS = "任务编号"
EXPORT_CHUNK = 500


class ExportService:
    # ---- 列表与导出共用的取数口径 ---------------------------------------

    def query_snapshot(
        self,
        *,
        params: dict[str, str | None],
        status: str | None,
    ) -> tuple[list[dict[str, Any]], int, int, dict[str, str]]:
        """取一致快照并按统一口径过滤，返回 (过滤后行, total, 快照版本, 规范化条件)。"""
        rows, version = store.snapshot(MODULE)
        filtered = apply_filters(
            rows,
            fields=FILTER_FIELDS,
            params=params,
            status=status,
            allowed_statuses=ALLOWED_STATUSES,
        )
        normalized = {
            field.name: value
            for field in FILTER_FIELDS
            if (value := normalize(params.get(field.name))) is not None
        }
        if (status_value := normalize(status)) is not None:
            normalized["status"] = status_value
        return filtered, len(filtered), version, normalized

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        extra_filters: dict[str, str | None] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        validate_page(page, size, size_max=settings.page_size_max)
        params: dict[str, str | None] = dict(extra_filters or {})
        if normalize(keyword) is not None:
            params.setdefault(KEYWORD_ALIAS, keyword)
        rows, total, _version, _filters = self.query_snapshot(params=params, status=status)
        return paginate(rows, page=page, size=size), total

    def export_entries(
        self,
        *,
        params: dict[str, str | None],
        status: str | None,
    ) -> dict[str, Any]:
        """按当前检索范围导出全量结果：一次快照、一个 total、分块产出。"""
        rows, total, version, filters = self.query_snapshot(params=params, status=status)
        snapshot_id = artifact_store.build_snapshot_id(MODULE, version, filters)

        def chunk_iter() -> Iterator[list[dict[str, Any]]]:
            for start in range(0, len(rows), EXPORT_CHUNK):
                yield rows[start:start + EXPORT_CHUNK]

        return {
            "module": MODULE,
            "total": total,
            "snapshot_id": snapshot_id,
            "snapshot_version": version,
            "filters": filters,
            "chunk_iter": chunk_iter,
        }

    def write_artifact(self, payload: dict[str, Any]) -> Any:
        return artifact_store.produce(
            snapshot_id=payload["snapshot_id"],
            module=MODULE,
            version=payload["snapshot_version"],
            total=payload["total"],
            filters=payload["filters"],
            chunk_iter=payload["chunk_iter"],
            header={"module": MODULE},
        )

    # ---- 明细与状态流转（保持既有接口形态） ------------------------------

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing

        def _build() -> dict[str, Any]:
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            store.insert(MODULE, entry)
            return entry

        with store.locked():
            return _build(), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据导出可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        def _mutate(entry: dict[str, Any]) -> None:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS

        updated = store.update(MODULE, entry_id, _mutate)
        if updated is None:
            return None, f"导出任务 {entry_id} 不存在或已归档"
        return updated, f"导出任务已{action}"


__all__ = [
    "ExportService",
    "FILTER_FIELDS",
    "ALLOWED_STATUSES",
    "KEYWORD_ALIAS",
    "MODULE",
    "QueryError",
]

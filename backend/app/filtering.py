"""统一的筛选与分页口径：列表接口和清单导出共用这一套取数逻辑。

设计要点：
- 编号字段（模板编号/任务编号）与名称字段（模板名称/查询SQL 等）含义不同，
  分别声明、分别匹配，避免把名称里的中文片段误当成编号；
- 空字符串、纯空白一律视为“没有筛选”，不能偷偷把结果集变成零匹配；
- 非法页码 / 每页条数直接抛 400，不做静默纠正，避免边界参数改变筛选语义；
- 过滤只作用于传入的快照，分页在过滤之后，导出与列表看到的是同一批行。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class QueryError(ValueError):
    """请求参数不合法；路由层统一翻译成 HTTP 400。"""


@dataclass(frozen=True)
class FilterField:
    """一个可检索字段的匹配规则。"""

    name: str
    exact: bool = False  # 编号类字段是否按整串精确匹配；名称类字段用包含匹配

    def matches(self, row: dict[str, Any], value: str) -> bool:
        cell = str(row.get(self.name, "") or "")
        if self.exact:
            return cell == value
        return value in cell


def normalize(value: str | None) -> str | None:
    """空白条件等价于未传条件；其余值保留原文（含中文），只去掉首尾空白。"""
    if value is None:
        return None
    text = value.strip()
    return text or None


def validate_page(page: int, size: int, *, size_max: int) -> None:
    """页码与每页条数必须是正整数；size 还有上限，防止一次拉取拖垮服务。"""
    if page < 1:
        raise QueryError(f"页码 page 必须是不小于 1 的正整数，收到 {page}")
    if size < 1:
        raise QueryError(f"每页条数 size 必须是不小于 1 的正整数，收到 {size}")
    if size > size_max:
        raise QueryError(f"每页最多 {size_max} 条，请缩小分页范围")


def apply_filters(
    rows: list[dict[str, Any]],
    *,
    fields: tuple[FilterField, ...],
    params: dict[str, str | None],
    status: str | None,
    allowed_statuses: tuple[str, ...],
) -> list[dict[str, Any]]:
    """按字段条件与状态过滤快照行，多个条件之间是 AND。

    - 只认识 fields 里声明过的字段，未声明的参数由路由层提前拒绝；
    - status 必须是模块允许的中文状态之一，非法值报错而不是返回空页；
    - 空字符串条件在 normalize 阶段已经被折叠成 None，不参与过滤。
    """
    status = normalize(status)
    if status is not None and status not in allowed_statuses:
        raise QueryError(
            f"状态「{status}」不合法，允许的取值为：{'、'.join(allowed_statuses)}"
        )

    result = rows
    for field in fields:
        value = normalize(params.get(field.name))
        if value is None:
            continue
        result = [row for row in result if field.matches(row, value)]

    if status is not None:
        result = [row for row in result if str(row.get("status", "") or "") == status]
    return result


def paginate(rows: list[dict[str, Any]], *, page: int, size: int) -> list[dict[str, Any]]:
    """过滤完成后再分页；越界页码返回空列表，total 仍由调用方按过滤结果给出。"""
    start = (page - 1) * size
    return rows[start:start + size]

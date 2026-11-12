"""数据导出接口：维护导出任务，覆盖提交执行、启动导出、终止导出、归档文件等动作。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.filtering import QueryError
from app.routers._export_support import (
    collect_filters,
    download_export,
    get_artifact,
    stream_export,
    translate_query_error,
)
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.export import ALLOWED_STATUSES, ExportService

router = APIRouter(prefix="/api/export", tags=["数据导出"])

service = ExportService()

LIST_FIELDS = ["任务编号", "查询SQL", "导出格式", "字段列表", "分隔符", "编码格式", "压缩方式", "目标路径"]
STATUSES = list(ALLOWED_STATUSES)


# 所有 /export 开头的路由都声明在 /{entry_id} 之前，避免字面量 "export" 与
# artifacts 子路径被当成整数明细编号解析成 422/404；数字明细访问仍由
# /{entry_id} 承接，老客户端的 /api/export/<数字> 不受影响。
@router.get("/export/artifacts/{snapshot_id}")
def download_artifact(snapshot_id: str) :
    """按快照标识补下载/重试：成功产物原样返回，失败记录给出可重试说明。"""
    return get_artifact(snapshot_id)


@router.get("/export")
def export_entries(
    request: Request,
    status: str | None = Query(default=None, description="草稿、待执行、执行中、已完成、已失败"),
    download: int = Query(default=0, description="1 时以附件文件下载，默认内联返回 JSON"),
) :
    """导出数据导出清单：承接页面当前检索范围，返回过滤后的全量结果而非当前页。"""
    params = collect_filters(request, "export")
    try:
        payload = service.export_entries(params=params, status=status)
        if download == 1:
            return download_export(service, payload)
        return stream_export(payload)
    except QueryError as exc:
        raise translate_query_error(exc)


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按任务编号检索（历史别名）"),
    status: str | None = Query(default=None, description="草稿、待执行、执行中、已完成、已失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号、查询SQL等字段与状态过滤数据导出列表；没有数据时返回空页，不报错。"""
    # collect_filters 已把历史 keyword 别名并入“任务编号”条件，列表与导出同口径。
    params = collect_filters(request, "export")
    try:
        items, total = service.list_entries(
            status=status, page=page, size=size, extra_filters=params
        )
    except QueryError as exc:
        raise translate_query_error(exc)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条导出任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"导出任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条导出任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="导出任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条导出任务执行提交执行、启动导出、终止导出、归档文件；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

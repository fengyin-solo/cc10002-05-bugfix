"""报告清单 / 导出任务清单两个路由共用的 HTTP 适配逻辑。

- 只接受模块显式声明过的筛选字段（含历史 keyword 别名），未识别参数直接 400，
  避免把拼错的条件静默吞掉、让用户误以为筛选生效；
- 导出支持两种既有入口形态：默认返回内联 JSON（老客户端 window.open 即可看），
  download=1 时返回带 Content-Disposition 的文件下载；
- 内联 JSON 用 StreamingResponse 分块输出，内存只持有一个分块，不做全量拼接。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.artifacts import ArtifactError, ArtifactRecord, artifact_store
from app.filtering import QueryError
from app.services.export import FILTER_FIELDS as EXPORT_FILTER_FIELDS
from app.services.export import KEYWORD_ALIAS as EXPORT_KEYWORD_ALIAS
from app.services.report import FILTER_FIELDS as REPORT_FILTER_FIELDS
from app.services.report import KEYWORD_ALIAS as REPORT_KEYWORD_ALIAS

# 分页与控制参数不属于字段筛选，收集条件时需要排除。
_RESERVED = {"page", "size", "status", "keyword", "download", "snapshot_id"}
_FIELDS_BY_MODULE = {"report": REPORT_FILTER_FIELDS, "export": EXPORT_FILTER_FIELDS}
_KEYWORD_ALIAS_BY_MODULE = {"report": REPORT_KEYWORD_ALIAS, "export": EXPORT_KEYWORD_ALIAS}


def collect_filters(request: Request, module: str) -> dict[str, str | None]:
    """把查询串翻译成服务层认识的字段条件，并做白名单校验。"""
    known = {field.name for field in _FIELDS_BY_MODULE[module]}
    params: dict[str, str | None] = {}
    for raw_key, value in request.query_params.multi_items():
        if raw_key in _RESERVED:
            continue
        if raw_key not in known:
            allowed = "、".join(sorted(known))
            raise HTTPException(status_code=400, detail=f"不支持的筛选字段「{raw_key}」，可检索字段：{allowed}")
        params[raw_key] = value

    keyword = request.query_params.get("keyword")
    if keyword is not None and keyword.strip():
        alias = _KEYWORD_ALIAS_BY_MODULE[module]
        # 显式编号字段与历史 keyword 别名同时出现且不一致时，不替用户二选一。
        if alias in params and params[alias] != keyword.strip():
            raise HTTPException(
                status_code=400,
                detail=f"keyword 与「{alias}」取值冲突，请只保留一个编号检索条件",
            )
        params.setdefault(alias, keyword)
    return params


def snapshot_headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "X-Snapshot-Id": payload["snapshot_id"],
        "X-Snapshot-Version": str(payload["snapshot_version"]),
        "X-Snapshot-Total": str(payload["total"]),
    }


def _iter_json(payload: dict[str, Any]) -> Iterator[bytes]:
    head = {
        "module": payload["module"],
        "total": payload["total"],
        "snapshot_id": payload["snapshot_id"],
        "snapshot_version": payload["snapshot_version"],
        "filters": payload["filters"],
    }
    opener = "{" + ",".join(
        f"{json.dumps(key, ensure_ascii=False)}:{json.dumps(value, ensure_ascii=False)}"
        for key, value in head.items()
    )
    yield (opener + ',"items":[').encode("utf-8")
    first = True
    for chunk in payload["chunk_iter"]():
        for row in chunk:
            prefix = b"" if first else b","
            yield prefix + json.dumps(row, ensure_ascii=False).encode("utf-8")
            first = False
    yield b"]}"


def stream_export(payload: dict[str, Any]) -> StreamingResponse:
    """内联返回导出 JSON：分块流式输出，避免一次性拼接整份清单。"""
    return StreamingResponse(
        _iter_json(payload),
        media_type="application/json; charset=utf-8",
        headers=snapshot_headers(payload),
    )


def download_export(service: Any, payload: dict[str, Any]) -> FileResponse:
    """落盘成产物并以附件形式返回；失败转 502，调用方可凭 snapshot_id 重试。"""
    try:
        record = service.write_artifact(payload)
    except ArtifactError as exc:
        raise HTTPException(status_code=502, detail={
            "message": str(exc),
            "snapshot_id": payload["snapshot_id"],
            "retry": "请使用同一 snapshot_id 重试下载，成功产物不会被覆盖",
        })
    return _file_response(record)


def _file_response(record: ArtifactRecord) -> FileResponse:
    assert record.path is not None
    filename = f"{record.module}-export-{record.snapshot_id}.json"
    return FileResponse(
        record.path,
        media_type="application/json; charset=utf-8",
        filename=filename,
        headers={
            "X-Snapshot-Id": record.snapshot_id,
            "X-Snapshot-Version": str(record.version),
            "X-Snapshot-Total": str(record.total),
        },
    )


def get_artifact(snapshot_id: str) -> FileResponse:
    """重试/补下载入口：成功产物直接复用；只有失败记录或未知 id 时分别报错。"""
    record = artifact_store.get(snapshot_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"导出产物 {snapshot_id} 不存在或已过期，请重新发起导出")
    if not record.ok or not record.path:
        raise HTTPException(status_code=409, detail={
            "message": f"该产物上次生成失败：{record.error or '未知错误'}",
            "snapshot_id": snapshot_id,
            "retry": "请重新调用导出下载接口重试",
        })
    return _file_response(record)


def translate_query_error(exc: QueryError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))

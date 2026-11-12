"""导出产物仓库：把一次清单导出的快照结果落盘，并提供可重试的下载入口。

一致性与重试语义：
- 每个产物由 (模块, 过滤条件, 快照版本) 决定 snapshot_id，天然对应“同一次导出”；
- 产物文件按 snapshot_id 唯一生成，写入走 “临时文件 -> 原子重命名”，
  成功后该文件永远不会被后续请求覆盖；
- 重试同一个 snapshot_id：已有成功产物直接复用，不重新生成、不覆盖文件；
  只失败过的记录允许再试一次，成功产物依旧落在一个新的唯一名下；
- 内存里只保留产物元数据（有上限），清单内容写文件，避免无上限拼接拖垮服务。
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterator

# 测试可以通过环境变量或 reset(directory=...) 指到临时目录，避免污染真实临时目录。
_DEFAULT_DIR = os.path.join(tempfile.gettempdir(), "metric-platform-exports")
_MAX_ENTRIES = 64


class ArtifactError(RuntimeError):
    """产物生成失败（含测试注入的失败），调用方可据此重试。"""


@dataclass(frozen=True)
class ArtifactRecord:
    snapshot_id: str
    module: str
    path: str | None
    ok: bool
    error: str | None
    total: int
    version: int
    filters: dict[str, str]


class ArtifactStore:
    def __init__(self, directory: str | None = None) -> None:
        self._lock = threading.RLock()
        self._dir = directory or _DEFAULT_DIR
        self._records: OrderedDict[str, ArtifactRecord] = OrderedDict()
        # snapshot_id -> 唯一序号，保证重试产生的新文件也不与旧文件撞名。
        self._seq = 0
        # 测试钩子：key 为 snapshot_id 时，下一次生成会抛出指定错误；
        # 生产代码不会往里写东西。
        self.faults: dict[str, str] = {}
        os.makedirs(self._dir, exist_ok=True)

    @staticmethod
    def build_snapshot_id(module: str, version: int, filters: dict[str, str]) -> str:
        """由模块、快照版本与规范化后的过滤条件拼出稳定标识。

        过滤条件部分用 URL 安全的 base64 编码：中文等非 ASCII 字符不会破坏
        HTTP 响应头/下载文件名（latin-1 限制），也不会被路径参数的
        百分号解码反向改写；同一条件永远得到同一标识。
        """
        parts = [f"{k}={filters[k]}" for k in sorted(filters) if filters[k]]
        raw = "-".join(parts) if parts else "all"
        token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{module}-v{version}-{token}"

    def get(self, snapshot_id: str) -> ArtifactRecord | None:
        with self._lock:
            return self._records.get(snapshot_id)

    def all_records(self) -> list[ArtifactRecord]:
        with self._lock:
            return list(self._records.values())

    def reset(self, directory: str | None = None) -> None:
        with self._lock:
            self._records.clear()
            self._seq = 0
            self.faults.clear()
            if directory is not None:
                self._dir = directory
            os.makedirs(self._dir, exist_ok=True)

    def _remember(self, record: ArtifactRecord) -> None:
        """登记元数据并按 LRU 淘汰最旧条目；淘汰只删元数据，成功文件保留在磁盘。"""
        with self._lock:
            self._records.pop(record.snapshot_id, None)
            self._records[record.snapshot_id] = record
            while len(self._records) > _MAX_ENTRIES:
                self._records.popitem(last=False)

    def produce(
        self,
        *,
        snapshot_id: str,
        module: str,
        version: int,
        total: int,
        filters: dict[str, str],
        chunk_iter: Callable[[], Iterator[list[dict[str, Any]]]],
        header: dict[str, Any],
    ) -> ArtifactRecord:
        """生成（或复用）一个导出产物。

        chunk_iter 返回按块产出的行迭代器，逐块写入临时文件，
        成功后原子重命名为唯一名；任何异常都不会留下半成品成功记录。
        """
        with self._lock:
            existing = self._records.get(snapshot_id)
            if existing is not None and existing.ok and existing.path and os.path.exists(existing.path):
                # 重试命中成功产物：直接复用，绝不重新生成或覆盖。
                self._records.move_to_end(snapshot_id)
                return existing
            # 精确 id 的故障只作用一次；"*" 是测试用的全局一次性故障。
            fault = self.faults.pop(snapshot_id, None) or self.faults.pop("*", None)
            self._seq += 1
            seq = self._seq

        if fault is not None:
            record = ArtifactRecord(
                snapshot_id=snapshot_id,
                module=module,
                path=None,
                ok=False,
                error=fault,
                total=total,
                version=version,
                filters=dict(filters),
            )
            self._remember(record)
            raise ArtifactError(fault)

        final_name = f"{snapshot_id}-{seq}.json"
        final_path = os.path.join(self._dir, final_name)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{snapshot_id}-", suffix=".tmp", dir=self._dir)
        try:
            written = 0
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                head = dict(header)
                head["snapshot_id"] = snapshot_id
                head["snapshot_version"] = version
                head["total"] = total
                head["filters"] = filters
                handle.write("{")
                first = True
                for key, value in head.items():
                    if not first:
                        handle.write(",")
                    handle.write(json.dumps(key, ensure_ascii=False))
                    handle.write(":")
                    handle.write(json.dumps(value, ensure_ascii=False))
                    first = False
                handle.write(",\"items\":[")
                row_first = True
                for chunk in chunk_iter():
                    for row in chunk:
                        if not row_first:
                            handle.write(",")
                        handle.write(json.dumps(row, ensure_ascii=False))
                        row_first = False
                        written += 1
                handle.write("]}")
            if written != total:
                raise ArtifactError(
                    f"导出快照不一致：过滤结果 {total} 条，实际写入 {written} 条，已丢弃本次产物"
                )
            os.replace(tmp_path, final_path)  # 同目录原子改名，失败时 tmp 由 except 清理
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            record = ArtifactRecord(
                snapshot_id=snapshot_id,
                module=module,
                path=None,
                ok=False,
                error="导出文件写入失败",
                total=total,
                version=version,
                filters=dict(filters),
            )
            self._remember(record)
            raise

        record = ArtifactRecord(
            snapshot_id=snapshot_id,
            module=module,
            path=final_path,
            ok=True,
            error=None,
            total=total,
            version=version,
            filters=dict(filters),
        )
        self._remember(record)
        return record


# 全进程单例：与内存仓库同生命周期，测试里用 reset() 清理。
artifact_store = ArtifactStore()

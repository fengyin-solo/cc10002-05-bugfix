"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

并发与快照约定：
- 所有读（snapshot/find）和写（insert/update）都走同一把可重入锁；
- 写入会让版本号 +1，读取通过 snapshot() 拿到某一版本下的深拷贝快照，
  之后即使别的线程改了线上数据，持有的快照也不会变，保证一次导出内部
  明细、总数、状态来自同一个一致时刻。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._version = 0
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    @contextmanager
    def locked(self) -> Iterator[None]:
        """对外暴露同一把锁，方便服务层组合多个仓库操作。"""
        with self._lock:
            yield

    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        # 历史调用方直接拿线上表；新代码请改用 snapshot()，避免遍历期间被并发写入。
        with self._lock:
            return self._tables.setdefault(module, [])

    def snapshot(self, module: str) -> tuple[list[dict[str, Any]], int]:
        """返回模块在同一时刻的深拷贝行集合与当前版本号。

        深拷贝在锁内完成：调用方拿到的列表与字典都是私有副本，
        后续任何并发写入都不会影响这份快照，适合清单导出这种
        “先取总数、再逐条产出”的长流程。
        """
        with self._lock:
            table = self._tables.setdefault(module, [])
            return [dict(row) for row in table], self._version

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._tables.setdefault(module, []):
                if int(row.get("id", 0)) == entry_id:
                    return row
        return None

    def insert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """在锁内追加一行并推进版本，返回写入内容的副本。"""
        with self._lock:
            table = self._tables.setdefault(module, [])
            table.append(row)
            self._version += 1
            return dict(row)

    def update(
        self,
        module: str,
        entry_id: int,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any] | None:
        """在锁内定位并修改一行，修改完成后推进版本并返回副本。

        查找与修改是同一个临界区，读侧要么看到改之前、要么看到改之后，
        不会看到改了一半的行。
        """
        with self._lock:
            target: dict[str, Any] | None = None
            for row in self._tables.setdefault(module, []):
                if int(row.get("id", 0)) == entry_id:
                    target = row
                    break
            if target is None:
                return None
            mutator(target)
            self._version += 1
            return dict(target)

    def next_id(self, module: str) -> int:
        with self._lock:
            return max((int(row.get("id", 0)) for row in self._tables.setdefault(module, [])), default=0) + 1

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def reset(self) -> None:
        """测试专用：恢复种子数据并清零版本号。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
            self._version = 0

    def overview(self) -> dict[str, object]:
        with self._lock:
            modules: list[dict[str, object]] = []
            for name in sorted(self._tables):
                rows = self._tables[name]
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
            cards = [
                {"label": "业务模块", "value": len(modules)},
                {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
                {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
                {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            ]
            return {"cards": cards, "modules": modules}


store = Store()

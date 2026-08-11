"""进度展示端口：用例只报告进度事件，展示由 CLI（Rich）负责。"""

from __future__ import annotations

from typing import Protocol


class ProgressReporter(Protocol):
    """批量任务的进度报告接口；无展示需求时传 None。"""

    def begin(self, total: int, phase: str) -> None: ...
    def advance(self, *, success: bool, idx: int, item_name: str) -> None: ...
    def end(self) -> None: ...

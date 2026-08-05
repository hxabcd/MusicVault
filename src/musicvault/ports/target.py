from __future__ import annotations

from pathlib import Path
from typing import Protocol


class TargetOperations(Protocol):
    """目标同步器可以使用的文件操作端口。"""

    def link(self, source: Path, destination: Path) -> None: ...

    def copy(self, source: Path, destination: Path) -> None: ...

    def write_text(self, destination: Path, content: str, encoding: str = "utf-8") -> None: ...

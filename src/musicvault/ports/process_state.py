"""处理管线状态端口：下载/处理进度与 raw 路径映射。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class ProcessStateRepository(Protocol):
    """处理管线状态（downloaded → processed）的读写能力。"""

    def mark_downloaded(self, path: str, track_id: int) -> None: ...

    def list_downloaded_track_ids(self) -> list[int]: ...

    def find_track_id_by_path(self, path: str) -> int | None: ...

    def mark_processed(self, track_id: int, updated_at: float) -> None: ...

    def is_processed(self, track_id: int, required_specs: set[str]) -> bool: ...

    def transaction(self) -> AbstractContextManager[Any]: ...

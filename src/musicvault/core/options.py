from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RunOptions:
    """运行参数"""

    playlist_id: int | None = None
    only_sync: bool = False
    only_process: bool = False
    include_translation: bool = True
    force: bool = False

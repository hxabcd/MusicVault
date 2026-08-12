from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """workspace 各生命周期区域的路径。"""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def media_store(self) -> Path:
        return self.root / "media_store"

    @property
    def library(self) -> Path:
        return self.root / "library"

    @property
    def state_db(self) -> Path:
        return self.root / "state.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> None:
        for path in (self.root, self.cache, self.media_store, self.library, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def media_asset_path(self, track_id: int, asset_type: str, filename: str) -> Path:
        """已废弃：<tid>/<asset_type>/ 旧布局（audio/ 段遗留）。

        扁平化后 canonical 与 .lrc 直接落 media_store/<tid>/，本方法仅被
        FileMediaStore.put 使用（同样无调用方）；新布局请直接拼 media_store/<tid>/。
        """
        return self.media_store / str(track_id) / asset_type / filename

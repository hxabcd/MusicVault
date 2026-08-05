from __future__ import annotations

import shutil
import time
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.domain.models import MediaAsset
from musicvault.shared.utils import same_file_content, sha256_file


class FileMediaStore:
    """media_store 的文件系统适配器，不暴露其目录布局给 preset。"""

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths
        self.paths.ensure()

    def put(
        self,
        source: str | Path,
        *,
        track_id: int,
        asset_type: str,
        spec: str,
        filename: str | None = None,
        source_name: str | None = None,
    ) -> MediaAsset:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"媒体源文件不存在：{source_path}")
        destination = self.paths.media_asset_path(track_id, asset_type, filename or source_path.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(source_path, destination)
        elif not same_file_content(source_path, destination):
            raise FileExistsError(f"媒体资产目标已存在且内容不同：{destination}")
        return MediaAsset(
            track_id=track_id,
            asset_type=asset_type,
            spec=spec,
            path=destination,
            size=destination.stat().st_size,
            sha256=sha256_file(destination),
            source=source_name or str(source_path),
            updated_at=time.time(),
        )

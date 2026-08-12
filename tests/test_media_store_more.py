"""FileMediaStore 补充单测：put 的复制、去重与异常分支。

覆盖：源文件缺失、首次复制、目标已存在（同内容放行 / 异内容抛错）、
自定义文件名与 source_name、目录自动创建、返回的 MediaAsset 字段完整性。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.filesystem.media_store import FileMediaStore
from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.shared.utils import sha256_file


def _store(tmp_path: Path) -> tuple[FileMediaStore, WorkspacePaths]:
    paths = WorkspacePaths(tmp_path / "ws")
    return FileMediaStore(paths), paths


class TestMediaStorePut:
    def test_init_creates_workspace_dirs(self, tmp_path) -> None:
        paths = WorkspacePaths(tmp_path / "ws")

        FileMediaStore(paths)

        for directory in ("ws", "ws/cache", "ws/media_store", "ws/library", "ws/logs"):
            assert (tmp_path / directory).is_dir()

    def test_missing_source_raises_file_not_found(self, tmp_path) -> None:
        store, _ = _store(tmp_path)

        with pytest.raises(FileNotFoundError, match="媒体源文件不存在"):
            store.put(tmp_path / "ghost.flac", track_id=1, asset_type="audio", spec="flac")

    def test_put_copies_and_returns_asset(self, tmp_path) -> None:
        store, paths = _store(tmp_path)
        source = tmp_path / "track.flac"
        source.write_bytes(b"flac-data")

        asset = store.put(source, track_id=1, asset_type="audio", spec="flac")

        destination = paths.media_asset_path(1, "audio", "track.flac")
        assert destination.read_bytes() == b"flac-data"
        assert asset.track_id == 1
        assert asset.asset_type == "audio"
        assert asset.spec == "flac"
        assert asset.path == destination
        assert asset.size == len(b"flac-data")
        assert asset.sha256 == sha256_file(destination)
        assert asset.source == str(source)
        assert isinstance(asset.updated_at, float)

    def test_put_existing_same_content_allowed(self, tmp_path) -> None:
        """目标已存在且内容相同 → 幂等放行，不重复复制。"""
        store, paths = _store(tmp_path)
        source = tmp_path / "track.flac"
        source.write_bytes(b"same-data")
        destination = paths.media_asset_path(1, "audio", "track.flac")
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"same-data")

        asset = store.put(source, track_id=1, asset_type="audio", spec="flac")

        assert asset.path == destination
        assert asset.sha256 == sha256_file(destination)

    def test_put_existing_different_content_raises(self, tmp_path) -> None:
        """目标已存在且内容不同 → FileExistsError 防止静默覆盖。"""
        store, paths = _store(tmp_path)
        source = tmp_path / "track.flac"
        source.write_bytes(b"new-data")
        destination = paths.media_asset_path(1, "audio", "track.flac")
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old-data")

        with pytest.raises(FileExistsError, match="目标已存在且内容不同"):
            store.put(source, track_id=1, asset_type="audio", spec="flac")

    def test_put_custom_filename(self, tmp_path) -> None:
        store, paths = _store(tmp_path)
        source = tmp_path / "track.flac"
        source.write_bytes(b"flac-data")

        asset = store.put(source, track_id=7, asset_type="audio", spec="flac", filename="canonical.flac")

        assert asset.path == paths.media_asset_path(7, "audio", "canonical.flac")
        assert asset.path.is_file()

    def test_put_source_name_recorded(self, tmp_path) -> None:
        store, _ = _store(tmp_path)
        source = tmp_path / "track.flac"
        source.write_bytes(b"flac-data")

        asset = store.put(source, track_id=1, asset_type="audio", spec="flac", source_name="remote.flac")

        assert asset.source == "remote.flac"

    def test_put_creates_nested_track_directories(self, tmp_path) -> None:
        store, _ = _store(tmp_path)
        source = tmp_path / "track.flac"
        source.write_bytes(b"flac-data")

        store.put(source, track_id=42, asset_type="cover", spec="300x300")

        assert (tmp_path / "ws" / "media_store" / "42" / "cover").is_dir()

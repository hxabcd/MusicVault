from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from musicvault.adapters.filesystem.media_store import FileMediaStore
from musicvault.adapters.filesystem.workspace import WorkspaceMigration, WorkspacePaths
from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository


def test_legacy_audio_is_copied_idempotently_to_media_store(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "123.flac").write_bytes(b"audio")
    (downloads / "not-a-track.bin").write_bytes(b"keep")
    (downloads / "cache").mkdir()
    (downloads / "cache" / "partial.tmp").write_bytes(b"partial")

    paths = WorkspacePaths(tmp_path)
    first = WorkspaceMigration(paths).migrate()
    second = WorkspaceMigration(paths).migrate()

    destination = tmp_path / "media_store" / "123" / "audio" / "123.flac"
    assert first.copied_assets == 1
    assert second.copied_assets == 0
    assert destination.read_bytes() == b"audio"
    assert first.copied_cache_files == 1
    assert (tmp_path / "cache" / "partial.tmp").read_bytes() == b"partial"
    assert (downloads / "not-a-track.bin").exists()
    assert (downloads / "123.flac").exists()


def test_migration_imports_legacy_relationships_into_sqlite(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "123.flac").write_bytes(b"audio")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "synced_tracks.json").write_text(json.dumps({"ids": {"123": [10]}}), encoding="utf-8")
    (state_dir / "playlists.json").write_text(json.dumps({"10": {"name": "歌单"}}), encoding="utf-8")

    paths = WorkspacePaths(tmp_path)
    repository = SQLiteStateRepository(SQLiteState(paths.state_db))
    report = WorkspaceMigration(paths, repository).migrate()

    assert report.imported_tracks == 1
    assert report.imported_playlists == 1
    assert repository.create_snapshot().playlists[0].track_ids == (123,)
    assert repository.list_media_assets(123)[0].spec == "FLAC"
    assert repository.list_media_assets(123)[0].sha256


def test_file_media_store_put_is_idempotent_and_returns_metadata(tmp_path: Path) -> None:
    content = b"audio-content"
    source = tmp_path / "src.flac"
    source.write_bytes(content)
    store = FileMediaStore(WorkspacePaths(tmp_path))

    first = store.put(source, track_id=123, asset_type="audio", spec="FLAC")
    second = store.put(source, track_id=123, asset_type="audio", spec="FLAC")

    destination = tmp_path / "media_store" / "123" / "audio" / "src.flac"
    assert first.path == destination
    assert first.size == len(content)
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert destination.read_bytes() == content
    # 幂等：重复 put 不抛错，返回元数据（除 updated_at 外）保持一致
    assert second.size == first.size
    assert second.sha256 == first.sha256
    assert second.path == first.path
    assert destination.read_bytes() == content


def test_file_media_store_put_conflict_raises_without_overwrite(tmp_path: Path) -> None:
    store = FileMediaStore(WorkspacePaths(tmp_path))
    first = tmp_path / "a.flac"
    first.write_bytes(b"content-A")
    store.put(first, track_id=123, asset_type="audio", spec="FLAC")
    second = tmp_path / "b.flac"
    second.write_bytes(b"content-B")

    with pytest.raises(FileExistsError):
        store.put(second, track_id=123, asset_type="audio", spec="FLAC", filename="a.flac")

    destination = tmp_path / "media_store" / "123" / "audio" / "a.flac"
    assert destination.read_bytes() == b"content-A"


def test_migration_conflicting_target_raises_without_overwrite(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "123.flac").write_bytes(b"audio")
    destination = tmp_path / "media_store" / "123" / "audio" / "123.flac"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"different")

    with pytest.raises(FileExistsError):
        WorkspaceMigration(WorkspacePaths(tmp_path)).migrate()

    assert destination.read_bytes() == b"different"


def test_cache_migration_copies_then_skips_on_repeat(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    cache = downloads / "cache"
    cache.mkdir(parents=True)
    (cache / "partial.tmp").write_bytes(b"partial")
    (cache / "sub").mkdir()
    (cache / "sub" / "nested.tmp").write_bytes(b"nested")
    paths = WorkspacePaths(tmp_path)

    first = WorkspaceMigration(paths).migrate()
    second = WorkspaceMigration(paths).migrate()

    assert first.copied_cache_files == 2
    assert first.skipped_cache_files == 0
    assert second.copied_cache_files == 0
    assert second.skipped_cache_files == 2
    assert (tmp_path / "cache" / "partial.tmp").read_bytes() == b"partial"
    assert (tmp_path / "cache" / "sub" / "nested.tmp").read_bytes() == b"nested"

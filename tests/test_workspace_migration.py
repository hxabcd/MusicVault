from __future__ import annotations

import json
from pathlib import Path

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.adapters.filesystem.workspace import WorkspaceMigration, WorkspacePaths


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

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.state.sqlite import SCHEMA_VERSION, SQLiteState, SQLiteStateRepository
from musicvault.domain.models import Track
from musicvault.domain.models import MediaAsset, Playlist


def _track(track_id: int = 1) -> Track:
    return Track(
        id=track_id,
        name=f"曲目 {track_id}",
        artists=["歌手"],
        album="专辑",
        aliases=["别名"],
        raw={"source": "test"},
    )


def test_initialize_creates_versioned_minimal_schema(tmp_path: Path) -> None:
    database = SQLiteState(tmp_path / "state.db")

    database.initialize()

    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert version == 2
    assert {
        "tracks",
        "playlists",
        "playlist_tracks",
        "managed_songs",
        "media_assets",
        "preset_registry",
        "export_targets",
        "processed_tracks",
        "pending_files",
    } <= tables


def test_repository_round_trip_and_snapshot_are_atomic(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))
    track = _track()
    playlist = Playlist(id=10, name="歌单", track_ids=(track.id,))
    asset = MediaAsset(
        track_id=track.id,
        asset_type="audio",
        spec="FLAC",
        path=tmp_path / "media_store" / "1" / "audio" / "1.flac",
        sha256="abc",
    )

    repo.save_source_state([track], [playlist], [asset])

    loaded = repo.get_track(track.id)
    snapshot = repo.create_snapshot()
    assert loaded is not None
    assert loaded.name == track.name
    assert snapshot.tracks[0].id == track.id
    assert snapshot.playlists[0].track_ids == (track.id,)
    assert snapshot.media_assets[0].sha256 == "abc"
    assert snapshot.snapshot_hash

    with pytest.raises(RuntimeError), repo.transaction() as connection:
        repo.upsert_track(_track(2), connection=connection)
        raise RuntimeError("模拟事务回滚")
    assert repo.get_track(2) is None


def test_unique_media_asset_is_replaced_not_duplicated(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))
    first = MediaAsset(track_id=1, asset_type="audio", spec="MP3-192k", path=tmp_path / "a.mp3")
    second = MediaAsset(track_id=1, asset_type="audio", spec="MP3-192k", path=tmp_path / "b.mp3")

    repo.upsert_track(_track())
    repo.upsert_media_asset(first)
    repo.upsert_media_asset(second)

    assets = repo.list_media_assets(track_id=1)
    assert len(assets) == 1
    assert assets[0].path == tmp_path / "b.mp3"


def test_preset_registry_kind_column_defaults_to_target(tmp_path: Path) -> None:
    """preset_registry 的 kind 列默认 'target'：旧行（v1 时代写入）迁移后兼容。"""
    path = tmp_path / "state.db"
    # 模拟 v1 时代写入的行：无 kind 列
    database = SQLiteState(path)
    with database.connect() as connection:
        connection.execute(
            "CREATE TABLE preset_registry (name TEXT PRIMARY KEY, source TEXT NOT NULL,"
            " api_version TEXT NOT NULL, enabled INTEGER NOT NULL, script_hash TEXT)"
        )
    SQLiteState(path).initialize()  # 迁移到 v2：补 kind 列

    with database.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(preset_registry)").fetchall()}
    assert "kind" in columns
    repo = SQLiteStateRepository(SQLiteState(path))
    repo.register_preset(name="old", source="builtin:old", api_version="v1")
    assert repo.list_registered_presets()[0].kind == "target"


def test_register_preset_with_kind_roundtrip(tmp_path: Path) -> None:
    """register_preset 按 kind 登记：preset/target 两类注册可区分。"""
    repo = SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.register_preset(name="archive", source="builtin:archive", api_version="v1", kind="preset")
    repo.register_preset(name="hardlink", source="builtin:hardlink", api_version="v1", kind="target")

    by_name = {item.name: item for item in repo.list_registered_presets()}
    assert by_name["archive"].kind == "preset"
    assert by_name["hardlink"].kind == "target"


def test_initialize_rejects_newer_database_version(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    SQLiteState(path).initialize()
    with SQLiteState(path).connect() as connection:
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION + 1,))

    with pytest.raises(RuntimeError, match="高于"):
        SQLiteState(path).initialize()


def test_transaction_rollback_keeps_prior_committed_state(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))

    with pytest.raises(RuntimeError), repo.transaction() as connection:
        repo.upsert_track(_track(2), connection=connection)
        raise RuntimeError("模拟回滚")

    assert repo.get_track(1) is not None
    assert repo.get_track(2) is None


def test_deleting_playlist_cascades_playlist_tracks(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.save_source_state([_track()], [Playlist(id=10, name="歌单", track_ids=(1,))], [])

    with repo.database.connect() as connection:
        connection.execute("DELETE FROM playlists WHERE id = 10")

    with repo.database.connect() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = 10").fetchone()[0]
    assert remaining == 0


def test_is_processed_requires_spec_coverage_and_record(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))
    repo.upsert_media_asset(MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=tmp_path / "1.flac"))
    repo.record_processed(1, "hash", 0.0)

    assert repo.is_processed(1, {"FLAC"})
    # spec 未覆盖 → 未处理
    assert not repo.is_processed(1, {"FLAC", "MP3-192k"})
    # 无处理记录 → 未处理
    repo.upsert_track(_track(2))
    assert not repo.is_processed(2, {"FLAC"})


def test_remove_track_cascades_processed_and_pending(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))
    repo.record_processed(1, "hash", 0.0)
    repo.add_pending_file("downloads/cache/1.mp3", 1)

    repo.remove_track(1)

    assert repo.get_track(1) is None
    assert repo.find_track_id_by_path("downloads/cache/1.mp3") is None
    assert not repo.is_processed(1, set())

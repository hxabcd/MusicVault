from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.state.sqlite import (
    SQLiteProcessStateRepository,
    SQLiteSourceStateRepository,
    SQLiteState,
)
from musicvault.domain.models import MediaAsset, Playlist, Track


def _track(track_id: int = 1) -> Track:
    return Track(
        id=track_id,
        name=f"曲目 {track_id}",
        artists=["歌手"],
        album="专辑",
        aliases=["别名"],
        raw={"source": "test"},
    )


def _source_repo(tmp_path: Path) -> SQLiteSourceStateRepository:
    return SQLiteSourceStateRepository(SQLiteState(tmp_path / "state.db"))


def test_initialize_creates_new_schema(tmp_path: Path) -> None:
    database = SQLiteState(tmp_path / "state.db")

    database.initialize()

    with database.connect() as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert {
        "tracks",
        "playlists",
        "playlist_tracks",
        "managed_tracks",
        "media_assets",
        "processing_state",
    } == tables


def test_initialize_rejects_legacy_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with SQLiteState(path).connect() as connection:
        connection.execute("CREATE TABLE preset_registry (name TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="旧格式数据库"):
        SQLiteState(path).initialize()


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = SQLiteState(tmp_path / "state.db")

    database.initialize()
    database.initialize()

    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count == 0


def test_repository_round_trip_and_snapshot_are_atomic(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
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
    repo = _source_repo(tmp_path)
    first = MediaAsset(track_id=1, asset_type="audio", spec="MP3-192k", path=tmp_path / "a.mp3")
    second = MediaAsset(track_id=1, asset_type="audio", spec="MP3-192k", path=tmp_path / "b.mp3")

    repo.upsert_track(_track())
    repo.upsert_media_asset(first)
    repo.upsert_media_asset(second)

    assets = repo.list_media_assets(track_id=1)
    assert len(assets) == 1
    assert assets[0].path == tmp_path / "b.mp3"


def test_lyrics_rows_are_hidden_from_media_assets(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    repo.upsert_track(_track(42))
    repo.save_lyrics(42, "[]", 0.0)

    assert repo.list_media_assets(track_id=42) == []
    assert repo.get_lyrics(42) == "[]"


def test_transaction_rollback_keeps_prior_committed_state(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    repo.upsert_track(_track(1))

    with pytest.raises(RuntimeError), repo.transaction() as connection:
        repo.upsert_track(_track(2), connection=connection)
        raise RuntimeError("模拟回滚")

    assert repo.get_track(1) is not None
    assert repo.get_track(2) is None


def test_deleting_playlist_cascades_playlist_tracks(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    repo.save_source_state([_track()], [Playlist(id=10, name="歌单", track_ids=(1,))], [])

    with repo.database.connect() as connection:
        connection.execute("DELETE FROM playlists WHERE id = 10")

    with repo.database.connect() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = 10").fetchone()[0]
    assert remaining == 0


def test_is_processed_requires_processed_state_and_spec_coverage(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    process = SQLiteProcessStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))
    repo.upsert_media_asset(MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=tmp_path / "1.flac"))
    process.mark_processed(1, 0.0)

    assert process.is_processed(1, {"FLAC"})
    # spec 未覆盖 → 未处理
    assert not process.is_processed(1, {"FLAC", "MP3-192k"})
    # 无处理状态 → 未处理
    repo.upsert_track(_track(2))
    assert not process.is_processed(2, {"FLAC"})


def test_downloaded_state_transitions_to_processed(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    process = SQLiteProcessStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))

    process.mark_downloaded("cache/1.mp3", 1)
    assert process.list_downloaded_track_ids() == [1]
    assert process.find_track_id_by_path("cache/1.mp3") == 1
    assert not process.is_processed(1, set())

    process.mark_processed(1, 0.0)
    assert process.list_downloaded_track_ids() == []
    assert process.find_track_id_by_path("cache/1.mp3") is None
    assert process.is_processed(1, set())


def test_remove_track_cascades_processing_state_and_lyrics(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    process = SQLiteProcessStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))
    process.mark_downloaded("cache/1.mp3", 1)
    repo.save_lyrics(1, "[]", 0.0)

    repo.remove_track(1)

    assert repo.get_track(1) is None
    assert process.find_track_id_by_path("cache/1.mp3") is None
    assert not process.is_processed(1, set())
    assert repo.get_lyrics(1) is None

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.core.models import Track
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
    assert version == 1
    assert {
        "tracks",
        "playlists",
        "playlist_tracks",
        "managed_songs",
        "media_assets",
        "preset_registry",
        "export_targets",
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

    with pytest.raises(RuntimeError):
        with repo.transaction() as connection:
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

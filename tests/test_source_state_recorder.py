from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.source_state import SourceStateRecorder, build_audio_asset_from_file
from musicvault.domain.models import Track
from musicvault.domain.models import Playlist


def _track(track_id: int = 1) -> Track:
    return Track(
        id=track_id,
        name=f"曲目 {track_id}",
        artists=["歌手"],
        album="专辑",
        aliases=["别名"],
        raw={"source": "test"},
    )


def _repository(tmp_path: Path) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))


def test_recorder_persists_tracks_playlists_and_managed_songs(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    recorder = SourceStateRecorder(repo)
    playlist = Playlist(id=10, name="歌单A", track_ids=(1, 2))

    recorder.record_source_state([_track(1), _track(2)], [playlist], managed_songs=[1])

    snapshot = repo.create_snapshot()
    assert [track.id for track in snapshot.tracks] == [1, 2]
    assert snapshot.playlists[0].name == "歌单A"
    assert snapshot.playlists[0].track_ids == (1, 2)


def test_recorder_persists_media_assets(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    audio = tmp_path / "downloads" / "1.flac"
    audio.parent.mkdir()
    audio.write_bytes(b"fake flac")
    recorder = SourceStateRecorder(repo)

    recorder.record_source_state([_track()], media_assets=[build_audio_asset_from_file(1, "FLAC", audio)])

    assets = repo.list_media_assets(1)
    assert len(assets) == 1
    assert assets[0].spec == "FLAC"
    assert assets[0].asset_type == "audio"
    assert assets[0].path == audio
    assert assets[0].size == len(b"fake flac")
    assert assets[0].sha256
    assert assets[0].source == "pipeline:downloads"


def test_recorder_is_idempotent(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    recorder = SourceStateRecorder(repo)
    playlist = Playlist(id=10, name="歌单A", track_ids=(1,))

    for _ in range(2):
        recorder.record_source_state([_track()], [playlist])

    assert len(repo.list_tracks()) == 1
    assert len(repo.list_playlists()) == 1


def test_recorder_transaction_is_atomic(tmp_path: Path) -> None:
    """managed_song 引用不存在的曲目时，整个事务应回滚，不留下部分写入。"""
    repo = _repository(tmp_path)
    recorder = SourceStateRecorder(repo)
    playlist = Playlist(id=10, name="歌单A", track_ids=(1,))

    with pytest.raises(Exception):
        recorder.record_source_state([_track(1)], [playlist], managed_songs=[999])

    assert repo.list_tracks() == []
    assert repo.list_playlists() == []


def test_build_audio_asset_computes_metadata(tmp_path: Path) -> None:
    audio = tmp_path / "media_store" / "2" / "audio" / "2.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"x" * 10)

    asset = build_audio_asset_from_file(2, "MP3-192k", audio, source="test:src")

    assert asset.track_id == 2
    assert asset.asset_type == "audio"
    assert asset.spec == "MP3-192k"
    assert asset.path == audio
    assert asset.size == 10
    assert asset.sha256
    assert asset.source == "test:src"
    assert asset.updated_at is not None

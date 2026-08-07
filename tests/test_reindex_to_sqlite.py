from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.core.config import Config
from musicvault.services.run_service import RunService
from musicvault.shared.utils import save_json


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def _repository(cfg: Config) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(cfg.state_db_file))


def _setup_downloads(cfg: Config) -> None:
    cfg.downloads_dir.mkdir(parents=True)
    (cfg.downloads_dir / "111.flac").write_bytes(b"fake flac 111")
    (cfg.downloads_dir / "222.mp3").write_bytes(b"fake mp3 222")


def _setup_playlists(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True)
    save_json(cfg.state_dir / "playlists.json", {"10": {"name": "歌单A", "track_count": 1}})


def _setup_library_links(cfg: Config) -> None:
    # library/<preset>/歌单A/ 下硬链接到 downloads 的 canonical 文件
    pl_dir = cfg.preset_dir("archive") / "歌单A"
    pl_dir.mkdir(parents=True)
    os.link(cfg.downloads_dir / "111.flac", pl_dir / "Artist - Song.flac")


def test_rebuild_index_records_tracks_playlists_and_assets(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    _setup_downloads(cfg)
    _setup_playlists(cfg)
    _setup_library_links(cfg)
    repo = _repository(cfg)

    service = RunService(cfg, api=MagicMock(), state=repo)
    track_count, playlist_count = service.rebuild_index()

    assert track_count == 2
    assert playlist_count == 1

    snapshot = repo.create_snapshot()
    assert [track.id for track in snapshot.tracks] == [111, 222]
    assert snapshot.playlists[0].name == "歌单A"
    assert snapshot.playlists[0].track_ids == (111,)
    assert len(snapshot.media_assets) == 2
    asset = snapshot.assets_for(111, "audio")[0]
    assert asset.spec == "FLAC"
    assert asset.path == cfg.downloads_dir / "111.flac"
    assert asset.source == "pipeline:reindex"


def test_rebuild_index_writes_sqlite_state(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    _setup_downloads(cfg)
    _setup_playlists(cfg)
    _setup_library_links(cfg)
    repo = _repository(cfg)

    service = RunService(cfg, api=MagicMock(), state=repo)
    track_count, playlist_count = service.rebuild_index()

    assert track_count == 2
    assert playlist_count == 1
    synced = service.sync_service._load_synced_state()
    assert synced == {111: [10], 222: []}
    assert not (cfg.state_dir / "synced_tracks.json").exists()


def test_rebuild_index_does_not_overwrite_known_track_metadata(tmp_path: Path) -> None:
    from musicvault.core.models import Track

    cfg = _make_cfg(tmp_path)
    _setup_downloads(cfg)
    _setup_playlists(cfg)
    _setup_library_links(cfg)
    repo = _repository(cfg)
    # 先登记真实元数据（模拟 msv sync 写入）
    repo.upsert_track(Track(id=111, name="真实歌名", artists=["歌手"], album="真实专辑", raw={}))

    RunService(cfg, api=MagicMock(), state=repo).rebuild_index()

    snapshot = repo.create_snapshot()
    track = snapshot.track(111)
    assert track is not None
    assert track.name == "真实歌名"
    assert track.album == "真实专辑"


def test_rebuild_index_skips_playlists_without_members(tmp_path: Path) -> None:
    from musicvault.core.models import Track
    from musicvault.domain.models import Playlist

    cfg = _make_cfg(tmp_path)
    _setup_downloads(cfg)
    _setup_playlists(cfg)
    _setup_library_links(cfg)
    repo = _repository(cfg)
    # 模拟 sync 已录：歌单 20 有真实曲目关系，但磁盘上无对应文件
    repo.upsert_track(Track(id=333, name="曲目", artists=[], album="专辑", raw={}))
    repo.upsert_playlist(Playlist(id=20, name="歌单B", track_ids=(333,)))

    RunService(cfg, api=MagicMock(), state=repo).rebuild_index()

    snapshot = repo.create_snapshot()
    playlist_b = snapshot.playlist(20)
    assert playlist_b is not None
    assert playlist_b.track_ids == (333,)

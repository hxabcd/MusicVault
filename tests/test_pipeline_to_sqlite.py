from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.application.bootstrap import build_runtime
from musicvault.application.source_state import SourceStateRecorder, build_audio_asset_from_file
from musicvault.application.sync_engine import SyncEngine
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.services.process_service import ProcessService
from musicvault.services.sync_service import SyncService


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def _make_track(track_id: int) -> Track:
    return Track(
        id=track_id,
        name=f"Song {track_id}",
        artists=["Artist"],
        album="Album",
        raw={},
    )


def _repository(cfg: Config) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(cfg.state_db_file))


def test_sync_records_tracks_and_playlists_to_sqlite(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    repo = _repository(cfg)

    api = MagicMock()
    api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
    api.get_playlist_tracks.return_value = [_make_track(111)]
    api.get_tracks_download_urls.return_value = {111: "http://example.com/111.mp3"}
    downloader = MagicMock()
    downloader.download_track.return_value = DownloadedTrack(
        track=_make_track(111),
        source_file=str(cfg.downloads_cache_dir / "111.mp3"),
        is_ncm=False,
        playlist_ids=[10],
    )

    SyncService(cfg, api, downloader, workers=2, dry_run=False, state=repo).run_sync("cookie", playlist_ids=[10])

    snapshot = repo.create_snapshot()
    assert [track.id for track in snapshot.tracks] == [111]
    assert snapshot.playlists[0].name == "歌单A"
    assert snapshot.playlists[0].track_ids == (111,)


def test_sync_records_managed_songs_to_sqlite(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    cfg.add_song(999)
    repo = _repository(cfg)

    api = MagicMock()
    api.get_tracks_detail.return_value = {999: _make_track(999)}
    downloader = MagicMock()

    SyncService(cfg, api, downloader, workers=2, dry_run=False, state=repo).run_sync("cookie", playlist_ids=[])

    snapshot = repo.create_snapshot()
    assert 999 in [track.id for track in snapshot.tracks]


def test_dry_run_sync_does_not_write_sqlite(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    repo = _repository(cfg)

    api = MagicMock()
    api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
    api.get_playlist_tracks.return_value = [_make_track(111)]
    api.get_tracks_download_urls.return_value = {111: "http://example.com/111.mp3"}

    SyncService(cfg, api, MagicMock(), workers=2, dry_run=True, state=repo).run_sync("cookie", playlist_ids=[10])

    assert repo.create_snapshot().tracks == ()


def test_process_records_media_assets_to_sqlite(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    cfg.presets = []  # 简化：只把 canonical 文件本身登记为媒体资产
    canonical = cfg.downloads_dir / "333.flac"
    canonical.write_bytes(b"fake flac")
    repo = _repository(cfg)

    api = MagicMock()
    api.get_track_lyrics.return_value = {}
    item = DownloadedTrack(track=_make_track(333), source_file=str(canonical), is_ncm=False, playlist_ids=[])
    svc = ProcessService(
        cfg, api, MagicMock(), MagicMock(), MagicMock(), workers=1, dry_run=False, state=repo
    )
    svc.run_process(downloaded=[item], force=False)

    snapshot = repo.create_snapshot()
    assert [track.id for track in snapshot.tracks] == [333]
    assert len(snapshot.media_assets) == 1
    asset = snapshot.media_assets[0]
    assert asset.track_id == 333
    assert asset.spec == "FLAC"
    assert asset.path == canonical
    assert asset.sha256


def test_sync_with_stale_song_id_does_not_crash(tmp_path: Path) -> None:
    """远端已删除的单曲不应触发 managed_songs 外键违反，sync 应正常完成。"""
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    cfg.add_song(999)
    cfg.add_song(1000)  # 该 id 已被远端删除
    repo = _repository(cfg)

    api = MagicMock()
    api.get_tracks_detail.return_value = {999: _make_track(999)}  # 1000 缺失
    downloader = MagicMock()

    SyncService(cfg, api, downloader, workers=2, dry_run=False, state=repo).run_sync("cookie", playlist_ids=[])

    snapshot = repo.create_snapshot()
    assert [track.id for track in snapshot.tracks] == [999]
    # 陈旧 id 已从 songs.json 移除，且未写入 SQLite
    assert cfg.get_song_ids() == [999]


def test_synced_state_feeds_target_sync_closed_loop(tmp_path: Path) -> None:
    """sync 写入 SQLite → 登记媒体资产 → target-sync 消费快照并生成 playlist_links。"""
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    repo = _repository(cfg)

    # 1) sync 写入曲目与歌单
    api = MagicMock()
    api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
    api.get_playlist_tracks.return_value = [_make_track(111)]
    api.get_tracks_download_urls.return_value = {111: "http://example.com/111.mp3"}
    downloader = MagicMock()
    downloader.download_track.return_value = DownloadedTrack(
        track=_make_track(111),
        source_file=str(cfg.downloads_dir / "111.flac"),
        is_ncm=False,
        playlist_ids=[10],
    )
    SyncService(cfg, api, downloader, workers=2, dry_run=False, state=repo).run_sync("cookie", playlist_ids=[10])

    # 2) 模拟 process 产出 canonical 音频并登记媒体资产
    canonical = cfg.downloads_dir / "111.flac"
    canonical.write_bytes(b"fake flac")
    SourceStateRecorder(repo).record_source_state(
        [], [], media_assets=[build_audio_asset_from_file(111, "FLAC", canonical)]
    )

    # 3) target-sync 消费 SQLite 快照
    runtime = build_runtime(cfg)
    result = SyncEngine(FilesystemTarget(runtime.paths.library), dry_run=False).run(
        repo.create_snapshot(),
        runtime.presets.registrations(enabled_only=True),
    )

    assert result.status == "succeeded"
    link = runtime.paths.library / "playlist_links" / "歌单A" / "Artist - Song 111.flac"
    assert link.exists()

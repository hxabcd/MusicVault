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
    api.get_tracks_download_urls.return_value = {}
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
    api.get_tracks_download_urls.return_value = {}
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


def test_sync_no_longer_writes_synced_tracks_json(tmp_path: Path) -> None:
    """synced_tracks.json 被 SQLite 完全替代：运行后不再产生该文件。"""
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

    assert not (cfg.state_dir / "synced_tracks.json").exists()


def test_second_sync_reads_synced_state_from_sqlite(tmp_path: Path) -> None:
    """第二次 sync 从 SQLite 识别已同步曲目，不重复下载，也不依赖 JSON。"""
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    repo = _repository(cfg)

    def _api() -> MagicMock:
        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
        api.get_playlist_tracks.return_value = [_make_track(111)]
        api.get_tracks_download_urls.return_value = {111: "http://example.com/111.mp3"}
        return api

    def _real_downloader() -> MagicMock:
        """真实落盘，避免 processed_files.json 的 stale 清理干扰本切片。"""
        downloader = MagicMock()

        def _download(track: Track, url: str, dest: Path) -> DownloadedTrack:
            file = dest / f"{track.id}.mp3"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"fake mp3")
            return DownloadedTrack(track=track, source_file=str(file), is_ncm=False, playlist_ids=[10])

        downloader.download_track.side_effect = _download
        return downloader

    SyncService(cfg, _api(), _real_downloader(), workers=2, dry_run=False, state=repo).run_sync(
        "cookie", playlist_ids=[10]
    )

    second_downloader = _real_downloader()
    SyncService(cfg, _api(), second_downloader, workers=2, dry_run=False, state=repo).run_sync(
        "cookie", playlist_ids=[10]
    )

    second_downloader.download_track.assert_not_called()


def test_process_no_longer_writes_processed_json(tmp_path: Path) -> None:
    """processed_files.json 被 SQLite 完全替代：处理完成后不再产生该文件。"""
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
    svc = ProcessService(cfg, api, MagicMock(), MagicMock(), MagicMock(), workers=1, dry_run=False, state=repo)
    svc.run_process(downloaded=[item], force=False)

    assert not (cfg.state_dir / "processed_files.json").exists()


def test_second_process_skips_when_specs_covered(tmp_path: Path) -> None:
    """media_assets 覆盖全部必需 spec 且 processed_tracks 有记录 → 第二次跳过。"""
    from musicvault.core.preset import compute_preset_hash

    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    # 预置：track 333 的 media_assets 覆盖默认 presets 的全部 spec（FLAC + MP3-192k）
    flac = cfg.downloads_dir / "333.flac"
    flac.write_bytes(b"fake flac")
    mp3 = cfg.downloads_dir / "333_192k.mp3"
    mp3.write_bytes(b"fake mp3")
    SourceStateRecorder(repo).record_source_state(
        [_make_track(333)],
        media_assets=[
            build_audio_asset_from_file(333, "FLAC", flac),
            build_audio_asset_from_file(333, "MP3-192k", mp3),
        ],
    )
    repo.record_processed(333, compute_preset_hash(cfg.presets), 0.0)

    api = MagicMock()
    api.get_track_lyrics.return_value = {}
    organizer = MagicMock()
    item = DownloadedTrack(track=_make_track(333), source_file=str(flac), is_ncm=False, playlist_ids=[])
    svc = ProcessService(cfg, api, MagicMock(), organizer, MagicMock(), workers=1, dry_run=False, state=repo)
    svc.run_process(downloaded=[item], force=False)

    organizer.route_audio.assert_not_called()


def test_guess_track_id_reads_pending_files(tmp_path: Path) -> None:
    """raw→track 映射存 SQLite：_guess_track_id 从 pending_files 反查。"""
    from musicvault.shared.utils import workspace_rel_path

    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    cfg.downloads_dir.mkdir(parents=True)
    cfg.downloads_cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    raw = cfg.downloads_cache_dir / "Artist - Song.mp3"
    raw.parent.mkdir(parents=True, exist_ok=True)
    rel = workspace_rel_path(raw, cfg.workspace_path)
    repo.add_pending_file(rel, 333)

    svc = ProcessService(cfg, MagicMock(), MagicMock(), MagicMock(), MagicMock(), workers=1, state=repo)
    assert svc._guess_track_id(raw) == 333

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.source_state import SourceStateRecorder
from musicvault.core.config import Config
from musicvault.domain.models import DownloadedTrack, Track
from musicvault.domain.models import Playlist
from musicvault.services.process_service import ProcessService
from musicvault.services.sync_service import SyncService
from musicvault.shared.utils import save_json


def _make_cfg(tmp_path: Path) -> Config:
    """真实 Config，workspace 指向临时目录。"""
    cfg = Config(workspace=str(tmp_path / "ws"))
    return cfg


def _make_track(track_id: int) -> Track:
    return Track(
        id=track_id,
        name=f"Song {track_id}",
        artists=["Test Artist"],
        album="Test Album",
        cover_url=None,
        raw={},
    )


def _repository(cfg: Config) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(cfg.state_db_file))


def _seed_synced(cfg: Config, state_map: dict[int, list[int]]) -> None:
    """把 {track_id: [playlist_ids]} 写入 SQLite，替代旧 synced_tracks.json 预置。"""
    repo = _repository(cfg)
    tracks = [Track(id=tid, name=f"Song {tid}", artists=[], album="Album", raw={}) for tid in state_map]
    playlists: dict[int, Playlist] = {}
    for tid, pids in state_map.items():
        for pid in pids:
            playlists.setdefault(pid, Playlist(pid, f"歌单{pid}", ()))
    for pid, playlist in playlists.items():
        object.__setattr__(playlist, "track_ids", tuple(tid for tid, pids in state_map.items() if pid in pids))
    SourceStateRecorder(repo).record_source_state(tracks, playlists.values())


class TestSyncDryRun:
    def test_new_track_reported_no_writes(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        cfg.downloads_cache_dir.mkdir(parents=True)
        # 已有 1 首已同步，playlists.json 索引已存在
        _seed_synced(cfg, {111: [10]})
        save_json(cfg.state_dir / "playlists.json", {"10": {"name": "歌单A", "track_count": 2}})

        # API：歌单信息变化（正式运行会更新索引文件）、曲目新增 222
        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 99}
        api.get_playlist_tracks.return_value = [_make_track(111), _make_track(222)]
        api.get_tracks_download_urls.return_value = {222: "http://example.com/222.mp3"}

        downloader = MagicMock()
        svc = SyncService(cfg, api, downloader, workers=2, dry_run=True, state=_repository(cfg))
        downloaded = svc.run_sync("cookie", playlist_ids=[10])

        # 不下载、不写状态
        assert downloaded == []
        downloader.download_track.assert_not_called()
        state_map = svc._load_synced_state()
        assert 222 not in state_map
        # playlists.json 保持原内容（正式运行会更新 track_count）
        raw = json.loads((cfg.state_dir / "playlists.json").read_text(encoding="utf-8"))
        assert raw == {"10": {"name": "歌单A", "track_count": 2}}

        # 计划包含新曲目与歌单信息变化
        assert [t.id for t in svc.plan["with_url"]] == [222]
        assert svc.plan["pruned"] == []

    def test_prune_reported_files_kept(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        cfg.downloads_cache_dir.mkdir(parents=True)
        # 本地有 111（远端已删除）和 222（远端仍在）
        _seed_synced(cfg, {111: [10], 222: [10]})
        canonical = cfg.downloads_dir / "111.flac"
        canonical.write_text("fake flac")

        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
        api.get_playlist_tracks.return_value = [_make_track(222)]

        svc = SyncService(cfg, api, MagicMock(), workers=2, dry_run=True, state=_repository(cfg))
        svc.run_sync("cookie", playlist_ids=[10])

        # 111 列入清理计划，但文件与状态均保留
        assert svc.plan["pruned"] == [111]
        assert canonical.exists()
        state_map = svc._load_synced_state()
        assert 111 in state_map

    def test_normal_mode_writes_to_sqlite(self, tmp_path: Path) -> None:
        """回归：dry_run=False 时下载并把状态写入 SQLite（不再写 JSON）。"""
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

        svc = SyncService(cfg, api, downloader, workers=2, dry_run=False, state=repo)
        downloaded = svc.run_sync("cookie", playlist_ids=[10])

        assert len(downloaded) == 1
        downloader.download_track.assert_called_once()
        assert 111 in svc._load_synced_state()
        assert not (cfg.state_dir / "synced_tracks.json").exists()


class TestProcessDryRun:
    def test_pending_files_reported_no_writes(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        cfg.downloads_cache_dir.mkdir(parents=True)
        # 本地 canonical 文件，不在 processed 索引中 → 待处理
        (cfg.downloads_dir / "333.flac").write_text("fake flac")
        save_json(cfg.state_dir / "playlists.json", {"10": {"name": "歌单A", "track_count": 1}})

        api = MagicMock()
        api.get_tracks_detail.return_value = {333: _make_track(333)}
        api.get_playlist_tracks.return_value = []

        decryptor = MagicMock()
        organizer = MagicMock()
        metadata = MagicMock()
        svc = ProcessService(cfg, api, decryptor, organizer, metadata, workers=2, dry_run=True, state=_repository(cfg))
        svc.run_process(downloaded=[], force=False)

        # 不执行任何处理管线、不写索引
        organizer.route_audio.assert_not_called()
        decryptor.decrypt_if_needed.assert_not_called()
        metadata.write.assert_not_called()
        assert not _repository(cfg).is_processed(333, {"FLAC"})

    def test_processed_track_skipped(self, tmp_path: Path) -> None:
        """media_assets 已覆盖 spec 且 processed_tracks 有记录 → dry-run 不列出。"""
        from musicvault.application.source_state import build_audio_asset_from_file
        from musicvault.domain.preset import compute_preset_hash

        cfg = _make_cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        cfg.downloads_cache_dir.mkdir(parents=True)
        canonical = cfg.downloads_dir / "333.flac"
        canonical.write_text("fake flac")

        repo = _repository(cfg)
        repo.upsert_track(_make_track(333))
        repo.upsert_media_asset(build_audio_asset_from_file(333, "FLAC", canonical))
        repo.record_processed(333, compute_preset_hash(cfg.presets), 0.0)

        api = MagicMock()
        api.get_tracks_detail.return_value = {333: _make_track(333)}
        api.get_playlist_tracks.return_value = []

        svc = ProcessService(cfg, api, MagicMock(), MagicMock(), MagicMock(), workers=2, dry_run=True, state=repo)
        svc.run_process(downloaded=[], force=False)

        # spec 已覆盖：dry-run 不应产生任何待处理项
        assert repo.is_processed(333, {"FLAC"})

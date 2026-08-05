from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
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


class TestSyncDryRun:
    def test_new_track_reported_no_writes(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        cfg.downloads_cache_dir.mkdir(parents=True)
        # 已有 1 首已同步，playlists.json 索引已存在
        SyncService._save_synced_state(cfg, {111: [10]})
        save_json(cfg.state_dir / "playlists.json", {"10": {"name": "歌单A", "track_count": 2}})

        # API：歌单信息变化（正式运行会更新索引文件）、曲目新增 222
        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 99}
        api.get_playlist_tracks.return_value = [_make_track(111), _make_track(222)]
        api.get_tracks_download_urls.return_value = {222: "http://example.com/222.mp3"}

        downloader = MagicMock()
        svc = SyncService(cfg, api, downloader, workers=2, dry_run=True)
        downloaded = svc.run_sync("cookie", playlist_ids=[10])

        # 不下载、不写状态
        assert downloaded == []
        downloader.download_track.assert_not_called()
        state_map = SyncService._load_synced_state(cfg)
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
        SyncService._save_synced_state(cfg, {111: [10], 222: [10]})
        canonical = cfg.downloads_dir / "111.flac"
        canonical.write_text("fake flac")

        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
        api.get_playlist_tracks.return_value = [_make_track(222)]

        svc = SyncService(cfg, api, MagicMock(), workers=2, dry_run=True)
        svc.run_sync("cookie", playlist_ids=[10])

        # 111 列入清理计划，但文件与状态均保留
        assert svc.plan["pruned"] == [111]
        assert canonical.exists()
        state_map = SyncService._load_synced_state(cfg)
        assert 111 in state_map

    def test_normal_mode_still_writes(self, tmp_path: Path) -> None:
        """回归：dry_run=False 时行为不变（下载并写入状态）。"""
        cfg = _make_cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        cfg.downloads_cache_dir.mkdir(parents=True)
        SyncService._save_synced_state(cfg, {})

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

        svc = SyncService(cfg, api, downloader, workers=2, dry_run=False)
        downloaded = svc.run_sync("cookie", playlist_ids=[10])

        assert len(downloaded) == 1
        downloader.download_track.assert_called_once()
        assert 111 in SyncService._load_synced_state(cfg)


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
        svc = ProcessService(cfg, api, decryptor, organizer, metadata, workers=2, dry_run=True)
        svc.run_process(downloaded=[], force=False)

        # 不执行任何处理管线、不写索引
        organizer.route_audio.assert_not_called()
        decryptor.decrypt_if_needed.assert_not_called()
        metadata.write.assert_not_called()
        assert not cfg.processed_state_file.exists()

    def test_processed_track_skipped(self, tmp_path: Path) -> None:
        """已在索引中且 preset 未变的曲目，dry-run 也不应列出。"""
        cfg = _make_cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        cfg.downloads_cache_dir.mkdir(parents=True)
        (cfg.downloads_dir / "333.flac").write_text("fake flac")

        from musicvault.core.preset import compute_preset_hash

        save_json(
            cfg.processed_state_file,
            {
                "333": {
                    "audios": {"FLAC": "downloads/333.flac"},
                    "preset_hash": compute_preset_hash(cfg.presets),
                    "updated_at": 0,
                }
            },
        )

        api = MagicMock()
        api.get_tracks_detail.return_value = {333: _make_track(333)}
        api.get_playlist_tracks.return_value = []

        svc = ProcessService(cfg, api, MagicMock(), MagicMock(), MagicMock(), workers=2, dry_run=True)
        svc.run_process(downloaded=[], force=False)

        # 索引未改动（dry-run 不重写），文件未动
        raw = json.loads(cfg.processed_state_file.read_text(encoding="utf-8"))
        assert "333" in raw

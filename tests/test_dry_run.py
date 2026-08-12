from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.source_state import SourceStateRecorder
from musicvault.core.config import Config
from musicvault.domain.models import DownloadedTrack, Track
from musicvault.domain.models import Playlist
from musicvault.application.process_use_case import ProcessUseCase
from musicvault.application.sync_use_case import SyncUseCase


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


def _seed_synced(cfg: Config, state_map: dict[int, list[int]]) -> SQLiteStateRepository:
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
    return repo


class TestSyncDryRun:
    def test_new_track_reported_no_writes(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        # 已有 1 首已下载（状态经 SQLite 预置，pending_files 标记下载产物）
        repo = _seed_synced(cfg, {111: [10]})
        repo.add_pending_file("cache/111.mp3", 111)

        # API：歌单信息变化、曲目新增 222
        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 99}
        api.get_playlist_tracks.return_value = [_make_track(111), _make_track(222)]
        api.get_tracks_download_urls.return_value = {222: "http://example.com/222.mp3"}

        downloader = MagicMock()
        svc = SyncUseCase(cfg, api, downloader, workers=2, dry_run=True, state=_repository(cfg))
        result = svc.run_pull("cookie", playlist_ids=[10])

        # 不下载、不写状态（dry-run 下 fetch 由 Pipeline 层跳过）
        assert result.downloaded == ()
        downloader.download_track.assert_not_called()
        state_map = svc.load_synced_state()
        assert 222 not in state_map
        # 计划包含新曲目与歌单信息变化
        assert [t.id for t in result.dry_run_plan["with_url"]] == [222]
        assert result.dry_run_plan["pruned"] == []

    def test_prune_reported_files_kept(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        # 本地有 111（远端已删除）和 222（远端仍在）
        _seed_synced(cfg, {111: [10], 222: [10]})
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("fake flac")

        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
        api.get_playlist_tracks.return_value = [_make_track(222)]

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=True, state=_repository(cfg))
        result = svc.run_pull("cookie", playlist_ids=[10])

        # 111 列入清理计划，但文件与状态均保留
        assert result.dry_run_plan["pruned"] == [111]
        assert canonical.exists()
        state_map = svc.load_synced_state()
        assert 111 in state_map

    def test_prune_deletes_canonical_from_media_store(self, tmp_path: Path) -> None:
        """非 dry-run：远端已删除曲目的整个 media_store/<tid>/ 目录被删除（扁平布局）。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        _seed_synced(cfg, {111: [10]})
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")

        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
        api.get_playlist_tracks.return_value = []  # 远端已无 111

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        svc.run_fetch("cookie", playlist_ids=[10])
        svc.run_pull("cookie", playlist_ids=[10])

        assert not canonical.exists()
        assert 111 not in svc.load_synced_state()

    def test_normal_mode_writes_to_sqlite(self, tmp_path: Path) -> None:
        """回归：dry_run=False 时下载并把状态写入 SQLite（不再写 JSON）。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)

        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
        api.get_playlist_tracks.return_value = [_make_track(111)]
        api.get_tracks_download_urls.return_value = {111: "http://example.com/111.mp3"}

        downloader = MagicMock()
        downloader.download_track.return_value = DownloadedTrack(
            track=_make_track(111),
            source_file=str(cfg.cache_dir / "111.mp3"),
            is_ncm=False,
            playlist_ids=[10],
        )

        svc = SyncUseCase(cfg, api, downloader, workers=2, dry_run=False, state=repo)
        svc.run_fetch("cookie", playlist_ids=[10])
        result = svc.run_pull("cookie", playlist_ids=[10])

        assert len(result.downloaded) == 1
        downloader.download_track.assert_called_once()
        # 下载缓存落在新 cache/ 目录（不再写 downloads/cache/）
        call_args = downloader.download_track.call_args
        assert call_args.args[2] == cfg.cache_dir
        assert 111 in svc.load_synced_state()
        assert not (cfg.workspace_path / "state").exists()


class TestProcessDryRun:
    def test_pending_reported_no_writes(self, tmp_path: Path) -> None:
        """process dry-run：待处理曲目计入 processed 计划，但不执行解码/转码/写索引。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        raw = cfg.cache_dir / "333.mp3"
        raw.write_bytes(b"fake mp3")

        api = MagicMock()
        item = DownloadedTrack(track=_make_track(333), source_file=str(raw), is_ncm=False, playlist_ids=[])

        decryptor = MagicMock()
        organizer = MagicMock()
        metadata = MagicMock()
        svc = ProcessUseCase(cfg, api, decryptor, organizer, metadata, workers=2, dry_run=True, state=_repository(cfg))
        result = svc.run_process(downloaded=[item], force=False)

        # 只计计划不执行：不写索引、不触任何管线组件
        assert result.processed == 1
        organizer.route_audio.assert_not_called()
        decryptor.decrypt_if_needed.assert_not_called()
        metadata.write.assert_not_called()
        assert not _repository(cfg).is_processed(333, {"FLAC"})

    def test_processed_track_skipped(self, tmp_path: Path) -> None:
        """media_assets 已覆盖 spec 且 processed_tracks 有记录 → 跳过（计入 skipped）。"""
        from musicvault.application.source_state import build_audio_asset_from_file

        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        canonical = cfg.media_store_dir / "333" / "333.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")
        mp3 = cfg.media_store_dir / "333" / "333_192k.mp3"
        mp3.write_bytes(b"fake mp3")

        repo = _repository(cfg)
        repo.upsert_track(_make_track(333))
        repo.upsert_media_asset(build_audio_asset_from_file(333, "FLAC", canonical))
        repo.upsert_media_asset(build_audio_asset_from_file(333, "MP3-192k", mp3))
        repo.record_processed(333, "preset-script", 0.0)

        api = MagicMock()
        item = DownloadedTrack(track=_make_track(333), source_file=str(canonical), is_ncm=False, playlist_ids=[])

        svc = ProcessUseCase(cfg, api, MagicMock(), MagicMock(), MagicMock(), workers=2, dry_run=True, state=repo)
        result = svc.run_process(downloaded=[item], force=False)

        # spec 已覆盖：不产生待处理项，计入 skipped
        assert result.processed == 0
        assert result.skipped == 1
        assert repo.is_processed(333, {"FLAC"})

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.source_state import SourceStateRecorder
from musicvault.application.sync_use_case import SyncUseCase
from musicvault.core.config import Config
from musicvault.domain.lyrics import LyricLine, lyrics_from_json, lyrics_to_json
from musicvault.domain.models import DownloadedTrack, Playlist, Track


def _make_cfg(tmp_path: Path) -> Config:
    """真实 Config，workspace 指向临时目录。"""
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


def _make_api(track_ids: list[int]) -> MagicMock:
    """fake SourceClient：歌单信息/曲目列表/直链均可配置。"""
    api = MagicMock()
    api.get_playlist_info.return_value = {"name": "歌单A", "track_count": len(track_ids)}
    api.get_playlist_tracks.return_value = [_make_track(track_id) for track_id in track_ids]
    api.get_tracks_download_urls.return_value = {
        track_id: f"http://example.com/{track_id}.mp3" for track_id in track_ids
    }
    return api


def _make_downloader(cfg: Config) -> MagicMock:
    """真实落盘 cache 的下载器，便于后续 pending_files 登记。"""
    downloader = MagicMock()

    def _download(track: Track, url: str, dest: Path) -> DownloadedTrack:
        file = dest / f"{track.id}.mp3"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"fake mp3")
        return DownloadedTrack(track=track, source_file=str(file), is_ncm=False)

    downloader.download_track.side_effect = _download
    return downloader


def _seed_synced(cfg: Config, state_map: dict[int, list[int]]) -> SQLiteStateRepository:
    """把 {track_id: [playlist_ids]} 写入 SQLite，模拟此前已同步的状态。"""
    repo = _repository(cfg)
    playlists: dict[int, Playlist] = {}
    tracks = [_make_track(track_id) for track_id in state_map]
    for track_id, playlist_ids in state_map.items():
        for playlist_id in playlist_ids:
            playlists.setdefault(playlist_id, Playlist(playlist_id, f"歌单{playlist_id}", ()))
    for playlist_id, playlist in playlists.items():
        object.__setattr__(
            playlist,
            "track_ids",
            tuple(track_id for track_id, pids in state_map.items() if playlist_id in pids),
        )
    SourceStateRecorder(repo).record_source_state(tracks, playlists.values())
    return repo


class TestFetch:
    def test_fetch_does_not_call_download_urls(self, tmp_path: Path) -> None:
        """fetch 只拉元数据：不查询直链、不下载。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = _make_api([111])

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        svc.run_fetch("cookie", playlist_ids=[10])

        api.get_tracks_download_urls.assert_not_called()

    def test_fetch_records_metadata_to_sqlite(self, tmp_path: Path) -> None:
        """fetch 把歌单与曲目元数据写入 SQLite，供 target-sync 消费。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = _make_api([111])

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        svc.run_fetch("cookie", playlist_ids=[10])

        snapshot = _repository(cfg).create_snapshot()
        assert [track.id for track in snapshot.tracks] == [111]
        assert snapshot.playlists[0].name == "歌单A"
        assert snapshot.playlists[0].track_ids == (111,)

    def test_fetch_renames_playlist_in_sqlite(self, tmp_path: Path) -> None:
        """歌单改名仅登记到 SQLite（upsert_playlist），不碰 library 目录。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        repo.upsert_playlist(Playlist(10, "旧名", ()))
        api = _make_api([111])

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=False, state=repo)
        svc.run_fetch("cookie", playlist_ids=[10])

        playlist = repo.get_playlist(10)
        assert playlist is not None
        assert playlist.name == "歌单A"


class TestPull:
    def test_pull_stores_lyrics(self, tmp_path: Path) -> None:
        """下载成功后把歌词转统一格式写入 SQLite。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = _make_api([111])
        api.get_track_lyrics.return_value = {"lrc": "[00:01.000]hello"}
        repo = _repository(cfg)

        svc = SyncUseCase(cfg, api, _make_downloader(cfg), workers=2, dry_run=False, state=repo)
        result = svc.run_pull("cookie", playlist_ids=[10])

        assert len(result.downloaded) == 1
        payload = repo.get_lyrics(111)
        assert payload is not None
        assert lyrics_from_json(payload) == (LyricLine(1000, 0, "hello"),)

    def test_pull_lyrics_failure_degrades_to_empty(self, tmp_path: Path) -> None:
        """歌词获取失败降级为空行入库，不阻塞下载。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = _make_api([111])
        api.get_track_lyrics.side_effect = RuntimeError("boom")
        repo = _repository(cfg)

        svc = SyncUseCase(cfg, api, _make_downloader(cfg), workers=2, dry_run=False, state=repo)
        result = svc.run_pull("cookie", playlist_ids=[10])

        assert len(result.downloaded) == 1
        assert repo.get_lyrics(111) == lyrics_to_json(())

    def test_pull_second_run_skips_downloaded(self, tmp_path: Path) -> None:
        """已下载（pending_files 有记录）的曲目第二次 pull 不再下载。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)

        svc = SyncUseCase(cfg, _make_api([111]), _make_downloader(cfg), workers=2, dry_run=False, state=repo)
        svc.run_fetch("cookie", playlist_ids=[10])
        svc.run_pull("cookie", playlist_ids=[10])

        second_downloader = _make_downloader(cfg)
        svc = SyncUseCase(cfg, _make_api([111]), second_downloader, workers=2, dry_run=False, state=repo)
        svc.run_fetch("cookie", playlist_ids=[10])
        svc.run_pull("cookie", playlist_ids=[10])

        second_downloader.download_track.assert_not_called()

    def test_pull_prunes_flat_track_dir_and_state(self, tmp_path: Path) -> None:
        """远端已删除曲目：rmtree 整个 media_store/<tid>/（扁平布局）并移除 SQLite 状态。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        _seed_synced(cfg, {111: [10]})
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")

        svc = SyncUseCase(cfg, _make_api([]), MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        svc.run_fetch("cookie", playlist_ids=[10])
        svc.run_pull("cookie", playlist_ids=[10])

        assert not canonical.parent.exists()
        assert 111 not in svc.load_synced_state()

    def test_pull_prune_removes_library_hardlinks(self, tmp_path: Path) -> None:
        """远端已删除曲目：library 中指向 canonical inode 的硬链接一并删除（rmtree 不释放链接磁盘）。"""
        import os

        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        _seed_synced(cfg, {111: [10]})
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")

        # 模拟 distribute 后 library 下的硬链接：歌单目录与未分类 各一个
        playlist_dir = cfg.library_dir / "歌单10"
        playlist_dir.mkdir(parents=True)
        os.link(canonical, playlist_dir / "111.flac")
        uncat_dir = cfg.library_dir / cfg.default_playlist_name
        uncat_dir.mkdir(parents=True)
        os.link(canonical, uncat_dir / "111.flac")

        svc = SyncUseCase(cfg, _make_api([]), MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        svc.run_fetch("cookie", playlist_ids=[10])
        svc.run_pull("cookie", playlist_ids=[10])

        assert not (playlist_dir / "111.flac").exists()
        assert not (uncat_dir / "111.flac").exists()

    def test_cleanup_stale_state_removes_library_hardlinks(self, tmp_path: Path) -> None:
        """canonical 手动缺失：remove_track 前收集其余文件 inode，删除 library 中对应硬链接。"""
        import os

        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        _seed_synced(cfg, {111: [10]})
        from musicvault.application.source_state import build_audio_asset_from_file

        repo = _repository(cfg)
        canonical = cfg.media_store_dir / "111" / "111.flac"
        variant = cfg.media_store_dir / "111" / "111_192k.mp3"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")
        variant.write_bytes(b"fake mp3")
        repo.upsert_media_asset(build_audio_asset_from_file(111, "FLAC", canonical))

        playlist_dir = cfg.library_dir / "歌单10"
        playlist_dir.mkdir(parents=True)
        os.link(variant, playlist_dir / "111_192k.mp3")

        # 模拟 canonical 手动缺失（仅 192k 变体在库，asset.path 指向的 flac 已消失）
        canonical.unlink()

        svc = SyncUseCase(cfg, MagicMock(), MagicMock(), workers=2, dry_run=False, state=repo)
        assert svc._cleanup_stale_state() == 1

        assert not (playlist_dir / "111_192k.mp3").exists()
        assert 111 not in svc.load_synced_state()


class TestFetchPullFlow:
    def test_fetch_then_pull_downloads_new_track(self, tmp_path: Path) -> None:
        """fetch 登记元数据后 pull 仍能识别新曲目并下载。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)

        svc = SyncUseCase(cfg, _make_api([111]), _make_downloader(cfg), workers=2, dry_run=False, state=repo)
        svc.run_fetch("cookie", playlist_ids=[10])
        result = svc.run_pull("cookie", playlist_ids=[10])

        assert len(result.downloaded) == 1
        assert [track.id for track in repo.create_snapshot().tracks] == [111]

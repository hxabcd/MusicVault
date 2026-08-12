"""SyncUseCase 边界与异常路径补充测试。

覆盖 fetch/pull 的空配置警告、stale 状态清理的各类分支、
find_canonical_for_spec 的规格匹配、inode 收集与 library 硬链接清理、
下载失败单曲降级、进度事件上报与中断时的部分保存。
"""

from __future__ import annotations

import logging
import os
import pathlib
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.source_state import build_audio_asset_from_file
from musicvault.application.sync_use_case import SyncResult, SyncUseCase
from musicvault.core.config import Config
from musicvault.domain.models import DownloadedTrack, MediaAsset, Track


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


def _svc(cfg: Config, api: MagicMock | None = None, *, dry_run: bool = False) -> SyncUseCase:
    return SyncUseCase(cfg, api or MagicMock(), MagicMock(), workers=2, dry_run=dry_run, state=_repository(cfg))


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

    def _download(track: Track, _: str, dest: Path) -> DownloadedTrack:
        file = dest / f"{track.id}.mp3"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"fake mp3")
        return DownloadedTrack(track=track, source_file=str(file), is_ncm=False)

    downloader.download_track.side_effect = _download
    return downloader


class _FakeProgress:
    """记录 begin/advance/end 事件的进度 fake。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def begin(self, total: int, phase: str) -> None:
        self.events.append(("begin", total, phase))

    def advance(self, *, success: bool, idx: int, item_name: str) -> None:
        self.events.append(("advance", success, idx, item_name))

    def end(self) -> None:
        self.events.append(("end",))


class TestEmptyConfiguration:
    def test_run_fetch_warns_when_nothing_configured(self, tmp_path: Path, monkeypatch) -> None:
        """未配置任何歌单或单曲：fetch 输出警告并直接返回，不登录源端。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        captured: list[str] = []
        monkeypatch.setattr("musicvault.application.sync_use_case.output_warn", lambda msg: captured.append(msg))
        api = MagicMock()

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        svc.run_fetch("cookie", playlist_ids=[])

        assert captured and "未配置" in captured[0]
        api.login_with_cookie.assert_not_called()

    def test_run_pull_returns_empty_result_when_nothing_configured(self, tmp_path: Path) -> None:
        """未配置任何歌单或单曲：pull 返回空结果，不登录源端。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = MagicMock()

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        result = svc.run_pull("cookie", playlist_ids=[])

        assert result == SyncResult()
        api.login_with_cookie.assert_not_called()


class TestRecordSourceState:
    def test_skips_non_numeric_and_non_dict_entries(self, tmp_path: Path) -> None:
        """playlist_index 中非数字键/非 dict 值被跳过，不参与 SQLite 登记。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)

        svc = SyncUseCase(cfg, MagicMock(), MagicMock(), workers=2, dry_run=False, state=repo)
        svc._record_source_state({}, {}, {"abc": {"name": "x", "track_count": 1}, "10": "not-dict"}, [])

        assert repo.list_playlists() == []

    def test_records_numeric_entries(self, tmp_path: Path) -> None:
        """数字键正常登记为歌单（-1 等去掉负号后为数字的键也被接受）。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)

        svc = SyncUseCase(cfg, MagicMock(), MagicMock(), workers=2, dry_run=False, state=repo)
        svc._record_source_state({}, {}, {"10": {"name": "歌单A", "track_count": 1}}, [])

        playlists = repo.list_playlists()
        assert [pl.id for pl in playlists] == [10]
        assert playlists[0].name == "歌单A"


class TestCleanupStaleState:
    def test_skips_non_audio_assets_and_counts_stale(self, tmp_path: Path) -> None:
        """非 audio 资产（cover 等）不参与 stale 判断；audio 路径缺失计入 stale。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        repo.upsert_track(_make_track(111))
        repo.upsert_track(_make_track(222))
        repo.upsert_media_asset(
            MediaAsset(track_id=111, asset_type="audio", spec="FLAC", path=cfg.media_store_dir / "111" / "111.flac")
        )
        repo.upsert_media_asset(
            MediaAsset(track_id=222, asset_type="cover", spec="cover", path=cfg.media_store_dir / "222" / "cover.jpg")
        )

        svc = SyncUseCase(cfg, MagicMock(), MagicMock(), workers=2, dry_run=False, state=repo)
        assert svc._cleanup_stale_state() == 1
        # 非 audio 资产及其曲目不受影响
        assert repo.list_media_assets(222)

    def test_no_stale_when_all_files_exist(self, tmp_path: Path) -> None:
        """所有 audio 资产路径都存在：无 stale，返回 0。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        repo.upsert_track(_make_track(111))
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")
        repo.upsert_media_asset(build_audio_asset_from_file(111, "FLAC", canonical))

        svc = SyncUseCase(cfg, MagicMock(), MagicMock(), workers=2, dry_run=False, state=repo)
        assert svc._cleanup_stale_state() == 0

    def test_dry_run_reports_without_removing(self, tmp_path: Path) -> None:
        """dry-run：只报告 stale 数量，不删除曲目状态。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        repo.upsert_track(_make_track(111))
        repo.upsert_media_asset(
            MediaAsset(track_id=111, asset_type="audio", spec="FLAC", path=cfg.media_store_dir / "111" / "111.flac")
        )

        svc = SyncUseCase(cfg, MagicMock(), MagicMock(), workers=2, dry_run=True, state=repo)
        assert svc._cleanup_stale_state() == 1
        assert repo.get_track(111) is not None


class TestFindCanonicalForSpec:
    def test_original_missing_dir_returns_none(self, tmp_path: Path) -> None:
        """media_store/<tid>/ 不存在：ORIGINAL 返回 None。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)

        assert _svc(cfg).find_canonical_for_spec(111, "ORIGINAL") is None

    def test_original_finds_flac(self, tmp_path: Path) -> None:
        """ORIGINAL 按扩展名优先级命中 flac。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")

        assert _svc(cfg).find_canonical_for_spec(111, "ORIGINAL") == canonical

    def test_original_falls_back_to_wav(self, tmp_path: Path) -> None:
        """ORIGINAL 按扩展名顺序兜底：仅存 wav 时返回 wav。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        wav = cfg.media_store_dir / "111" / "111.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"fake wav")

        assert _svc(cfg).find_canonical_for_spec(111, "ORIGINAL") == wav

    def test_original_none_when_no_audio(self, tmp_path: Path) -> None:
        """目录仅含 .lrc 等非音频文件：ORIGINAL 返回 None。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        lrc = cfg.media_store_dir / "111" / "111.flac.lrc"
        lrc.parent.mkdir(parents=True, exist_ok=True)
        lrc.write_text("x")

        assert _svc(cfg).find_canonical_for_spec(111, "ORIGINAL") is None

    def test_spec_with_bitrate_preferred(self, tmp_path: Path) -> None:
        """带 bitrate 的 spec：优先匹配 <tid>_<bitrate><ext>。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        mp3 = cfg.media_store_dir / "111" / "111_192k.mp3"
        mp3.parent.mkdir(parents=True, exist_ok=True)
        mp3.write_bytes(b"fake mp3")

        assert _svc(cfg).find_canonical_for_spec(111, "MP3-192k") == mp3

    def test_spec_falls_back_to_plain(self, tmp_path: Path) -> None:
        """无带 bitrate 变体时回退到无后缀同名文件。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        mp3 = cfg.media_store_dir / "111" / "111.mp3"
        mp3.parent.mkdir(parents=True, exist_ok=True)
        mp3.write_bytes(b"fake mp3")

        assert _svc(cfg).find_canonical_for_spec(111, "MP3-192k") == mp3

    def test_spec_unknown_format_uses_default_ext(self, tmp_path: Path) -> None:
        """未知格式名（WAV 等）：扩展名按默认规则拼 .wav 查找。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        wav = cfg.media_store_dir / "111" / "111.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"fake wav")

        assert _svc(cfg).find_canonical_for_spec(111, "WAV") == wav

    def test_spec_not_found_returns_none(self, tmp_path: Path) -> None:
        """spec 无对应文件：返回 None。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        (cfg.media_store_dir / "111").mkdir()

        assert _svc(cfg).find_canonical_for_spec(111, "MP3-192k") is None


class TestResolveDryUrls:
    def test_empty_tracks_returns_empty(self, tmp_path: Path) -> None:
        """空曲目列表：dry-run 直链解析直接返回两组空。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)

        assert _svc(cfg, dry_run=True)._resolve_dry_urls([]) == ([], [])


class TestCollectInodes:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        """目录不存在：inode 收集返回空集。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)

        assert _svc(cfg)._collect_inodes(cfg.media_store_dir / "nope") == set()

    def test_skips_subdirectories(self, tmp_path: Path) -> None:
        """目录内的子目录不收集（只收集普通文件）。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        track_dir = cfg.media_store_dir / "111"
        track_dir.mkdir()
        (track_dir / "111.flac").write_bytes(b"fake flac")
        sub = track_dir / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_bytes(b"x")

        assert len(_svc(cfg)._collect_inodes(track_dir)) == 1

    def test_ignores_stat_failure(self, tmp_path: Path, monkeypatch) -> None:
        """个别文件 stat 失败（权限等）：跳过该文件，不中断收集。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        track_dir = cfg.media_store_dir / "111"
        track_dir.mkdir()
        (track_dir / "111.flac").write_bytes(b"fake flac")
        (track_dir / "blocked.flac").write_bytes(b"fake blocked")

        def _flaky_stat(self, **kwargs):
            del kwargs
            if self.name == "blocked.flac":
                raise OSError("拒绝访问")
            return os.stat(self)

        monkeypatch.setattr(pathlib.Path, "stat", _flaky_stat)
        monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)

        assert len(_svc(cfg)._collect_inodes(track_dir)) == 1


class TestRemoveLibraryLinksByInode:
    def test_skips_non_dirs_and_non_files(self, tmp_path: Path) -> None:
        """library 顶层有文件、歌单目录内有子目录：一律跳过不中断。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.library_dir.mkdir(parents=True)
        (cfg.library_dir / "note.txt").write_bytes(b"x")  # 顶层非目录
        playlist_dir = cfg.library_dir / "歌单A"
        playlist_dir.mkdir()
        (playlist_dir / "sub").mkdir()  # 歌单目录内非文件

        assert _svc(cfg)._remove_library_links_by_inode({(1, 2)}) == 0

    def test_ignores_stat_failure(self, tmp_path: Path, monkeypatch) -> None:
        """library 文件 stat 失败：跳过该文件，不中断清理。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.library_dir.mkdir(parents=True)
        playlist_dir = cfg.library_dir / "歌单A"
        playlist_dir.mkdir()
        (playlist_dir / "111.flac").write_bytes(b"fake flac")
        (playlist_dir / "blocked.flac").write_bytes(b"fake blocked")

        def _flaky_stat(self, **kwargs):
            del kwargs
            if self.name == "blocked.flac":
                raise OSError("拒绝访问")
            return os.stat(self)

        monkeypatch.setattr(pathlib.Path, "stat", _flaky_stat)
        monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)

        assert _svc(cfg)._remove_library_links_by_inode({(1, 2)}) == 0


class TestPullWithProgress:
    def test_reports_begin_advance_end(self, tmp_path: Path) -> None:
        """带进度汇报的 pull：成功下载上报 begin/advance/end 事件。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = _make_api([111])
        progress = _FakeProgress()

        svc = SyncUseCase(cfg, api, _make_downloader(cfg), workers=2, dry_run=False, state=_repository(cfg))
        result = svc.run_pull("cookie", playlist_ids=[10], progress=progress)

        assert len(result.downloaded) == 1
        assert ("begin", 1, "下载中") in progress.events
        assert ("advance", True, 1, "Song 111") in progress.events
        assert ("end",) in progress.events


class TestDownloadFailureDegradation:
    def test_single_failure_does_not_block_other_tracks(self, tmp_path: Path, caplog) -> None:
        """单曲下载失败：上报失败进度并记录日志，其余曲目正常下载，结果聚合成功部分。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = _make_api([111, 222])
        downloader = MagicMock()

        def _download(track: Track, _: str, dest: Path) -> DownloadedTrack:
            if track.id == 222:
                raise RuntimeError("网络中断")
            file = dest / f"{track.id}.mp3"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"fake mp3")
            return DownloadedTrack(track=track, source_file=str(file), is_ncm=False)

        downloader.download_track.side_effect = _download
        repo = _repository(cfg)
        progress = _FakeProgress()

        svc = SyncUseCase(cfg, api, downloader, workers=2, dry_run=False, state=repo)
        with caplog.at_level(logging.ERROR, logger="musicvault.application.sync_use_case"):
            result = svc.run_pull("cookie", playlist_ids=[10], progress=progress)

        # 部分成功聚合：111 下载成功登记，222 失败不登记
        assert len(result.downloaded) == 1
        assert result.downloaded[0].track.id == 111
        assert result.added == 1
        assert repo.list_pending_track_ids() == [111]
        # 进度与日志：失败曲目上报 advance(False) 与错误日志
        assert ("advance", False, 2, "Song 222") in progress.events
        assert any("下载失败" in record.message and "222" in record.message for record in caplog.records)


class TestDownloadInterrupt:
    def test_keyboard_interrupt_saves_partial_downloads(self, tmp_path: Path) -> None:
        """下载中被 Ctrl+C：已成功下载的曲目登记到 SQLite（部分保存），异常继续上抛。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        api = _make_api([111, 222])
        downloader = MagicMock()

        def _download(track: Track, _: str, dest: Path) -> DownloadedTrack:
            if track.id == 222:
                time.sleep(0.3)
                raise KeyboardInterrupt
            file = dest / f"{track.id}.mp3"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"fake mp3")
            return DownloadedTrack(track=track, source_file=str(file), is_ncm=False)

        downloader.download_track.side_effect = _download
        repo = _repository(cfg)

        svc = SyncUseCase(cfg, api, downloader, workers=2, dry_run=False, state=repo)
        with pytest.raises(KeyboardInterrupt):
            svc.run_pull("cookie", playlist_ids=[10], progress=_FakeProgress())

        # 部分保存：已成功的 111 登记 pending 与 track，失败的 222 不登记
        assert repo.list_pending_track_ids() == [111]
        assert repo.get_track(111) is not None
        assert repo.get_track(222) is None

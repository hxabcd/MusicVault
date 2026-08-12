"""ProcessUseCase 边界路径补充测试。

覆盖 preset 注入缺失、scan 形态过滤、单曲处理失败隔离、中断冒泡、
track_id 推断失败、年份回退、spec 推断、已处理空标记、详情缓存
与 LRC 编码回退。
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from musicvault.adapters.state.sqlite import SQLiteProcessStateRepository, SQLiteSourceStateRepository, SQLiteState
from musicvault.application.process_use_case import ProcessUseCase, _write_lrc
from musicvault.core.config import Config
from musicvault.domain.models import DownloadedTrack, Track
from musicvault.preset_api.v1 import AudioFormat, BasePreset, LyricEncoding, PresetLoadError


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def _make_track(track_id: int) -> Track:
    return Track(id=track_id, name=f"Song {track_id}", artists=["Artist"], album="Album", raw={})


def _repository(cfg: Config) -> SQLiteSourceStateRepository:
    return SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file))


def _process_repository(cfg: Config) -> SQLiteProcessStateRepository:
    return SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file))


def _downloaded(track_id: int, source_file: Path) -> DownloadedTrack:
    return DownloadedTrack(track=_make_track(track_id), source_file=str(source_file), is_ncm=False, playlist_ids=[])


def _process_svc(cfg: Config, repo: SQLiteSourceStateRepository, *, organizer=None, metadata=None, api=None, presets=None):
    return ProcessUseCase(
        cfg=cfg,
        api=api or MagicMock(),
        decryptor=MagicMock(),
        organizer=organizer or MagicMock(),
        metadata=metadata if metadata is not None else MagicMock(),
        workers=1,
        state=repo,
        process_state=_process_repository(cfg),
        presets=presets if presets is not None else {},
    )


class _DefaultLyricsPreset(BasePreset):
    """默认 build_lyrics（standard_lrc）：空行列表返回空文本。"""

    format = AudioFormat.FLAC


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


class TestPresetInjection:
    def test_missing_presets_raises_load_error(self, tmp_path: Path) -> None:
        """未注入 preset 实例索引：构造即抛 PresetLoadError（只能经 build_pipeline 组装）。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        repo = _repository(cfg)

        with pytest.raises(PresetLoadError, match="presets"):
            ProcessUseCase(
                cfg, MagicMock(), MagicMock(), MagicMock(), MagicMock(), workers=1, state=repo,
                process_state=_process_repository(cfg),
            )


class TestScanCanonicalFiles:
    def test_scan_missing_media_store_returns_empty(self, tmp_path: Path) -> None:
        """media_store 不存在：scan 返回空列表。"""
        cfg = _make_cfg(tmp_path)
        repo = _repository(cfg)

        assert _process_svc(cfg, repo)._scan_canonical_files() == []

    def test_scan_filters_non_dir_non_audio_mismatched(self, tmp_path: Path) -> None:
        """scan 只收集 <tid>/<tid>.ext 形态的 canonical 文件：过滤文件/非数字目录/非音频/前缀不匹配。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        repo = _repository(cfg)
        media_store = cfg.media_store_dir
        (media_store / "readme.txt").write_bytes(b"x")  # 顶层文件：非目录
        (media_store / "abc").mkdir()  # 目录名非数字
        d333 = media_store / "333"
        d333.mkdir()
        (d333 / "332.lrc").write_text("x")  # 非音频扩展（排序在 canonical 前，先被遍历）
        (d333 / "332.txt").write_text("x")  # 非音频扩展
        (d333 / "330.mp3").write_bytes(b"x")  # 音频扩展但文件名前缀与目录名不一致
        canonical = d333 / "333.flac"
        canonical.write_bytes(b"fake flac")

        svc = _process_svc(cfg, repo)
        assert svc._scan_canonical_files() == [(canonical, 333)]


class TestProcessFailureIsolation:
    def test_single_failure_does_not_block_other_tracks(self, tmp_path: Path, caplog) -> None:
        """单曲处理失败（转码异常）：计入 failed 并上报失败进度，其余曲目正常处理。"""
        cfg = _make_cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        raw333 = cfg.cache_dir / "333.mp3"
        raw333.write_bytes(b"fake mp3")
        raw444 = cfg.cache_dir / "444.mp3"
        raw444.write_bytes(b"fake mp3")

        def _route(src, track, output_dir, audio_specs, force=False):
            del src, audio_specs, force
            if track.id == 444:
                raise RuntimeError("ffmpeg 崩溃")
            out = output_dir / f"{track.id}.flac"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake flac")
            return {(AudioFormat.FLAC, None): out}

        organizer = MagicMock()
        organizer.route_audio.side_effect = _route
        progress = _FakeProgress()
        svc = _process_svc(cfg, repo, organizer=organizer, presets={"flac": _DefaultLyricsPreset()})

        with caplog.at_level(logging.ERROR, logger="musicvault.application.process_use_case"):
            result = svc.run_process(
                downloaded=[_downloaded(333, raw333), _downloaded(444, raw444)],
                force=False,
                progress=progress,
            )

        assert result.processed == 1
        assert result.failed == 1
        assert result.skipped == 0
        assert _process_repository(cfg).is_processed(333, {"FLAC"})
        assert not _process_repository(cfg).is_processed(444, {"FLAC"})
        assert ("begin", 2, "处理中") in progress.events
        assert sum(1 for e in progress.events if e[0] == "advance" and e[1] is True) == 1
        assert sum(1 for e in progress.events if e[0] == "advance" and e[1] is False) == 1
        assert ("end",) in progress.events
        assert any("处理失败" in record.message and "444" in record.message for record in caplog.records)

    def test_keyboard_interrupt_aborts_batch(self, tmp_path: Path) -> None:
        """处理中 Ctrl+C：中断冒泡，不写任何处理状态。"""
        cfg = _make_cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        raw = cfg.cache_dir / "333.mp3"
        raw.write_bytes(b"fake mp3")
        organizer = MagicMock()
        organizer.route_audio.side_effect = KeyboardInterrupt
        svc = _process_svc(cfg, repo, organizer=organizer, presets={"flac": _DefaultLyricsPreset()})

        with pytest.raises(KeyboardInterrupt):
            svc.run_process(downloaded=[_downloaded(333, raw)], force=False)

        assert not _process_repository(cfg).is_processed(333, {"FLAC"})


class TestProcessFileTrackIdInference:
    def test_unresolvable_track_id_raises(self, tmp_path: Path) -> None:
        """raw 文件无法从 pending_files 反查 track_id：抛 RuntimeError。"""
        cfg = _make_cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        raw = cfg.cache_dir / "unknown.mp3"
        raw.write_bytes(b"fake mp3")

        svc = _process_svc(cfg, repo)
        with pytest.raises(RuntimeError, match="无法推断 track_id"):
            svc._process_file(raw)

    def test_prefetched_track_without_id_raises(self, tmp_path: Path) -> None:
        """prefetched_track 存在但 id 为空：抛 RuntimeError（防御性检查）。"""
        cfg = _make_cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        raw = cfg.cache_dir / "333.mp3"
        raw.write_bytes(b"fake mp3")

        svc = _process_svc(cfg, repo)
        with pytest.raises(RuntimeError, match="无法推断 track_id"):
            svc._process_file(raw, prefetched_track=Track(id=None, name="x", artists=[], album=""))


class TestYearFallback:
    def test_year_fallback_populates_publish_time(self, tmp_path: Path) -> None:
        """raw 缺 publishTime 且带专辑 ID：从专辑 API 回填发布年份时间戳。"""
        cfg = _make_cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        raw = cfg.cache_dir / "333.mp3"
        raw.write_bytes(b"fake mp3")
        track = _make_track(333)
        track.raw = {"al": {"id": 123}}
        api = MagicMock()
        api.get_album_info.return_value = {"album": {"publishTime": 1700000000000}}
        organizer = MagicMock()
        canonical = cfg.media_store_dir / "333" / "333.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")
        organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
        svc = _process_svc(cfg, repo, organizer=organizer, api=api, presets={"flac": _DefaultLyricsPreset()})

        svc._process_file(raw, prefetched_track=track)

        assert track.raw["publishTime"] == 1700000000000
        api.get_album_info.assert_called_once_with(123)

    def test_year_fallback_album_api_failure_ignored(self, tmp_path: Path) -> None:
        """专辑 API 失败：年份回退静默忽略，不中断处理。"""
        cfg = _make_cfg(tmp_path)
        cfg.cache_dir.mkdir(parents=True)
        repo = _repository(cfg)
        raw = cfg.cache_dir / "333.mp3"
        raw.write_bytes(b"fake mp3")
        track = _make_track(333)
        track.raw = {"al": {"id": 123}}
        api = MagicMock()
        api.get_album_info.side_effect = OSError("网络故障")
        organizer = MagicMock()
        canonical = cfg.media_store_dir / "333" / "333.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")
        organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
        svc = _process_svc(cfg, repo, organizer=organizer, api=api, presets={"flac": _DefaultLyricsPreset()})

        svc._process_file(raw, prefetched_track=track)

        assert "publishTime" not in track.raw


class TestSpecFromCanonical:
    def test_unknown_extension_returns_none(self, tmp_path: Path) -> None:
        """未知扩展名（wav 等）：无法推断规格，返回 None。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        svc = _process_svc(cfg, _repository(cfg))

        assert svc._spec_from_canonical(Path("111.wav")) is None

    def test_bitrate_suffix_parsed(self, tmp_path: Path) -> None:
        """带 bitrate 后缀的 canonical 名（<tid>_<bitrate>.ext）：解析出 (格式, bitrate)。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        svc = _process_svc(cfg, _repository(cfg))

        assert svc._spec_from_canonical(Path("111_192k.mp3")) == (AudioFormat.MP3, "192k")


class TestMarkProcessed:
    def test_empty_audio_map_is_noop(self, tmp_path: Path) -> None:
        """空 audio_map：标记为空操作，不写处理记录。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        repo = _repository(cfg)
        svc = _process_svc(cfg, repo)

        svc._mark_processed({})

        assert not _process_repository(cfg).is_processed(333, {"FLAC"})


class TestSafeTrack:
    def test_returns_api_detail_and_caches(self, tmp_path: Path) -> None:
        """详情 API 返回曲目：首次获取并实例内缓存，同实例再次获取不再打 API。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        repo = _repository(cfg)
        detail = _make_track(333)
        api = MagicMock()
        api.get_track_detail.return_value = detail
        svc = _process_svc(cfg, repo, api=api)

        assert svc._safe_track(333, "fallback") is detail
        assert svc._safe_track(333, "fallback") is detail
        api.get_track_detail.assert_called_once_with(333)


class TestWriteLrc:
    def test_defaults_to_utf8_without_encodings(self, tmp_path: Path) -> None:
        """encodings 为空：回退 utf-8 写出。"""
        target = tmp_path / "x.lrc"

        assert _write_lrc(target, "hello", encodings=()) == target
        assert target.read_bytes() == b"hello"

    def test_falls_back_when_encoding_fails(self, tmp_path: Path) -> None:
        """首选编码失败：跳过该编码并用 utf-8（replace）兜底写出。"""

        class _FlakyStr(str):
            def encode(self, encoding="utf-8", errors="strict"):
                if errors == "strict":
                    raise UnicodeEncodeError(encoding, self, 0, 1, "模拟编码失败")
                return super().encode(encoding, errors=errors)

        target = tmp_path / "x.lrc"
        _write_lrc(target, _FlakyStr("中文"), encodings=(LyricEncoding.UTF_8,))

        assert target.read_bytes() == "中文".encode("utf-8")

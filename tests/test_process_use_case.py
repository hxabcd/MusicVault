"""ProcessUseCase 重构测试：离线歌词消费、preset 歌词函数、歌词文件写出。"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from musicvault.adapters.state.sqlite import SQLiteProcessStateRepository, SQLiteSourceStateRepository, SQLiteState
from musicvault.application.process_use_case import ProcessUseCase
from musicvault.core.config import Config
from musicvault.domain.lyrics import LyricLine, lyrics_to_json
from musicvault.domain.models import DownloadedTrack, Track
from musicvault.preset_api.v1 import AudioFormat, BasePreset, LyricEncoding, MetadataSpec


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


class _CustomLyricsPreset(BasePreset):
    """自定义 build_lyrics 的 preset：记录收到的行并返回固定文本。"""

    format = AudioFormat.FLAC

    def __init__(self, text: str = "custom lrc") -> None:
        self.text = text
        self.received: list[LyricLine] = []

    def build_lyrics(self, line: LyricLine) -> str:
        self.received.append(line)
        return self.text


class _DefaultLyricsPreset(BasePreset):
    """默认 build_lyrics（standard_lrc_line）：无歌词行时框架产出空文本。"""

    format = AudioFormat.FLAC


class _RecordingMetadata:
    """记录 metadata.write 参数的 fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, MetadataSpec]] = []

    def write(self, audio_file: Path, _: Track, *, metadata: MetadataSpec, cover_timeout: int = 15) -> None:
        del cover_timeout
        self.calls.append((audio_file, metadata))


def _process_svc(
    cfg: Config,
    repo: SQLiteSourceStateRepository,
    *,
    organizer: MagicMock,
    metadata=None,
    api=None,
    presets: dict[str, BasePreset],
) -> ProcessUseCase:
    return ProcessUseCase(
        cfg=cfg,
        api=api or MagicMock(),
        decryptor=MagicMock(),
        organizer=organizer,
        metadata=metadata if metadata is not None else MagicMock(),
        workers=1,
        state=repo,
        process_state=_process_repository(cfg),
        presets=presets,
    )


def test_process_writes_lyrics_file_from_preset(tmp_path: Path) -> None:
    """离线歌词消费：state.get_lyrics → lines → preset.build_lyrics → 写 {tid}.{preset}.lrc。"""
    cfg = _make_cfg(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json((LyricLine(1000, 0, "hello"),)), 0.0)

    raw = cfg.cache_dir / "333.mp3"
    raw.write_bytes(b"fake mp3")
    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")

    preset = _CustomLyricsPreset("custom lrc")
    organizer = MagicMock()
    organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
    api = MagicMock()
    svc = _process_svc(cfg, repo, organizer=organizer, api=api, presets={"custom": preset})
    svc.run_process(downloaded=[_downloaded(333, raw)], force=False)

    lrc = cfg.media_store_dir / "333" / "333.custom.lrc"
    assert lrc.read_text(encoding="utf-8") == "custom lrc"
    assert preset.received == [LyricLine(1000, 0, "hello")]
    # 完全离线：不再调用歌词 API
    api.get_track_lyrics.assert_not_called()


def test_process_empty_lyrics_skips_file(tmp_path: Path) -> None:
    """get_lyrics 返回 None → build_lyrics 得空文本 → 不写 .lrc 文件。"""
    cfg = _make_cfg(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    # 未保存歌词：get_lyrics 返回 None

    raw = cfg.cache_dir / "333.mp3"
    raw.write_bytes(b"fake mp3")
    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")

    organizer = MagicMock()
    organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
    svc = _process_svc(cfg, repo, organizer=organizer, presets={"custom": _DefaultLyricsPreset()})
    svc.run_process(downloaded=[_downloaded(333, raw)], force=False)

    assert list((cfg.media_store_dir / "333").glob("*.lrc")) == []


def test_process_metadata_spec_union(tmp_path: Path) -> None:
    """两个 preset 共享 spec：embed_cover 与 fields 按并集传给 metadata.write。"""
    cfg = _make_cfg(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json(()), 0.0)

    raw = cfg.cache_dir / "333.mp3"
    raw.write_bytes(b"fake mp3")
    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")

    p1 = _DefaultLyricsPreset()
    p1.metadata = MetadataSpec(embed_cover=True, cover_max_size=800, fields=("year",))
    p2 = _DefaultLyricsPreset()
    p2.metadata = MetadataSpec(embed_cover=False, cover_max_size=0, fields=("genre", "year"))

    recorder = _RecordingMetadata()
    organizer = MagicMock()
    organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
    svc = _process_svc(cfg, repo, organizer=organizer, metadata=recorder, presets={"p1": p1, "p2": p2})
    svc.run_process(downloaded=[_downloaded(333, raw)], force=False)

    assert len(recorder.calls) == 1
    _, merged = recorder.calls[0]
    assert merged.embed_cover is True
    assert merged.cover_max_size == 800
    assert merged.fields == ("genre", "year")


def test_process_writes_lrc_with_preset_encodings(tmp_path: Path) -> None:
    """按 preset.lyrics_encodings 的 .value 序列尝试编码（GB18030 可回退）。"""
    cfg = _make_cfg(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json((LyricLine(1000, 0, "中文歌词"),)), 0.0)

    raw = cfg.cache_dir / "333.mp3"
    raw.write_bytes(b"fake mp3")
    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")

    preset = _DefaultLyricsPreset()
    preset.lyrics_encodings = (LyricEncoding.GB18030,)
    organizer = MagicMock()
    organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
    svc = _process_svc(cfg, repo, organizer=organizer, presets={"custom": preset})
    svc.run_process(downloaded=[_downloaded(333, raw)], force=False)

    data = (cfg.media_store_dir / "333" / "333.custom.lrc").read_bytes()
    assert data.decode("gb18030") == "[00:01.000]中文歌词"
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")


def test_filter_pending_uses_preset_param_specs(tmp_path: Path) -> None:
    """_filter_pending 的必需 spec 只来自 presets 参数（v1 枚举），无任何领域 Preset 回退。

    presets 只声明 MP3-192k：media_assets 覆盖该 spec 且有处理记录 → 第二次跳过；
    若再叠加默认 FLAC 计算，FLAC 未覆盖会误判为待处理。
    """
    from musicvault.application.source_state import build_audio_asset_from_file

    cfg = _make_cfg(tmp_path)
    cfg.media_store_dir.mkdir(parents=True)
    canonical = cfg.media_store_dir / "333" / "333_192k.mp3"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake mp3")

    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.upsert_media_asset(build_audio_asset_from_file(333, "MP3-192k", canonical))
    _process_repository(cfg).mark_processed(333, 0.0)

    class _Mp3Preset(BasePreset):
        format = AudioFormat.MP3
        bitrate = "192k"

    organizer = MagicMock()
    svc = _process_svc(cfg, repo, organizer=organizer, presets={"mp3": _Mp3Preset()})
    result = svc.run_process(downloaded=[_downloaded(333, canonical)], force=False)

    assert result.processed == 0
    assert result.skipped == 1
    organizer.route_audio.assert_not_called()


def test_run_process_without_downloads_returns_empty(tmp_path: Path) -> None:
    """run_process 无下载输入 → 直接返回空结果（本地独立模式已移除）。"""
    cfg = _make_cfg(tmp_path)
    cfg.media_store_dir.mkdir(parents=True)
    repo = _repository(cfg)
    organizer = MagicMock()
    svc = _process_svc(cfg, repo, organizer=organizer, presets={})
    result = svc.run_process(downloaded=[], force=False)

    assert result.processed == 0
    assert result.skipped == 0
    assert result.failed == 0
    organizer.route_audio.assert_not_called()


def test_run_process_reprocesses_canonical_for_new_spec(tmp_path: Path) -> None:
    """preset 声明变更（新增规格）传播到存量 canonical：run_process 扫描 media_store 并补产新规格。"""
    from musicvault.application.source_state import build_audio_asset_from_file

    cfg = _make_cfg(tmp_path)
    cfg.media_store_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json((LyricLine(1000, 0, "hello"),)), 0.0)

    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")

    # 模拟此前用 FLAC-only preset 处理过：spec 覆盖不足（缺 MP3-192k），非 force 也应重处理
    repo.upsert_track(_make_track(333))
    repo.upsert_media_asset(build_audio_asset_from_file(333, "FLAC", canonical))
    _process_repository(cfg).mark_processed(333, 0.0)

    class _Mp3Preset(BasePreset):
        format = AudioFormat.MP3
        bitrate = "192k"

    def _route(src, track, output_dir, audio_specs, force=False):
        del src, track, audio_specs, force
        out = output_dir / "333_192k.mp3"
        out.write_bytes(b"fake mp3")
        return {(AudioFormat.MP3, "192k"): out}

    organizer = MagicMock()
    organizer.route_audio.side_effect = _route
    api = MagicMock()
    api.get_track_detail.return_value = None  # 走 fallback Track
    svc = _process_svc(cfg, repo, organizer=organizer, api=api, presets={"mp3": _Mp3Preset()})
    result = svc.run_process(downloaded=[], force=False)

    assert result.processed == 1
    assert (cfg.media_store_dir / "333" / "333_192k.mp3").exists()
    # 新规格产物登记进 media_assets，此后再次运行跳过
    assert _process_repository(cfg).is_processed(333, {"MP3-192k"})
    again = svc.run_process(downloaded=[], force=False)
    assert again.processed == 0


def test_run_process_scan_survives_track_detail_api_failure(tmp_path: Path) -> None:
    """存量 canonical 的 get_track_detail 抛异常（网络故障重试耗尽）→ 单曲计入 failed，其余曲目继续处理。

    spec 覆盖不足的存量曲目逐首打详情 API（preset 规格变更后首次 sync 的场景），
    修复前任一曲目失败会冒泡崩溃整个 run_process；修复后单曲降级、不阻塞整体。
    """
    from musicvault.application.source_state import build_audio_asset_from_file

    cfg = _make_cfg(tmp_path)
    cfg.media_store_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json((LyricLine(1000, 0, "hello"),)), 0.0)

    # 曲目 333：详情 API 抛异常（类似 _retry_api 重试耗尽后重新抛出）
    bad = cfg.media_store_dir / "333" / "333.flac"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"fake flac")
    repo.upsert_track(_make_track(333))
    repo.upsert_media_asset(build_audio_asset_from_file(333, "FLAC", bad))
    _process_repository(cfg).mark_processed(333, 0.0)

    # 曲目 999：详情 API 正常（返回 None 走 fallback Track），应被正常处理
    good = cfg.media_store_dir / "999" / "999.flac"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(b"fake flac")
    repo.upsert_track(_make_track(999))
    repo.upsert_media_asset(build_audio_asset_from_file(999, "FLAC", good))
    _process_repository(cfg).mark_processed(999, 0.0)

    class _Mp3Preset(BasePreset):
        format = AudioFormat.MP3
        bitrate = "192k"

    def _route(src, track, output_dir, audio_specs, force=False):
        del src, audio_specs, force
        out = output_dir / f"{track.id}_192k.mp3"
        out.write_bytes(b"fake mp3")
        return {(AudioFormat.MP3, "192k"): out}

    def _detail(track_id: int) -> Track | None:
        if track_id == 333:
            raise OSError("网络故障（重试耗尽）")
        return None

    organizer = MagicMock()
    organizer.route_audio.side_effect = _route
    api = MagicMock()
    api.get_track_detail.side_effect = _detail
    svc = _process_svc(cfg, repo, organizer=organizer, api=api, presets={"mp3": _Mp3Preset()})

    result = svc.run_process(downloaded=[], force=False)
    assert result.processed == 1  # 999 正常处理
    assert result.failed == 1  # 333 详情失败计入 failed
    assert result.skipped == 0
    assert (cfg.media_store_dir / "999" / "999_192k.mp3").exists()
    assert not (cfg.media_store_dir / "333" / "333_192k.mp3").exists()

    # 再次运行不崩溃：999 已被覆盖跳过，333 仍逐首降级为 failed
    again = svc.run_process(downloaded=[], force=False)
    assert again.processed == 0
    assert again.skipped == 1
    assert again.failed == 1


def test_run_process_force_reprocesses_covered_canonical(tmp_path: Path) -> None:
    """force 重处理语义：spec 已覆盖的存量 canonical 非 force 跳过，force 时无条件重处理。"""
    from musicvault.application.source_state import build_audio_asset_from_file

    cfg = _make_cfg(tmp_path)
    cfg.media_store_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json((LyricLine(1000, 0, "hello"),)), 0.0)

    canonical = cfg.media_store_dir / "333" / "333.flac"
    mp3 = cfg.media_store_dir / "333" / "333_192k.mp3"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")
    mp3.write_bytes(b"fake mp3")
    repo.upsert_track(_make_track(333))
    repo.upsert_media_asset(build_audio_asset_from_file(333, "FLAC", canonical))
    repo.upsert_media_asset(build_audio_asset_from_file(333, "MP3-192k", mp3))
    _process_repository(cfg).mark_processed(333, 0.0)

    class _Mp3Preset(BasePreset):
        format = AudioFormat.MP3
        bitrate = "192k"

    organizer = MagicMock()
    api = MagicMock()
    api.get_track_detail.return_value = None
    svc = _process_svc(cfg, repo, organizer=organizer, api=api, presets={"mp3": _Mp3Preset()})

    result = svc.run_process(downloaded=[], force=False)
    assert result.processed == 0
    assert result.skipped == 1
    organizer.route_audio.assert_not_called()

    result = svc.run_process(downloaded=[], force=True)
    assert result.processed == 1
    organizer.route_audio.assert_called()


def test_preset_dict_key_must_match_preset_name(tmp_path: Path) -> None:
    """presets 注入键必须与 preset.name 一致：LRC 文件名与分发按注册名对应，键名漂移会静默失配。"""
    from musicvault.preset_api.v1 import PresetLoadError

    cfg = _make_cfg(tmp_path)
    cfg.media_store_dir.mkdir(parents=True)
    repo = _repository(cfg)

    class _NamedPreset(BasePreset):
        format = AudioFormat.FLAC
        name = "archive"

    with pytest.raises(PresetLoadError, match="archive"):
        _process_svc(cfg, repo, organizer=MagicMock(), presets={"wrong-key": _NamedPreset()})

    # 键名一致则正常构造
    _process_svc(cfg, repo, organizer=MagicMock(), presets={"archive": _NamedPreset()})


def test_process_build_lyrics_error_isolates_preset(tmp_path: Path, caplog) -> None:
    """build_lyrics 抛异常（preset 脚本代码）→ 该 preset 歌词文件跳过，不阻塞其他 preset 与整曲。"""
    cfg = _make_cfg(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json((LyricLine(1000, 0, "hello"),)), 0.0)

    raw = cfg.cache_dir / "333.mp3"
    raw.write_bytes(b"fake mp3")
    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")

    class _ExplodingPreset(BasePreset):
        format = AudioFormat.FLAC

        def build_lyrics(self, line):
            del line
            raise RuntimeError("preset 脚本崩溃")

    p_bad = _ExplodingPreset()
    p_good = _CustomLyricsPreset("good lrc")
    organizer = MagicMock()
    organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
    svc = _process_svc(cfg, repo, organizer=organizer, presets={"bad": p_bad, "good": p_good})

    with caplog.at_level(logging.WARNING, logger="musicvault.application.process_use_case"):
        result = svc.run_process(downloaded=[_downloaded(333, raw)], force=False)

    assert result.processed == 1  # 整曲成功，不计 failed
    assert result.failed == 0
    # 坏 preset 的歌词文件不写，另一 preset 正常写
    assert not (cfg.media_store_dir / "333" / "333.bad.lrc").exists()
    assert (cfg.media_store_dir / "333" / "333.good.lrc").read_text(encoding="utf-8") == "good lrc"
    # 警告记录含 preset 名与曲目 ID
    assert any("bad" in r.message and "333" in r.message for r in caplog.records)


def test_process_multiline_lyrics_filters_empty_lines(tmp_path: Path) -> None:
    """多行歌词：框架逐行调用 build_lyrics，空文本行被过滤，其余行 join 成 .lrc。"""
    cfg = _make_cfg(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(
        333,
        lyrics_to_json(
            (
                LyricLine(1000, 0, "first"),
                LyricLine(2000, 0, "skip-me"),
                LyricLine(3000, 0, "third"),
            )
        ),
        0.0,
    )

    raw = cfg.cache_dir / "333.mp3"
    raw.write_bytes(b"fake mp3")
    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")

    class _LineFilteringPreset(BasePreset):
        format = AudioFormat.FLAC

        def build_lyrics(self, line: LyricLine) -> str:
            if line.text == "skip-me":
                return ""
            return f"[{line.start_ms}]{line.text}"

    organizer = MagicMock()
    organizer.route_audio.return_value = {(AudioFormat.FLAC, None): canonical}
    svc = _process_svc(cfg, repo, organizer=organizer, presets={"custom": _LineFilteringPreset()})
    svc.run_process(downloaded=[_downloaded(333, raw)], force=False)

    lrc = cfg.media_store_dir / "333" / "333.custom.lrc"
    assert lrc.read_text(encoding="utf-8") == "[1000]first\n[3000]third"


def test_safe_track_detail_cached_within_instance(tmp_path: Path) -> None:
    """_safe_track 单次缓存：同一实例内同曲目 scan 与 process 各走一次只打 1 次详情 API。

    存量 canonical 重处理先经 run_process 的 scan 路径 _safe_track 一次，
    _process_file 无 prefetched 直调时命中缓存，不再重复请求详情。
    """
    from musicvault.application.source_state import build_audio_asset_from_file
    from musicvault.shared.utils import workspace_rel_path

    cfg = _make_cfg(tmp_path)
    cfg.media_store_dir.mkdir(parents=True)
    repo = _repository(cfg)
    repo.upsert_track(_make_track(333))
    repo.save_lyrics(333, lyrics_to_json((LyricLine(1000, 0, "hello"),)), 0.0)

    canonical = cfg.media_store_dir / "333" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")
    repo.upsert_track(_make_track(333))
    repo.upsert_media_asset(build_audio_asset_from_file(333, "FLAC", canonical))
    _process_repository(cfg).mark_processed(333, 0.0)

    class _Mp3Preset(BasePreset):
        format = AudioFormat.MP3
        bitrate = "192k"

    def _route(src, track, output_dir, audio_specs, force=False):
        del src, audio_specs, force
        out = output_dir / f"{track.id}_192k.mp3"
        out.write_bytes(b"fake mp3")
        return {(AudioFormat.MP3, "192k"): out}

    organizer = MagicMock()
    organizer.route_audio.side_effect = _route
    api = MagicMock()
    api.get_track_detail.return_value = None  # 走 fallback Track
    svc = _process_svc(cfg, repo, organizer=organizer, api=api, presets={"mp3": _Mp3Preset()})

    # scan 路径（run_process 内部 _safe_track 一次）
    result = svc.run_process(downloaded=[], force=False)
    assert result.processed == 1

    # process 路径（_process_file 无 prefetched 时再走 _safe_track）：缓存命中不再打详情 API
    rel = workspace_rel_path(canonical, cfg.workspace_path)
    _process_repository(cfg).mark_downloaded(rel, 333)
    svc._process_file(canonical)
    assert api.get_track_detail.call_count == 1

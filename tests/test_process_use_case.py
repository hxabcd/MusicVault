"""ProcessUseCase 重构测试：离线歌词消费、preset 歌词函数、歌词文件写出。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.process_use_case import ProcessUseCase
from musicvault.core.config import Config
from musicvault.domain.lyrics import LyricLine, lyrics_to_json
from musicvault.domain.models import DownloadedTrack, Track
from musicvault.preset_api.v1 import AudioFormat, BasePreset, LyricEncoding, MetadataSpec


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def _make_track(track_id: int) -> Track:
    return Track(id=track_id, name=f"Song {track_id}", artists=["Artist"], album="Album", raw={})


def _repository(cfg: Config) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(cfg.state_db_file))


def _downloaded(track_id: int, source_file: Path) -> DownloadedTrack:
    return DownloadedTrack(track=_make_track(track_id), source_file=str(source_file), is_ncm=False, playlist_ids=[])


class _CustomLyricsPreset(BasePreset):
    """自定义 build_lyrics 的 preset：记录收到的行并返回固定文本。"""

    format = AudioFormat.FLAC

    def __init__(self, text: str = "custom lrc") -> None:
        self.text = text
        self.received: tuple[LyricLine, ...] = ()

    def build_lyrics(self, lines: tuple[LyricLine, ...]) -> str:
        self.received = lines
        return self.text


class _DefaultLyricsPreset(BasePreset):
    """默认 build_lyrics（standard_lrc）：空行列表返回空文本。"""

    format = AudioFormat.FLAC


class _RecordingMetadata:
    """记录 metadata.write 参数的 fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, MetadataSpec]] = []

    def write(self, audio_file: Path, track: Track, *, metadata: MetadataSpec, cover_timeout: int = 15) -> None:
        self.calls.append((audio_file, metadata))


def _process_svc(
    cfg: Config,
    repo: SQLiteStateRepository,
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
        presets=presets,
    )


def test_process_writes_lyrics_file_from_preset(tmp_path: Path) -> None:
    """离线歌词消费：state.get_lyrics → lines → preset.build_lyrics → 写 {tid}.{preset}.lrc。"""
    cfg = _make_cfg(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    repo = _repository(cfg)
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
    assert preset.received == (LyricLine(1000, 0, "hello"),)
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

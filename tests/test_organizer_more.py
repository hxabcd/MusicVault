"""Organizer 补充单测：路由去重、force 覆盖、ffmpeg 转码成功/失败与边界。

覆盖：目标已存在（跳过 / force 覆盖）、wav/ape 无损源转 FLAC、有损源转有损
目标（含 codec 映射与默认 bitrate）、ffmpeg 缺失告警与 RuntimeError、
_transcode_* 的失败分支、模块级文件名/扩展名辅助函数。
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from musicvault.adapters.processors import organizer as organizer_module
from musicvault.adapters.processors.organizer import Organizer, _format_to_ext, _spec_to_filename
from musicvault.domain.models import Track
from musicvault.preset_api.v1 import AudioFormat


def _make_track(track_id: int) -> Track:
    return Track(id=track_id, name="Test", artists=["A"], album="B", cover_url=None, raw={})


def _success_proc() -> Mock:
    return Mock(returncode=0, stderr=b"")


def _failure_proc(stderr: bytes = b"encode boom") -> Mock:
    return Mock(returncode=1, stderr=stderr)


@pytest.fixture
def no_ffmpeg(monkeypatch) -> None:
    """模拟环境无 ffmpeg：which 返回 None。"""
    monkeypatch.setattr(organizer_module.shutil, "which", Mock(return_value=None))


class TestInitWarnings:
    def test_warns_when_ffmpeg_missing(self, no_ffmpeg, monkeypatch) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(organizer_module, "output_warn", warnings.append)

        org = Organizer(ffmpeg_path="")

        assert org._ffmpeg_path is None
        assert warnings and "未检测到 ffmpeg" in warnings[0]

    def test_ffmpeg_threads_at_least_one(self, monkeypatch) -> None:
        monkeypatch.setattr(organizer_module.shutil, "which", Mock(return_value="ffmpeg"))

        assert Organizer(ffmpeg_threads=0).ffmpeg_threads == 1


class TestRouteAudioExistingTarget:
    def test_existing_target_skipped_without_force(self, tmp_path) -> None:
        """目标已存在且 force=False → 跳过（保留已有内容，结果指向旧文件）。"""
        src = tmp_path / "test.mp3"
        src.write_bytes(b"source-data")
        output = tmp_path / "out"
        target = output / "1.mp3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing-data")

        result = Organizer(ffmpeg_path="").route_audio(src, _make_track(1), output, {(None, None)})

        assert result[(None, None)] == target
        assert target.read_bytes() == b"existing-data"

    def test_force_replaces_existing_target(self, tmp_path) -> None:
        """目标已存在且 force=True → 删除后重新生成。"""
        src = tmp_path / "test.mp3"
        src.write_bytes(b"source-data")
        output = tmp_path / "out"
        target = output / "1.mp3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stale-data")

        result = Organizer(ffmpeg_path="").route_audio(src, _make_track(1), output, {(None, None)}, force=True)

        assert result[(None, None)] == target
        assert target.read_bytes() == b"source-data"


class TestRouteAudioTranscode:
    def test_wav_source_transcoded_to_flac(self, tmp_path, monkeypatch) -> None:
        """无损源（wav）转 FLAC → 走 _transcode_to_flac。"""
        src = tmp_path / "test.wav"
        src.write_bytes(b"fake-wav")
        run = Mock(return_value=_success_proc())
        monkeypatch.setattr(organizer_module.subprocess, "run", run)

        org = Organizer(ffmpeg_path="ffmpeg")
        result = org.route_audio(src, _make_track(1), tmp_path / "out", {(AudioFormat.FLAC, None)})

        assert result[(AudioFormat.FLAC, None)].name == "1.flac"
        cmd = run.call_args.args[0]
        assert "-threads" in cmd and cmd[cmd.index("-threads") + 1] == "1"
        assert cmd[cmd.index("-codec:a") + 1] == "flac"

    def test_ape_source_transcoded_to_flac(self, tmp_path, monkeypatch) -> None:
        src = tmp_path / "test.ape"
        src.write_bytes(b"fake-ape")
        run = Mock(return_value=_success_proc())
        monkeypatch.setattr(organizer_module.subprocess, "run", run)

        org = Organizer(ffmpeg_path="ffmpeg")
        result = org.route_audio(src, _make_track(1), tmp_path / "out", {(AudioFormat.FLAC, None)})

        assert result[(AudioFormat.FLAC, None)].name == "1.flac"

    def test_flac_source_transcoded_to_mp3_with_default_bitrate(self, tmp_path, monkeypatch) -> None:
        """有损目标 + bitrate=None → 默认 192k。"""
        src = tmp_path / "test.flac"
        src.write_bytes(b"fake-flac")
        run = Mock(return_value=_success_proc())
        monkeypatch.setattr(organizer_module.subprocess, "run", run)

        org = Organizer(ffmpeg_path="ffmpeg")
        result = org.route_audio(src, _make_track(1), tmp_path / "out", {(AudioFormat.MP3, None)})

        assert result[(AudioFormat.MP3, None)].name == "1.mp3"
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-codec:a") + 1] == "libmp3lame"
        assert cmd[cmd.index("-b:a") + 1] == "192k"

    @pytest.mark.parametrize(
        ("fmt", "codec", "ext"),
        [
            (AudioFormat.MP3, "libmp3lame", "mp3"),
            (AudioFormat.AAC, "aac", "m4a"),
            (AudioFormat.OGG, "libvorbis", "ogg"),
            (AudioFormat.OPUS, "libopus", "opus"),
        ],
    )
    def test_lossy_codec_mapping(self, tmp_path, monkeypatch, fmt, codec, ext) -> None:
        """有损转码的 codec 与扩展名映射。"""
        src = tmp_path / "test.flac"
        src.write_bytes(b"fake-flac")
        run = Mock(return_value=_success_proc())
        monkeypatch.setattr(organizer_module.subprocess, "run", run)

        org = Organizer(ffmpeg_path="ffmpeg")
        result = org.route_audio(src, _make_track(1), tmp_path / "out", {(fmt, "320k")})

        assert result[(fmt, "320k")].name == f"1_320k.{ext}"
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-codec:a") + 1] == codec
        assert cmd[cmd.index("-b:a") + 1] == "320k"

    def test_uppercase_source_suffix_copied(self, tmp_path) -> None:
        """源扩展名大写时按小写匹配，直接复制而非转码。"""
        src = tmp_path / "test.MP3"
        src.write_bytes(b"fake-mp3")
        output = tmp_path / "out"

        org = Organizer(ffmpeg_path="")
        result = org.route_audio(src, _make_track(1), output, {(AudioFormat.MP3, "320k")})

        # ext（.mp3）== suffix（.mp3）→ 复制分支，目标带 bitrate 后缀
        assert result[(AudioFormat.MP3, "320k")].name == "1_320k.mp3"
        assert result[(AudioFormat.MP3, "320k")].read_bytes() == b"fake-mp3"


class TestTranscodeToFlac:
    def test_without_ffmpeg_raises(self, tmp_path, no_ffmpeg) -> None:
        org = Organizer(ffmpeg_path="")
        src = tmp_path / "src.wav"
        src.write_bytes(b"x")

        with pytest.raises(RuntimeError, match="未找到 ffmpeg"):
            org._transcode_to_flac(src, tmp_path / "dst.flac")

    def test_ffmpeg_failure_raises_with_stderr(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(organizer_module.subprocess, "run", Mock(return_value=_failure_proc()))
        org = Organizer(ffmpeg_path="ffmpeg")
        src = tmp_path / "src.wav"
        src.write_bytes(b"x")

        with pytest.raises(RuntimeError, match="encode boom"):
            org._transcode_to_flac(src, tmp_path / "dst.flac")

    def test_success_creates_dst_dir(self, tmp_path, monkeypatch) -> None:
        """目标父目录不存在时自动创建。"""
        run = Mock(return_value=_success_proc())
        monkeypatch.setattr(organizer_module.subprocess, "run", run)
        org = Organizer(ffmpeg_path="ffmpeg")
        src = tmp_path / "src.wav"
        src.write_bytes(b"x")
        dst = tmp_path / "nested" / "sub" / "dst.flac"

        org._transcode_to_flac(src, dst)

        assert dst.parent.is_dir()


class TestTranscodeLossy:
    def test_without_ffmpeg_raises(self, tmp_path, no_ffmpeg) -> None:
        org = Organizer(ffmpeg_path="")
        src = tmp_path / "src.flac"
        src.write_bytes(b"x")

        with pytest.raises(RuntimeError, match="未找到 ffmpeg"):
            org._transcode_lossy(src, tmp_path / "dst.mp3", AudioFormat.MP3, "192k")

    def test_ffmpeg_failure_raises_with_stderr(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(organizer_module.subprocess, "run", Mock(return_value=_failure_proc(b"mux error")))
        org = Organizer(ffmpeg_path="ffmpeg")
        src = tmp_path / "src.flac"
        src.write_bytes(b"x")

        with pytest.raises(RuntimeError, match="mux error"):
            org._transcode_lossy(src, tmp_path / "dst.mp3", AudioFormat.MP3, "192k")


class TestModuleHelpers:
    def test_format_to_ext_none_keeps_source_suffix(self) -> None:
        assert _format_to_ext(None, ".ncm") == ".ncm"

    def test_format_to_ext_mapped(self) -> None:
        assert _format_to_ext(AudioFormat.AAC, ".mp3") == ".m4a"

    def test_format_to_ext_enum_value_fallback(self) -> None:
        assert _format_to_ext(AudioFormat.FLAC, ".mp3") == ".flac"

    def test_spec_filename_original(self) -> None:
        assert _spec_to_filename(1, None, None) == "1.mp3"

    def test_spec_filename_original_with_source_suffix(self) -> None:
        assert _spec_to_filename(1, None, None, source_suffix=".ncm") == "1.ncm"

    def test_spec_filename_with_bitrate(self) -> None:
        assert _spec_to_filename(1, AudioFormat.MP3, "320k") == "1_320k.mp3"

    def test_spec_filename_without_bitrate(self) -> None:
        assert _spec_to_filename(1, AudioFormat.MP3, None) == "1.mp3"

    def test_spec_filename_enum_value_fallback(self) -> None:
        assert _spec_to_filename(1, AudioFormat.FLAC, "800k") == "1_800k.flac"

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from musicvault.domain.models import Track
from musicvault.preset_api.v1 import AudioFormat
from musicvault.shared.output import warn as output_warn

_LOSSY_SUFFIX_MAP = {AudioFormat.MP3: ".mp3", AudioFormat.AAC: ".m4a", AudioFormat.OGG: ".ogg", AudioFormat.OPUS: ".opus"}
_LOSSY_CODEC_MAP = {AudioFormat.MP3: "libmp3lame", AudioFormat.AAC: "aac", AudioFormat.OGG: "libvorbis", AudioFormat.OPUS: "libopus"}


class Organizer:
    def __init__(
        self,
        ffmpeg_threads: int = 1,
        ffmpeg_path: str = "",
    ) -> None:
        self.ffmpeg_threads = max(1, ffmpeg_threads)
        self._ffmpeg_path = ffmpeg_path.strip() or shutil.which("ffmpeg")
        if self._ffmpeg_path is None:
            output_warn("未检测到 ffmpeg，转码功能将不可用")

    def route_audio(
        self,
        src: Path,
        track: Track,
        output_dir: Path,
        audio_specs: set[tuple[AudioFormat | None, str | None]],
        force: bool = False,
    ) -> dict[tuple[AudioFormat | None, str | None], Path]:
        """路由音频源文件到 N 个 canonical 文件（按规格去重）。

        返回 {spec: canonical_path}（键与传入的 spec 元素类型一致）。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = src.suffix.lower()
        result: dict[tuple[AudioFormat | None, str | None], Path] = {}

        # 兼容 str 输入（Task 12 前 process_use_case 仍传字符串 format）：统一归一化为枚举后再计算
        normalized_specs = {(_coerce_format(fmt), bitrate) for fmt, bitrate in audio_specs}
        same_format_counts = _count_same_formats(normalized_specs)

        for fmt, bitrate in audio_specs:
            fmt_enum = _coerce_format(fmt)
            spec = (fmt, bitrate)
            ext = _format_to_ext(fmt_enum, suffix)
            filename = _spec_to_filename(track.id, fmt_enum, bitrate, same_format_counts.get(fmt_enum, 0), source_suffix=suffix)
            target = output_dir / filename

            if target.exists():
                if force:
                    target.unlink()
                else:
                    result[spec] = target
                    continue

            if fmt_enum is None or ext == suffix:
                _copy(src, target)
            elif suffix in {".flac", ".wav", ".ape"} and fmt_enum is AudioFormat.FLAC:
                if suffix == ".flac":
                    _copy(src, target)
                else:
                    self._transcode_to_flac(src, target)
            elif suffix in {".flac", ".wav", ".ape"} and fmt_enum is not AudioFormat.FLAC:
                self._transcode_lossy(src, target, fmt_enum, bitrate or "192k")
            else:
                self._transcode_lossy(src, target, fmt_enum, bitrate or "192k")

            result[spec] = target

        return result

    def _transcode_to_flac(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self._ffmpeg_path:
            raise RuntimeError(f"转码失败：未找到 ffmpeg，文件={src.name}")
        cmd = [
            self._ffmpeg_path, "-y", "-threads", str(self.ffmpeg_threads),
            "-i", str(src), "-codec:a", "flac", str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")

    def _transcode_lossy(self, src: Path, dst: Path, fmt: AudioFormat, bitrate: str) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self._ffmpeg_path:
            raise RuntimeError(f"转码失败：未找到 ffmpeg，文件={src.name}")
        codec = _LOSSY_CODEC_MAP.get(fmt, "libmp3lame")
        cmd = [
            self._ffmpeg_path, "-y", "-threads", str(self.ffmpeg_threads),
            "-i", str(src), "-codec:a", codec, "-b:a", bitrate, str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")


def _coerce_format(fmt: AudioFormat | str | None) -> AudioFormat | None:
    """将字符串 format 归一化为枚举（Task 12 改为纯枚举后可移除）。"""
    if fmt is None or isinstance(fmt, AudioFormat):
        return fmt
    return AudioFormat(fmt)


def _format_to_ext(fmt: AudioFormat | None, source_suffix: str) -> str:
    if fmt is None:
        return source_suffix
    return _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt.value}")


def _spec_to_filename(track_id: int, fmt: AudioFormat | None, bitrate: str | None, same_format_count: int, source_suffix: str = ".mp3") -> str:
    if fmt is None:
        return f"{track_id}{source_suffix}"
    ext = _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt.value}")
    if bitrate:
        return f"{track_id}_{bitrate}{ext}"
    return f"{track_id}{ext}"


def _count_same_formats(specs: set[tuple[AudioFormat | None, str | None]]) -> dict[AudioFormat | None, int]:
    counts: dict[AudioFormat | None, int] = {}
    for fmt, _ in specs:
        counts[fmt] = counts.get(fmt, 0) + 1
    return counts


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

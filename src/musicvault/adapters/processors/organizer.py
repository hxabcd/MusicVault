from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from musicvault.core.models import Track
from musicvault.shared.output import warn as output_warn

_LOSSY_SUFFIX_MAP = {"mp3": ".mp3", "aac": ".m4a", "ogg": ".ogg", "opus": ".opus"}
_LOSSY_CODEC_MAP = {"mp3": "libmp3lame", "aac": "aac", "ogg": "libvorbis", "opus": "libopus"}


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
        audio_specs: set[tuple[str | None, str | None]],
    ) -> dict[tuple[str | None, str | None], Path]:
        """路由音频源文件到 N 个 canonical 文件（按规格去重）。

        返回 {spec: canonical_path}。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = src.suffix.lower()
        result: dict[tuple[str | None, str | None], Path] = {}

        same_format_counts = _count_same_formats(audio_specs)

        for fmt, bitrate in audio_specs:
            ext = _format_to_ext(fmt, suffix)
            filename = _spec_to_filename(track.id, fmt, bitrate, same_format_counts.get(fmt, 0))
            target = output_dir / filename

            if target.exists():
                result[(fmt, bitrate)] = target
                continue

            if fmt is None or ext == suffix:
                _copy(src, target)
            elif suffix in {".flac", ".wav", ".ape"} and fmt == "flac":
                if suffix == ".flac":
                    _copy(src, target)
                else:
                    self._transcode_to_flac(src, target)
            elif suffix in {".flac", ".wav", ".ape"} and fmt != "flac":
                self._transcode_lossy(src, target, fmt, bitrate or "192k")
            else:
                self._transcode_lossy(src, target, fmt, bitrate or "192k")

            result[(fmt, bitrate)] = target

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

    def _transcode_lossy(self, src: Path, dst: Path, fmt: str, bitrate: str) -> None:
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


def _format_to_ext(fmt: str | None, source_suffix: str) -> str:
    if fmt is None:
        return source_suffix
    return _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt}")


def _spec_to_filename(track_id: int, fmt: str | None, bitrate: str | None, same_format_count: int) -> str:
    if fmt is None:
        return f"{track_id}{_LOSSY_SUFFIX_MAP.get('mp3', '.mp3')}"
    ext = _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt}")
    if same_format_count > 1 and bitrate:
        return f"{track_id}_{bitrate}{ext}"
    return f"{track_id}{ext}"


def _count_same_formats(specs: set[tuple[str | None, str | None]]) -> dict[str | None, int]:
    counts: dict[str | None, int] = {}
    for fmt, _ in specs:
        counts[fmt] = counts.get(fmt, 0) + 1
    return counts


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

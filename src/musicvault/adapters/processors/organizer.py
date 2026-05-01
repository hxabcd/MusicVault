from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from musicvault.core.models import Track
from musicvault.shared.output import warn as output_warn

_LOSSY_SUFFIX_MAP = {"mp3": ".mp3", "aac": ".m4a", "ogg": ".ogg", "opus": ".opus"}
_LOSSY_CODEC_MAP = {"mp3": "libmp3lame", "aac": "aac", "ogg": "libvorbis", "opus": "libopus"}


class Organizer:
    """音频处理：输出 canonical lossless + lossy 到 output_dir"""

    def __init__(
        self,
        ffmpeg_threads: int = 1,
        lossy_bitrate: str = "192k",
        lossy_format: str = "mp3",
        ffmpeg_path: str = "",
    ) -> None:
        self.ffmpeg_threads = max(1, ffmpeg_threads)
        self.lossy_bitrate = lossy_bitrate
        self.lossy_format = lossy_format
        self._lossy_suffix = _LOSSY_SUFFIX_MAP.get(lossy_format, ".mp3")
        self._ffmpeg_path = ffmpeg_path.strip() or shutil.which("ffmpeg")
        if self._ffmpeg_path is None:
            output_warn("未检测到 ffmpeg，转码功能将不可用")

    def route_audio(self, src: Path, track: Track, output_dir: Path) -> tuple[Path, Path]:
        suffix = src.suffix.lower()

        if self._is_lossless_suffix(suffix):
            lossless_target = output_dir / f"{track.id}.flac"
            lossy_target = output_dir / f"{track.id}{self._lossy_suffix}"
            if suffix == ".flac":
                self._copy(src, lossless_target)
            else:
                self._transcode_to_flac(src, lossless_target)
            self._transcode_lossy(src, lossy_target)
            return lossless_target, lossy_target

        lossless_target = output_dir / f"{track.id}{self._lossy_suffix}"
        if suffix == self._lossy_suffix:
            self._copy(src, lossless_target)
        else:
            self._transcode_lossy(src, lossless_target)
        return lossless_target, lossless_target

    def _copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def _transcode_to_flac(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self._ffmpeg_path:
            raise RuntimeError(f"转码失败：未找到 ffmpeg，文件={src.name}")
        cmd = [
            self._ffmpeg_path,
            "-y",
            "-threads",
            str(self.ffmpeg_threads),
            "-i",
            str(src),
            "-codec:a",
            "flac",
            str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")

    def _transcode_lossy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self._ffmpeg_path:
            raise RuntimeError(f"转码失败：未找到 ffmpeg，文件={src.name}")
        codec = _LOSSY_CODEC_MAP.get(self.lossy_format, "libmp3lame")
        cmd = [
            self._ffmpeg_path,
            "-y",
            "-threads",
            str(self.ffmpeg_threads),
            "-i",
            str(src),
            "-codec:a",
            codec,
            "-b:a",
            self.lossy_bitrate,
            str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")

    @staticmethod
    def _is_lossless_suffix(suffix: str) -> bool:
        return suffix in {".flac", ".wav", ".ape"}

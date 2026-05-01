from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from musicvault.core.models import Track
from musicvault.shared.output import warn as output_warn

_FFMPEG_BITRATE = "192k"


class Organizer:
    """音频处理：输出 canonical lossless ({track_id}.flac) + lossy ({track_id}.mp3) 到 output_dir"""

    def __init__(self, ffmpeg_threads: int = 1) -> None:
        self.ffmpeg_threads = max(1, ffmpeg_threads)
        self._ffmpeg_path = shutil.which("ffmpeg")
        if self._ffmpeg_path is None:
            output_warn("未检测到 ffmpeg，转码功能将不可用")

    def route_audio(self, src: Path, track: Track, output_dir: Path) -> tuple[Path, Path]:
        """输出 canonical 文件到 output_dir。
        无损源 → {track_id}.flac + {track_id}.mp3
        有损源 → {track_id}.mp3（同时作为 lossless/lossy，用 ID3 存完整元数据）
        """
        suffix = src.suffix.lower()

        if self._is_lossless_suffix(suffix):
            lossless_target = output_dir / f"{track.id}.flac"
            lossy_target = output_dir / f"{track.id}.mp3"
            if suffix == ".flac":
                self._copy(src, lossless_target)
            else:
                self._transcode_to_flac(src, lossless_target)
            self._transcode_to_mp3(src, lossy_target)
            return lossless_target, lossy_target

        # 有损源：只产出 .mp3，作为 canonical 唯一文件
        lossless_target = output_dir / f"{track.id}.mp3"
        if suffix == ".mp3":
            self._copy(src, lossless_target)
        else:
            self._transcode_to_mp3(src, lossless_target)
        return lossless_target, lossless_target

    def _copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

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

    def _transcode_to_mp3(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self._ffmpeg_path:
            raise RuntimeError(f"转码失败：未找到 ffmpeg，文件={src.name}")
        cmd = [
            self._ffmpeg_path, "-y", "-threads", str(self.ffmpeg_threads),
            "-i", str(src), "-codec:a", "libmp3lame", "-b:a", _FFMPEG_BITRATE, str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")

    @staticmethod
    def _is_lossless_suffix(suffix: str) -> bool:
        return suffix in {".flac", ".wav", ".ape"}

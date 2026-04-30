from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from musicvault.core.models import Track
from musicvault.shared.output import warn as output_warn
from musicvault.shared.utils import safe_filename

_FFMPEG_BITRATE = "192k"


class Organizer:
    """音频文件分流与转码"""

    def __init__(self, ffmpeg_threads: int = 1) -> None:
        self.ffmpeg_threads = max(1, ffmpeg_threads)
        self._ffmpeg_path = shutil.which("ffmpeg")
        if self._ffmpeg_path is None:
            output_warn("未检测到 ffmpeg，转码功能将不可用")

    def route_audio(self, src: Path, track: Track, lossless_dir: Path, lossy_dir: Path) -> tuple[Path, Path]:
        """按规则生成 lossless 与 lossy 两份输出。"""
        # 1. 根据曲目信息生成目标文件名并判断输入音质类型。
        suffix = src.suffix.lower()
        base_name = safe_filename(f"{track.artist_text} - {track.name}")
        lossy_name = self._lossy_name(track)

        # 2. 无损源直接复制到 lossless，并转码生成 lossy mp3。
        if self._is_lossless_suffix(suffix):
            lossless_target = lossless_dir / f"{base_name}{suffix}"
            lossy_target = lossy_dir / f"{lossy_name}.mp3"
            self._copy(src, lossless_target)
            self._transcode_to_mp3(src, lossy_target)
            return lossless_target, lossy_target

        # 3. 有损源保留原格式到 lossless，同时保证输出 lossy mp3。
        lossless_target = lossless_dir / f"{base_name}{suffix}"
        lossy_target = lossy_dir / f"{lossy_name}.mp3"
        self._copy(src, lossless_target)
        if suffix == ".mp3":
            self._copy(src, lossy_target)
        else:
            self._transcode_to_mp3(src, lossy_target)
        return lossless_target, lossy_target

    def _copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def _transcode_to_mp3(self, src: Path, dst: Path) -> None:
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
            "libmp3lame",
            "-b:a",
            _FFMPEG_BITRATE,
            str(dst),
        ]

        # 2. 执行转码并在失败时透传 ffmpeg 错误输出。
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")

    def _lossy_name(self, track: Track) -> str:
        """生成 lossy 文件名：{别名} {原名} - {歌手}。"""
        prefix = f"{track.alias} " if track.alias else ""
        return safe_filename(f"{prefix}{track.name} - {track.artist_text}".strip())

    @staticmethod
    def _is_lossless_suffix(suffix: str) -> bool:
        """判断扩展名是否属于无损音频。"""
        return suffix in {".flac", ".wav", ".ape"}

from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from musicvault.core.models import DownloadedTrack, Track
from musicvault.shared.utils import safe_filename


class Downloader:
    """音频下载器"""

    def download_track(self, track: Track, url: str, output_dir: Path) -> DownloadedTrack:
        """下载单曲并返回本地文件信息"""
        # 1. 准备输出目录和标准化文件名。
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(f"{track.artist_text} - {track.name}")

        # 2. 建立下载连接并读取响应头用于格式判断。
        try:
            resp = urlopen(url, timeout=30)  # nosec B310 - controlled URL from music API
            content_type = resp.headers.get("Content-Type", "")
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"下载失败，track_id={track.id}，原因：{exc}") from exc

        # 先用响应头判断格式，再回退到 URL 后缀。
        guessed_ext = ".mp3"
        if "flac" in content_type:
            guessed_ext = ".flac"
        elif "audio/x-ncm" in content_type or "application/octet-stream" in content_type:
            guessed_ext = ".ncm"
        else:
            path_ext = Path(urlparse(url).path).suffix.lower()
            if path_ext in {".ncm", ".flac", ".mp3", ".m4a"}:
                guessed_ext = path_ext

        # 3. 流式写入本地文件并返回下载结果模型。
        target = output_dir / f"{stem}{guessed_ext}"
        # 流式写入，避免大文件一次性占用内存。
        with target.open("wb") as fp:
            while True:
                chunk = resp.read(1024 * 128)
                if not chunk:
                    break
                fp.write(chunk)

        is_ncm = target.suffix.lower() == ".ncm"
        return DownloadedTrack(track=track, source_file=str(target), is_ncm=is_ncm)


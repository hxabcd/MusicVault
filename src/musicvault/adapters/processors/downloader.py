from __future__ import annotations

import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from musicvault.core.models import DownloadedTrack, Track
from musicvault.shared.utils import format_track_name

_DOWNLOAD_TIMEOUT = 30
_DOWNLOAD_CHUNK_SIZE = 1024 * 128
_RETRIES = 3
_RETRY_BACKOFF = (1.0, 3.0, 5.0)


class Downloader:
    def __init__(self, filename_template: str = "{artist} - {name}") -> None:
        self.filename_template = filename_template

    def download_track(self, track: Track, url: str, output_dir: Path) -> DownloadedTrack:
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = format_track_name(self.filename_template, track)

        resp = self._open_with_retry(url)
        content_type = resp.headers.get("Content-Type", "")

        guessed_ext = ".mp3"
        if "flac" in content_type:
            guessed_ext = ".flac"
        elif "audio/x-ncm" in content_type or "application/octet-stream" in content_type:
            guessed_ext = ".ncm"
        else:
            path_ext = Path(urlparse(url).path).suffix.lower()
            if path_ext in {".ncm", ".flac", ".mp3", ".m4a"}:
                guessed_ext = path_ext

        target = output_dir / f"{stem}{guessed_ext}"
        with target.open("wb") as fp:
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                fp.write(chunk)

        is_ncm = target.suffix.lower() == ".ncm"
        return DownloadedTrack(track=track, source_file=str(target), is_ncm=is_ncm)

    @staticmethod
    def _open_with_retry(url: str):
        for attempt in range(_RETRIES):
            if attempt > 0:
                delay = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                time.sleep(delay)
            try:
                return urlopen(url, timeout=_DOWNLOAD_TIMEOUT)  # nosec B310
            except HTTPError as exc:
                if attempt == _RETRIES - 1:
                    raise RuntimeError(f"下载失败（HTTP {exc.code}），无法恢复") from exc
                if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                    raise RuntimeError(f"下载失败（HTTP {exc.code}），不重试") from exc
            except (URLError, OSError, TimeoutError) as exc:
                if attempt == _RETRIES - 1:
                    raise RuntimeError(f"下载失败（网络错误），已重试 {_RETRIES} 次：{exc}") from exc

from __future__ import annotations

import time
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from musicvault.domain.models import DownloadedTrack, Track
from musicvault.shared.utils import format_track_name

_DOWNLOAD_TIMEOUT = 30
_DOWNLOAD_CHUNK_SIZE = 1024 * 128
_RETRY_BACKOFF = (1.0, 3.0, 5.0)


class RetryBudgetExceeded(RuntimeError):
    """跨曲目连续重试达到上限，中止下载。"""

    def __init__(self, limit: int) -> None:
        super().__init__(f"下载中止：跨曲目连续重试超过上限（{limit} 次）")


@dataclass
class RetryBudget:
    """跨曲目连续重试熔断预算：任一曲目成功即清零，达上限后熔断。

    下载批次以线程池并发，计数器需加锁保护。
    """

    limit: int
    _consecutive: int = field(default=0, init=False)
    _tripped: bool = field(default=False, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def note_retry(self) -> None:
        """记录一次重试；连续计数达到上限后置熔断标志。"""
        with self._lock:
            self._consecutive += 1
            if self._consecutive >= self.limit:
                self._tripped = True

    def note_success(self) -> None:
        """任一曲目下载成功即清零连续计数。"""
        with self._lock:
            self._consecutive = 0

    @property
    def tripped(self) -> bool:
        """是否已熔断（熔断后保持，直到新批次重建预算）。"""
        return self._tripped


class Downloader:
    def __init__(
        self,
        filename_template: str = "{artist} - {name}",
        max_retries: int = 2,
        retry_budget: RetryBudget | None = None,
    ) -> None:
        self.filename_template = filename_template
        self.max_retries = max_retries
        self.retry_budget = retry_budget

    def download_track(self, track: Track, url: str, output_dir: Path) -> DownloadedTrack:
        """下载单个曲目；失败按 max_retries 整体重试（建连 + 读流写盘）。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = format_track_name(self.filename_template, track)
        target: Path | None = None
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            if self.retry_budget is not None and self.retry_budget.tripped:
                raise RetryBudgetExceeded(self.retry_budget.limit)
            if attempt > 0:
                self._note_retry()
                time.sleep(_RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)])
            try:
                resp = urlopen(url, timeout=_DOWNLOAD_TIMEOUT)  # nosec B310
                try:
                    if target is None:
                        target = output_dir / f"{stem}{self._guess_extension(resp, url)}"
                    with target.open("wb") as fp:
                        while True:
                            chunk = resp.read(_DOWNLOAD_CHUNK_SIZE)
                            if not chunk:
                                break
                            fp.write(chunk)
                finally:
                    resp.close()
                self._note_success()
                break
            except HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                    raise RuntimeError(f"下载失败（HTTP {exc.code}），不重试") from exc
                last_exc = exc
                self._cleanup_partial(target)
            except (URLError, OSError, TimeoutError, HTTPException) as exc:
                last_exc = exc
                self._cleanup_partial(target)
        else:
            if isinstance(last_exc, HTTPError):
                raise RuntimeError(f"下载失败（HTTP {last_exc.code}），无法恢复") from last_exc
            raise RuntimeError(f"下载失败（网络错误），已重试 {self.max_retries} 次：{last_exc}") from last_exc

        is_ncm = target.suffix.lower() == ".ncm"
        return DownloadedTrack(track=track, source_file=str(target), is_ncm=is_ncm)

    @staticmethod
    def _guess_extension(resp: object, url: str) -> str:
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
        return guessed_ext

    @staticmethod
    def _cleanup_partial(target: Path | None) -> None:
        """删除重试前的半截文件，避免残留损坏文件。"""
        if target is not None and target.exists():
            try:
                target.unlink()
            except OSError:
                pass

    def _note_retry(self) -> None:
        if self.retry_budget is not None:
            self.retry_budget.note_retry()

    def _note_success(self) -> None:
        if self.retry_budget is not None:
            self.retry_budget.note_success()

"""Downloader 补充单测：扩展名推断、分块写盘与重试/熔断策略。

覆盖：Content-Type / URL 路径扩展名推断（flac/ncm/m4a/默认 mp3）、is_ncm 标志、
分块下载、download_track 逐曲目重试（4xx 不重试、5xx/408/网络/超时/中途读流重试、
瞬时失败后恢复）、RetryBudget 跨曲目连续重试熔断（递增/成功清零/熔断即中止）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from musicvault.adapters.processors import downloader as downloader_module
from musicvault.adapters.processors.downloader import Downloader, RetryBudget, RetryBudgetExceeded
from musicvault.domain.models import Track


class _FakeResponse:
    """模拟 urllib 响应：headers 字典 + 支持分块读取的 read()。

    read_error 不为 None 时每次 read 都抛出该异常（模拟中途读流失败）。
    """

    def __init__(self, data: bytes, headers: dict[str, str] | None = None, read_error: Exception | None = None) -> None:
        self._data = data
        self.headers = headers or {}
        self._read_error = read_error

    def read(self, size: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        if size < 0 or not self._data:
            chunk, self._data = self._data, b""
            return chunk
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def close(self) -> None:
        """与 http.client.HTTPResponse 对齐；当前无资源需释放。"""
        return None


def _track(track_id: int = 1) -> Track:
    return Track(id=track_id, name="测试歌曲", artists=["歌手A"], album="专辑", raw={})


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://example.invalid/1", code, "err", {}, None)


def _stub_urlopen(monkeypatch, *results) -> Mock:
    """按调用顺序依次返回 response 或抛出异常；耗尽后继续调用会抛 StopIteration。"""
    urlopen = Mock(side_effect=list(results))
    monkeypatch.setattr(downloader_module, "urlopen", urlopen)
    return urlopen


class TestDownloadTrack:
    def test_download_writes_mp3_by_default(self, tmp_path, monkeypatch) -> None:
        """无 Content-Type 且 URL 无扩展名 → 默认 .mp3。"""
        _stub_urlopen(monkeypatch, _FakeResponse(b"audio-data"))

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert item.source_file == str(tmp_path / "歌手A - 测试歌曲.mp3")
        assert item.is_ncm is False
        assert item.track == _track()
        assert Path(item.source_file).read_bytes() == b"audio-data"

    def test_flac_content_type_infers_flac_ext(self, tmp_path, monkeypatch) -> None:
        _stub_urlopen(monkeypatch, _FakeResponse(b"flac-data", {"Content-Type": "audio/flac"}))

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert item.source_file.endswith(".flac")

    def test_ncm_content_type_sets_is_ncm(self, tmp_path, monkeypatch) -> None:
        _stub_urlopen(monkeypatch, _FakeResponse(b"ncm-data", {"Content-Type": "audio/x-ncm"}))

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert item.source_file.endswith(".ncm")
        assert item.is_ncm is True

    def test_octet_stream_content_type_infers_ncm(self, tmp_path, monkeypatch) -> None:
        _stub_urlopen(monkeypatch, _FakeResponse(b"ncm-data", {"Content-Type": "application/octet-stream"}))

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert item.source_file.endswith(".ncm")
        assert item.is_ncm is True

    @pytest.mark.parametrize("path_ext", [".ncm", ".flac", ".mp3", ".m4a"])
    def test_url_path_extension_inferred(self, tmp_path, monkeypatch, path_ext) -> None:
        """无识别 Content-Type 时按 URL 路径扩展名推断。"""
        _stub_urlopen(monkeypatch, _FakeResponse(b"data", {"Content-Type": "audio/mpeg"}))

        item = Downloader().download_track(_track(), f"https://example.invalid/song{path_ext}", tmp_path)

        assert item.source_file.endswith(path_ext)
        assert item.is_ncm == (path_ext == ".ncm")

    def test_chunked_download_writes_full_content(self, tmp_path, monkeypatch) -> None:
        """大文件分块读取并完整落盘。"""
        data = b"chunk-" * 100
        _stub_urlopen(monkeypatch, _FakeResponse(data))
        monkeypatch.setattr(downloader_module, "_DOWNLOAD_CHUNK_SIZE", 7)  # 强制多次 read

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert Path(item.source_file).read_bytes() == data

    def test_output_dir_created_automatically(self, tmp_path, monkeypatch) -> None:
        _stub_urlopen(monkeypatch, _FakeResponse(b"data"))
        output = tmp_path / "not" / "exists"

        Downloader().download_track(_track(), "https://example.invalid/1", output)

        assert output.is_dir()


class TestRetryStrategy:
    """download_track 逐曲目重试：默认每首重试 2 次（共 3 次尝试）。"""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch) -> None:
        monkeypatch.setattr(downloader_module.time, "sleep", Mock())

    def test_success_first_attempt(self, tmp_path, monkeypatch) -> None:
        resp = _FakeResponse(b"ok")
        urlopen = _stub_urlopen(monkeypatch, resp)

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert Path(item.source_file).read_bytes() == b"ok"
        assert urlopen.call_count == 1

    def test_http_4xx_raises_without_retry(self, tmp_path, monkeypatch) -> None:
        urlopen = _stub_urlopen(monkeypatch, _http_error(404))

        with pytest.raises(RuntimeError, match="不重试"):
            Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert urlopen.call_count == 1

    def test_http_5xx_retries_then_raises(self, tmp_path, monkeypatch) -> None:
        urlopen = _stub_urlopen(monkeypatch, _http_error(500), _http_error(500), _http_error(500))

        with pytest.raises(RuntimeError, match="无法恢复"):
            Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert urlopen.call_count == 3

    def test_http_408_retries_then_raises(self, tmp_path, monkeypatch) -> None:
        """408/429 可重试，但最后一次尝试仍失败时抛「无法恢复」。"""
        urlopen = _stub_urlopen(monkeypatch, _http_error(408), _http_error(408), _http_error(408))

        with pytest.raises(RuntimeError, match="无法恢复"):
            Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert urlopen.call_count == 3

    @pytest.mark.parametrize(
        "error",
        [URLError(OSError("connection reset")), TimeoutError("timed out"), OSError("socket closed")],
    )
    def test_network_errors_retry_then_raise(self, tmp_path, monkeypatch, error) -> None:
        urlopen = _stub_urlopen(monkeypatch, error, error, error)

        with pytest.raises(RuntimeError, match="网络错误"):
            Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert urlopen.call_count == 3

    def test_transient_failure_then_success(self, tmp_path, monkeypatch) -> None:
        """首次网络错误、第二次成功 → 重试后返回响应。"""
        urlopen = _stub_urlopen(monkeypatch, URLError(OSError("reset")), _FakeResponse(b"recovered"))

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert Path(item.source_file).read_bytes() == b"recovered"
        assert urlopen.call_count == 2
        assert downloader_module.time.sleep.call_count == 1  # 退避 1.0s

    def test_mid_stream_failure_retries_then_success(self, tmp_path, monkeypatch) -> None:
        """中途读流失败 → 清理半截文件后重试成功（整个下载流程重试）。"""
        urlopen = _stub_urlopen(
            monkeypatch,
            _FakeResponse(b"partial", read_error=OSError("connection reset")),
            _FakeResponse(b"full-data"),
        )

        item = Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert Path(item.source_file).read_bytes() == b"full-data"
        assert urlopen.call_count == 2

    def test_mid_stream_failure_retries_then_raises(self, tmp_path, monkeypatch) -> None:
        """中途读流持续失败 → 重试耗尽后抛网络错误。"""
        urlopen = _stub_urlopen(
            monkeypatch,
            _FakeResponse(b"a", read_error=OSError("socket closed")),
            _FakeResponse(b"b", read_error=OSError("socket closed")),
            _FakeResponse(b"c", read_error=OSError("socket closed")),
        )

        with pytest.raises(RuntimeError, match="网络错误"):
            Downloader().download_track(_track(), "https://example.invalid/1", tmp_path)

        assert urlopen.call_count == 3

    def test_max_retries_zero_no_retry(self, tmp_path, monkeypatch) -> None:
        """max_retries=0 → 仅一次尝试，失败立即抛出。"""
        urlopen = _stub_urlopen(monkeypatch, URLError(OSError("reset")))

        with pytest.raises(RuntimeError, match="网络错误"):
            Downloader(max_retries=0).download_track(_track(), "https://example.invalid/1", tmp_path)

        assert urlopen.call_count == 1


class TestRetryBudget:
    """跨曲目连续重试熔断：任一成功清零，达上限后中止。"""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch) -> None:
        monkeypatch.setattr(downloader_module.time, "sleep", Mock())

    def test_retries_consume_budget_and_trip(self, tmp_path, monkeypatch) -> None:
        """单曲耗尽全部重试 → 熔断触发。"""
        budget = RetryBudget(limit=2)
        urlopen = _stub_urlopen(
            monkeypatch,
            URLError(OSError("reset")),
            URLError(OSError("reset")),
            URLError(OSError("reset")),
        )
        downloader = Downloader(max_retries=2, retry_budget=budget)

        with pytest.raises(RuntimeError, match="网络错误"):
            downloader.download_track(_track(), "https://example.invalid/1", tmp_path)

        assert budget.tripped is True
        assert urlopen.call_count == 3

    def test_success_resets_budget(self, tmp_path, monkeypatch) -> None:
        """一曲目重试后成功 → 清零连续计数，不触发熔断。"""
        budget = RetryBudget(limit=2)
        urlopen = _stub_urlopen(monkeypatch, URLError(OSError("reset")), _FakeResponse(b"ok"))
        downloader = Downloader(max_retries=2, retry_budget=budget)

        downloader.download_track(_track(), "https://example.invalid/1", tmp_path)

        assert budget.tripped is False
        assert urlopen.call_count == 2

    def test_cross_track_consecutive_retries_trip(self, tmp_path, monkeypatch) -> None:
        """跨曲目连续重试累计：预算 4，前两首各完全失败（各 2 次重试）后触发。"""
        budget = RetryBudget(limit=4)
        downloader = Downloader(max_retries=2, retry_budget=budget)
        urlopen = _stub_urlopen(
            monkeypatch,
            *([URLError(OSError("reset"))] * 6),
        )

        with pytest.raises(RuntimeError, match="网络错误"):
            downloader.download_track(_track(1), "https://example.invalid/1", tmp_path)
        assert budget.tripped is False  # 仅贡献 2 次重试，未达上限

        with pytest.raises(RuntimeError, match="网络错误"):
            downloader.download_track(_track(2), "https://example.invalid/2", tmp_path)
        assert budget.tripped is True  # 累计 4 次重试，熔断

        assert urlopen.call_count == 6

    def test_tripped_budget_aborts_without_download(self, tmp_path, monkeypatch) -> None:
        """预算已熔断 → 立即抛 RetryBudgetExceeded，不再发起请求。"""
        budget = RetryBudget(limit=2)
        budget.note_retry()
        budget.note_retry()
        urlopen = _stub_urlopen(monkeypatch, _FakeResponse(b"ok"))
        downloader = Downloader(max_retries=2, retry_budget=budget)

        with pytest.raises(RetryBudgetExceeded):
            downloader.download_track(_track(), "https://example.invalid/1", tmp_path)

        assert urlopen.call_count == 0

"""Downloader 补充单测：扩展名推断、分块写盘与重试策略。

覆盖：Content-Type / URL 路径扩展名推断（flac/ncm/m4a/默认 mp3）、is_ncm 标志、
分块下载、_open_with_retry 的成功与各类失败（4xx 不重试、5xx 重试、
408 重试耗尽、URLError/超时重试、瞬时失败后恢复）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from musicvault.adapters.processors import downloader as downloader_module
from musicvault.adapters.processors.downloader import Downloader
from musicvault.domain.models import Track


class _FakeResponse:
    """模拟 urllib 响应：headers 字典 + 支持分块读取的 read()。"""

    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self._data = data
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size < 0 or not self._data:
            chunk, self._data = self._data, b""
            return chunk
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


def _track(track_id: int = 1) -> Track:
    return Track(id=track_id, name="测试歌曲", artists=["歌手A"], album="专辑", raw={})


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://example.invalid/1", code, "err", {}, None)


def _stub_urlopen(monkeypatch, response: object) -> None:
    monkeypatch.setattr(downloader_module, "urlopen", Mock(return_value=response))


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


class TestOpenWithRetry:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch) -> None:
        monkeypatch.setattr(downloader_module.time, "sleep", Mock())

    def test_success_first_attempt(self, monkeypatch) -> None:
        resp = _FakeResponse(b"ok")
        monkeypatch.setattr(downloader_module, "urlopen", Mock(return_value=resp))

        result = Downloader._open_with_retry("https://example.invalid/1")

        assert result is resp

    def test_http_4xx_raises_without_retry(self, monkeypatch) -> None:
        urlopen = Mock(side_effect=_http_error(404))
        monkeypatch.setattr(downloader_module, "urlopen", urlopen)

        with pytest.raises(RuntimeError, match="不重试"):
            Downloader._open_with_retry("https://example.invalid/1")

        assert urlopen.call_count == 1

    def test_http_5xx_retries_then_raises(self, monkeypatch) -> None:
        urlopen = Mock(side_effect=_http_error(500))
        monkeypatch.setattr(downloader_module, "urlopen", urlopen)

        with pytest.raises(RuntimeError, match="无法恢复"):
            Downloader._open_with_retry("https://example.invalid/1")

        assert urlopen.call_count == 3

    def test_http_408_retries_then_raises_on_last_attempt(self, monkeypatch) -> None:
        """408/429 可重试，但最后一次尝试仍失败时抛「无法恢复」。"""
        urlopen = Mock(side_effect=_http_error(408))
        monkeypatch.setattr(downloader_module, "urlopen", urlopen)

        with pytest.raises(RuntimeError, match="无法恢复"):
            Downloader._open_with_retry("https://example.invalid/1")

        assert urlopen.call_count == 3

    @pytest.mark.parametrize(
        "error",
        [URLError(OSError("connection reset")), TimeoutError("timed out"), OSError("socket closed")],
    )
    def test_network_errors_retry_then_raise(self, monkeypatch, error) -> None:
        urlopen = Mock(side_effect=error)
        monkeypatch.setattr(downloader_module, "urlopen", urlopen)

        with pytest.raises(RuntimeError, match="网络错误"):
            Downloader._open_with_retry("https://example.invalid/1")

        assert urlopen.call_count == 3

    def test_transient_failure_then_success(self, monkeypatch) -> None:
        """首次网络错误、第二次成功 → 重试后返回响应。"""
        resp = _FakeResponse(b"recovered")
        urlopen = Mock(side_effect=[URLError(OSError("reset")), resp])
        monkeypatch.setattr(downloader_module, "urlopen", urlopen)

        result = Downloader._open_with_retry("https://example.invalid/1")

        assert result is resp
        assert downloader_module.time.sleep.call_count == 1  # 退避 1.0s

"""MetadataWriter 补充单测：封面下载/缩放/重试与 extra 字段提取的边界路径。

覆盖：_download_cover 缓存与失败、_resize_cover 缩放与模式转换、_fetch_cover
重试策略（HTTP 4xx/5xx/408、URLError、超时）、_extract_* 系列字段提取的非法
输入、_set_vorbis_text 删除分支、不支持后缀写入为 no-op、幂等重写。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
from PIL import Image

from musicvault.adapters.processors import metadata_writer as mw_module
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.domain.models import Track
from musicvault.preset_api.v1 import MetadataSpec


def _make_mp3(path: Path) -> None:
    """写入最小合法 MP3：4 个连续 MPEG1 Layer3 帧（mutagen 同步需要 ≥4 帧）。"""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame * 4)


def _make_flac(path: Path) -> None:
    """写入最小合法 FLAC：fLaC 标记 + 合法 STREAMINFO 块。"""
    import struct

    streaminfo = struct.pack(">HH", 4096, 4096)  # 最小/最大 block size
    streaminfo += b"\x00" * 6  # 最小/最大 frame size
    streaminfo += ((44100 << 44) | (2 << 41) | (16 << 36)).to_bytes(8, "big")  # 采样率/声道/位深
    streaminfo += b"\x00" * 16  # MD5
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)


def _make_image_bytes(size: tuple[int, int] = (200, 100), mode: str = "RGB") -> bytes:
    """用 PIL 生成内存图片字节（PNG 格式）。"""
    img = Image.new(mode, size, (255, 0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _track(**raw) -> Track:
    return Track(id=1, name="测试", artists=["歌手"], album="专辑", raw=raw)


class _FakeUrlopenResp:
    """模拟 urllib 响应：支持 with 上下文与 read()。"""

    def __init__(self, data: bytes = b"cover-bytes") -> None:
        self._data = data

    def __enter__(self) -> "_FakeUrlopenResp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


class TestDownloadCover:
    def test_no_url_returns_none(self) -> None:
        writer = MetadataWriter()

        assert writer._download_cover(None, 15, 0) is None

    def test_cached_cover_returned_without_refetch(self, monkeypatch) -> None:
        """同 URL 第二次调用命中实例级缓存，不再发起网络请求。"""
        writer = MetadataWriter()
        fetch = Mock(return_value=b"cover-data")
        monkeypatch.setattr(writer, "_fetch_cover", fetch)

        first = writer._download_cover("https://example.invalid/1.jpg", 15, 0)
        second = writer._download_cover("https://example.invalid/1.jpg", 15, 0)

        assert first == second == b"cover-data"
        fetch.assert_called_once()

    def test_fetch_failure_returns_none(self, monkeypatch) -> None:
        writer = MetadataWriter()
        monkeypatch.setattr(writer, "_fetch_cover", Mock(return_value=None))

        assert writer._download_cover("https://example.invalid/1.jpg", 15, 0) is None

    def test_max_size_triggers_resize(self, monkeypatch) -> None:
        """cover_max_size > 0 时对超限图片缩放后再缓存。"""
        writer = MetadataWriter()
        large = _make_image_bytes((200, 100))
        monkeypatch.setattr(writer, "_fetch_cover", Mock(return_value=large))

        result = writer._download_cover("https://example.invalid/big.jpg", 15, 100)

        resized = Image.open(BytesIO(result))
        assert resized.size == (100, 50)
        assert writer._cover_cache["https://example.invalid/big.jpg"] == result

    def test_max_size_zero_keeps_original(self, monkeypatch) -> None:
        writer = MetadataWriter()
        data = b"raw-cover"
        monkeypatch.setattr(writer, "_fetch_cover", Mock(return_value=data))

        assert writer._download_cover("https://example.invalid/1.jpg", 15, 0) == data


class TestResizeCover:
    def test_invalid_image_data_returned_as_is(self) -> None:
        writer = MetadataWriter()
        data = b"\xff\xd8 garbage-not-an-image"

        assert writer._resize_cover(data, 100) == data

    def test_small_image_returned_as_is(self) -> None:
        writer = MetadataWriter()
        data = _make_image_bytes((50, 30))

        assert writer._resize_cover(data, 100) == data

    def test_large_image_resized(self) -> None:
        writer = MetadataWriter()
        data = _make_image_bytes((400, 200))

        result = writer._resize_cover(data, 100)

        assert Image.open(BytesIO(result)).size == (100, 50)

    def test_rgba_image_converted_to_rgb(self) -> None:
        """非 RGB/L 模式（RGBA）缩放后转换为 RGB 再存 JPEG。"""
        writer = MetadataWriter()
        data = _make_image_bytes((300, 150), mode="RGBA")

        result = writer._resize_cover(data, 100)

        img = Image.open(BytesIO(result))
        assert img.mode == "RGB"
        assert img.size == (100, 50)


class TestFetchCover:
    def test_success_returns_body(self, monkeypatch) -> None:
        monkeypatch.setattr(mw_module, "urlopen", Mock(return_value=_FakeUrlopenResp(b"jpeg-data")))

        result = MetadataWriter()._fetch_cover("https://example.invalid/1.jpg", 15)

        assert result == b"jpeg-data"

    def test_http_4xx_stops_immediately(self, monkeypatch) -> None:
        """404 等 4xx（除 408/429）不重试直接放弃。"""
        urlopen = Mock(side_effect=HTTPError("https://example.invalid/1.jpg", 404, "Not Found", {}, None))
        monkeypatch.setattr(mw_module, "urlopen", urlopen)
        monkeypatch.setattr(mw_module.time, "sleep", Mock())

        assert MetadataWriter()._fetch_cover("https://example.invalid/1.jpg", 15) is None
        assert urlopen.call_count == 1

    def test_http_5xx_retries_three_times(self, monkeypatch) -> None:
        urlopen = Mock(side_effect=HTTPError("https://example.invalid/1.jpg", 500, "Server Error", {}, None))
        monkeypatch.setattr(mw_module, "urlopen", urlopen)
        sleep = Mock()
        monkeypatch.setattr(mw_module.time, "sleep", sleep)

        assert MetadataWriter()._fetch_cover("https://example.invalid/1.jpg", 15) is None
        assert urlopen.call_count == 3
        assert sleep.call_count == 2  # 0.5s 与 1.5s 各一次

    def test_http_408_retries_three_times(self, monkeypatch) -> None:
        """408/429 视为可重试错误，重试耗尽后放弃。"""
        urlopen = Mock(side_effect=HTTPError("https://example.invalid/1.jpg", 408, "Timeout", {}, None))
        monkeypatch.setattr(mw_module, "urlopen", urlopen)
        monkeypatch.setattr(mw_module.time, "sleep", Mock())

        assert MetadataWriter()._fetch_cover("https://example.invalid/1.jpg", 15) is None
        assert urlopen.call_count == 3

    @pytest.mark.parametrize(
        "error",
        [URLError(OSError("connection reset")), TimeoutError("timed out"), OSError("socket closed")],
    )
    def test_network_errors_retry_then_give_up(self, monkeypatch, error) -> None:
        urlopen = Mock(side_effect=error)
        monkeypatch.setattr(mw_module, "urlopen", urlopen)
        monkeypatch.setattr(mw_module.time, "sleep", Mock())

        assert MetadataWriter()._fetch_cover("https://example.invalid/1.jpg", 15) is None
        assert urlopen.call_count == 3

    def test_transient_failure_then_success(self, monkeypatch) -> None:
        """首次 URLError、第二次成功 → 返回最终数据。"""
        urlopen = Mock(side_effect=[URLError(OSError("reset")), _FakeUrlopenResp(b"recovered")])
        monkeypatch.setattr(mw_module, "urlopen", urlopen)
        monkeypatch.setattr(mw_module.time, "sleep", Mock())

        assert MetadataWriter()._fetch_cover("https://example.invalid/1.jpg", 15) == b"recovered"


class TestExtractYear:
    def test_ms_timestamp(self) -> None:
        assert MetadataWriter()._extract_year({"publishTime": 1_600_000_000_000}) == "2020"

    def test_seconds_timestamp(self) -> None:
        assert MetadataWriter()._extract_year({"publishTime": 1_600_000_000}) == "2020"

    def test_missing_timestamp_returns_none(self) -> None:
        assert MetadataWriter()._extract_year({}) is None

    def test_invalid_text_returns_none(self) -> None:
        assert MetadataWriter()._extract_year({"publishTime": "不是时间"}) is None

    def test_out_of_range_returns_none(self) -> None:
        """超大时间戳触发 fromtimestamp 的 OSError（平台 time_t 溢出）。"""
        assert MetadataWriter()._extract_year({"publishTime": 10**16}) is None


class TestExtractTrackNumber:
    def test_valid_int(self) -> None:
        assert MetadataWriter._extract_track_number(3) == "3"

    def test_zero_returns_none(self) -> None:
        assert MetadataWriter._extract_track_number(0) is None

    def test_invalid_value_returns_none(self) -> None:
        assert MetadataWriter._extract_track_number("第3首") is None

    def test_none_returns_none(self) -> None:
        assert MetadataWriter._extract_track_number(None) is None


class TestExtractDisc:
    def test_int_disc(self) -> None:
        assert MetadataWriter._extract_disc(2) == "2"

    def test_zero_disc_returns_none(self) -> None:
        assert MetadataWriter._extract_disc(0) is None

    def test_text_with_slash_takes_first_segment(self) -> None:
        assert MetadataWriter._extract_disc(" 2/10 ") == "2"

    def test_blank_text_returns_none(self) -> None:
        assert MetadataWriter._extract_disc("   ") is None

    def test_non_numeric_text_kept(self) -> None:
        assert MetadataWriter._extract_disc("侧标") == "侧标"


class TestExtractGenre:
    def test_stripped_string(self) -> None:
        assert MetadataWriter._extract_genre(" 摇滚 ") == "摇滚"

    def test_blank_string_returns_none(self) -> None:
        assert MetadataWriter._extract_genre("") is None

    def test_list_joined(self) -> None:
        assert MetadataWriter._extract_genre(["摇滚", "", "流行"]) == "摇滚/流行"

    def test_other_types_returns_none(self) -> None:
        assert MetadataWriter._extract_genre(42) is None


class TestExtractAlbumArtist:
    def test_names_joined(self) -> None:
        assert MetadataWriter._extract_album_artist({"ar": [{"name": "A"}, {"name": "B"}]}) == "A/B"

    def test_non_list_returns_none(self) -> None:
        assert MetadataWriter._extract_album_artist({}) is None

    def test_non_dict_items_skipped(self) -> None:
        """列表中混入非 dict 元素（如 ID）时跳过。"""
        assert MetadataWriter._extract_album_artist({"ar": [123, {"name": "A"}]}) == "A"

    def test_missing_name_skipped(self) -> None:
        assert MetadataWriter._extract_album_artist({"ar": [{"id": 1}, {"name": ""}]}) is None


class TestExtractNamedPeople:
    def test_string(self) -> None:
        assert MetadataWriter._extract_named_people(" 张三 ") == "张三"

    def test_blank_string_returns_none(self) -> None:
        assert MetadataWriter._extract_named_people("") is None

    def test_dict_with_name(self) -> None:
        assert MetadataWriter._extract_named_people({"name": "张三"}) == "张三"

    def test_dict_without_name_returns_none(self) -> None:
        assert MetadataWriter._extract_named_people({"id": 1}) is None

    def test_list_of_dicts_and_strings(self) -> None:
        assert MetadataWriter._extract_named_people([{"name": "张三"}, "李四", 42]) == "张三/李四"

    def test_list_with_empty_names_returns_none(self) -> None:
        assert MetadataWriter._extract_named_people([{"name": ""}, 42]) is None

    def test_other_types_returns_none(self) -> None:
        assert MetadataWriter._extract_named_people(42) is None


class TestExtractComment:
    def test_tns_used_first(self) -> None:
        assert MetadataWriter._extract_comment({"tns": ["别名A", "别名B"]}, _track()) == "别名A/别名B"

    def test_alia_used_when_no_tns(self) -> None:
        assert MetadataWriter._extract_comment({"alia": ["译名"]}, _track()) == "译名"

    def test_track_aliases_fallback(self) -> None:
        track = Track(id=1, name="测试", artists=["歌手"], album="专辑", aliases=["曾用名"], raw={})
        assert MetadataWriter._extract_comment({}, track) == "曾用名"

    def test_nothing_returns_none(self) -> None:
        assert MetadataWriter._extract_comment({}, _track()) is None


class TestWriteEdgeCases:
    def test_unsupported_suffix_is_noop(self, tmp_path, monkeypatch) -> None:
        """.wav 等未支持后缀不报错也不改动文件（无对应写入分支）。"""
        audio = tmp_path / "1.wav"
        audio.write_bytes(b"fake-wav")
        writer = MetadataWriter()
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=b"cover"))

        writer.write(audio, _track(), metadata=MetadataSpec.full())

        assert audio.read_bytes() == b"fake-wav"

    def test_mp3_rewrite_is_idempotent(self, tmp_path, monkeypatch) -> None:
        """同曲目重写两次标签不重复（tags.clear 幂等）。"""
        from mutagen.mp3 import MP3

        audio = tmp_path / "1.mp3"
        _make_mp3(audio)
        writer = MetadataWriter()
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=b"cover"))

        writer.write(audio, _track(), metadata=MetadataSpec.full())
        writer.write(audio, _track(), metadata=MetadataSpec.full())

        tags = MP3(str(audio)).tags
        assert tags is not None
        assert len(tags.getall("TIT2")) == 1
        assert len(tags.getall("APIC:Cover")) == 1

    def test_flac_comment_removed_when_field_dropped(self, tmp_path) -> None:
        """字段白名单变化后旧 key 被删除（_set_vorbis_text 的 del 分支）。"""
        from mutagen.flac import FLAC

        audio = tmp_path / "1.flac"
        _make_flac(audio)
        writer = MetadataWriter()
        track_with_raw = _track(tns=["别名"], publishTime=1_600_000_000_000)

        writer.write(audio, track_with_raw, metadata=MetadataSpec(embed_cover=False, fields=("comment",)))
        flac = FLAC(str(audio))
        assert flac["comment"] == ["别名"]

        writer.write(audio, track_with_raw, metadata=MetadataSpec(embed_cover=False, fields=("year",)))
        flac = FLAC(str(audio))
        assert "comment" not in flac
        assert flac["date"] == ["2020"]

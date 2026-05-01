from __future__ import annotations

import pytest
from musicvault.adapters.processors.lyrics import (
    build_lossless_lyrics,
    build_lossy_lyrics,
    _build_translation_map,
    _find_translation,
    _is_json_metadata_line,
    _merge_translation,
    _parse_yrc_line,
    _sanitize_lyrics_text,
    _normalize_lrc_timestamps,
    _normalize_time_tag,
    _is_same_text,
)

# fmt: off
SAMPLE_LRC = """[00:01.000]First line
[00:05.500]Second line
[00:10.000]Third line"""

SAMPLE_TLYRIC = """[00:01.000]第一行
[00:05.500]第二行
[00:10.000]第三行"""

SAMPLE_YRC = """[1000,4000](1000,500,0)First(1500,500,0)line
[5500,4000](5500,500,0)Second(6000,500,0)line
[10000,5000](10000,500,0)Third(10500,500,0)line"""
# fmt: on


# ---- build_lossless_lyrics -------------------------------------------------


class TestBuildLosslessLyrics:
    def test_lrc_only_no_translation(self) -> None:
        payload = {"lrc": SAMPLE_LRC}
        result = build_lossless_lyrics(payload, include_translation=False)
        assert result == _normalize_lrc_timestamps(SAMPLE_LRC)

    def test_lrc_with_translation(self) -> None:
        payload = {"lrc": SAMPLE_LRC, "tlyric": SAMPLE_TLYRIC}
        result = build_lossless_lyrics(payload, include_translation=True)
        # 翻译行插入在原行之间，逐行检查
        lines = result.splitlines()
        assert len(lines) == 6  # 3 原文 + 3 翻译
        assert "First line" in lines[0]
        assert "第一行" in lines[1]
        assert "Second line" in lines[2]
        assert "第二行" in lines[3]

    def test_yrc_no_translation(self) -> None:
        payload = {"yrc": SAMPLE_YRC}
        result = build_lossless_lyrics(payload, include_translation=False)
        # YRC 每词带独立时间戳，输出为 [00:01.000]First[00:01.500]line[00:05.000]
        assert "First" in result
        assert "line" in result
        assert "Second" in result
        assert "Third" in result
        # 无翻译行
        assert "第一行" not in result

    def test_yrc_with_ytlyric(self) -> None:
        ytlyric = """[1000,4000](1000,500,0)第一(1500,500,0)行
[5500,4000](5500,500,0)第二(6000,500,0)行"""
        payload = {"yrc": SAMPLE_YRC, "ytlyric": ytlyric, "tlyric": ""}
        result = build_lossless_lyrics(payload, include_translation=True)
        # ytlyric translation should appear after each yrc line
        assert "第一行" in result
        assert "第二行" in result

    def test_yrc_falls_back_to_tlyric(self) -> None:
        payload = {"yrc": SAMPLE_YRC, "tlyric": SAMPLE_TLYRIC}
        result = build_lossless_lyrics(payload, include_translation=True)
        assert "第一行" in result

    def test_empty_payload(self) -> None:
        result = build_lossless_lyrics({})
        assert result == ""

    def test_translation_disabled(self) -> None:
        ytlyric = """[1000,4000](1000,500,0)第一行"""
        payload = {"yrc": SAMPLE_YRC, "ytlyric": ytlyric, "tlyric": SAMPLE_TLYRIC}
        result = build_lossless_lyrics(payload, include_translation=False)
        assert "第一行" not in result

    def test_yrc_with_stray_non_yrc_lines(self) -> None:
        # YRC 中夹带非 YRC 的非空行应原样透传（覆盖 line 90-92）
        yrc_with_stray = SAMPLE_YRC + "\n[meta]some info"
        payload = {"yrc": yrc_with_stray}
        result = build_lossless_lyrics(payload, include_translation=False)
        assert "[meta]some info" in result


# ---- build_lossy_lyrics ----------------------------------------------------


class TestBuildLossyLyrics:
    def test_lrc_only_no_translation(self) -> None:
        payload = {"lrc": SAMPLE_LRC}
        result = build_lossy_lyrics(payload, include_translation=False)
        assert result == _normalize_lrc_timestamps(SAMPLE_LRC)

    def test_lrc_with_inline_translation(self) -> None:
        payload = {"lrc": SAMPLE_LRC, "tlyric": SAMPLE_TLYRIC}
        result = build_lossy_lyrics(payload, include_translation=True)
        # inline: [00:01.000]第一行 First line
        assert "第一行" in result
        assert "First line" in result

    def test_empty_payload(self) -> None:
        result = build_lossy_lyrics({})
        assert result == ""

    def test_translation_disabled(self) -> None:
        payload = {"lrc": SAMPLE_LRC, "tlyric": SAMPLE_TLYRIC}
        result = build_lossy_lyrics(payload, include_translation=False)
        assert "第一行" not in result


# ---- _build_translation_map -------------------------------------------------


class TestBuildTranslationMap:
    def test_basic_lrc(self) -> None:
        mapping = _build_translation_map("[00:01.000]Hello\n[00:05.500]World")
        assert mapping == {"00:01.000": "Hello", "00:05.500": "World"}

    def test_yrc_format(self) -> None:
        mapping = _build_translation_map("[1000,4000](1000,500,0)Hello(1500,500,0)World")
        assert mapping.get("00:01.000") == "HelloWorld"

    def test_empty_lines_skipped(self) -> None:
        mapping = _build_translation_map("\n\n[00:01.000]X\n\n")
        assert mapping == {"00:01.000": "X"}

    def test_no_timestamp_lines_skipped(self) -> None:
        mapping = _build_translation_map("plain text\n[ti:Title]")
        assert mapping == {}


# ---- _merge_translation ----------------------------------------------------


class TestMergeTranslation:
    def test_inline(self) -> None:
        result = _merge_translation(
            "[00:01.000]Hello", "[00:01.000]你好", inline=True
        )
        assert result == "[00:01.000]你好 Hello"

    def test_append_next_line(self) -> None:
        result = _merge_translation(
            "[00:01.000]Hello", "[00:01.000]你好", inline=False
        )
        assert "[00:01.000]Hello\n[00:01.000]你好" == result

    def test_skip_same_text(self) -> None:
        result = _merge_translation(
            "[00:01.000]Hello", "[00:01.000]Hello", inline=True
        )
        assert "[00:01.000]你好" not in result
        assert result == "[00:01.000]Hello"

    def test_empty_translation_map(self) -> None:
        result = _merge_translation("[00:01.000]X", "", inline=True)
        assert result == "[00:01.000]X"

    def test_metadata_lines_preserved(self) -> None:
        # 无时间戳的元数据行应原样透传（覆盖 line 59-60）
        result = _merge_translation(
            "[ti:Title]\n[00:01.000]Hello", "[00:01.000]你好", inline=False
        )
        lines = result.splitlines()
        assert "[ti:Title]" in lines[0]
        assert "Hello" in lines[1]


# ---- _parse_yrc_line -------------------------------------------------------


class TestParseYrcLine:
    def test_basic(self) -> None:
        parsed = _parse_yrc_line("[22200,3840](22200,400,0)你(22600,400,0)好")
        assert parsed is not None
        start_ms, duration_ms, words, plain = parsed
        assert start_ms == 22200
        assert duration_ms == 3840
        assert words == [(22200, "你"), (22600, "好")]
        assert plain == "你好"

    def test_empty_text_skipped_in_words(self) -> None:
        parsed = _parse_yrc_line("[0,1000](0,100,0)(100,100,0)AB")
        assert parsed is not None
        words = parsed[2]
        assert len(words) == 1
        assert words[0] == (100, "AB")

    def test_not_yrc_line(self) -> None:
        assert _parse_yrc_line("[00:01.000]plain lrc") is None
        assert _parse_yrc_line("") is None
        assert _parse_yrc_line("just text") is None


# ---- _sanitize_lyrics_text ------------------------------------------------


class TestSanitizeLyricsText:
    def test_json_metadata_removed(self) -> None:
        text = '{"t":16153,"c":[{"tx":"how"}]}\n[00:01.000]Real lyric'
        result = _sanitize_lyrics_text(text)
        assert "{" not in result
        assert "Real lyric" in result

    def test_no_json_lines_untouched(self) -> None:
        text = "[00:01.000]Line1\n[00:02.000]Line2"
        assert _sanitize_lyrics_text(text) == text

    def test_non_json_braces_kept(self) -> None:
        text = "[00:01.000]{not valid json"
        result = _sanitize_lyrics_text(text)
        assert "{not valid json" in result


# ---- _normalize_time_tag --------------------------------------------------


class TestNormalizeTimeTag:
    def test_standard(self) -> None:
        assert _normalize_time_tag("00:01.50") == "00:01.500"

    def test_colon_variant(self) -> None:
        assert _normalize_time_tag("00:01:50") == "00:01.500"

    def test_no_fraction(self) -> None:
        assert _normalize_time_tag("00:01") == "00:01.000"

    def test_pads_leading_zeros(self) -> None:
        assert _normalize_time_tag("0:1.5") == "00:01.500"

    def test_no_colon_returns_raw(self) -> None:
        assert _normalize_time_tag("notimetag") == "notimetag"


# ---- _normalize_lrc_timestamps --------------------------------------------


class TestNormalizeLrcTimestamps:
    def test_all_tags_normalized(self) -> None:
        result = _normalize_lrc_timestamps("[00:01.5]A\n[00:02:30]B")
        assert "[00:01.500]A" in result
        # 冒号变体：30 是百分位，即 0.300 秒
        assert "[00:02.300]B" in result

    def test_no_tags_unchanged(self) -> None:
        assert _normalize_lrc_timestamps("plain text") == "plain text"


# ---- _is_same_text ---------------------------------------------------------


class TestIsSameText:
    def test_same(self) -> None:
        assert _is_same_text("Hello", "Hello") is True
        assert _is_same_text("  Hello  ", "Hello") is True

    def test_different(self) -> None:
        assert _is_same_text("Hello", "World") is False


# ---- _find_translation -------------------------------------------------------


class TestFindTranslation:
    def test_found(self) -> None:
        tmap = {"00:01.000": "你好"}
        assert _find_translation(["00:01.000", "00:02.000"], tmap) == "你好"

    def test_not_found(self) -> None:
        tmap = {"00:03.000": "你好"}
        assert _find_translation(["00:01.000", "00:02.000"], tmap) == ""

    def test_empty_map(self) -> None:
        assert _find_translation(["00:01.000"], {}) == ""


# ---- _is_json_metadata_line --------------------------------------------------


class TestIsJsonMetadataLine:
    def test_valid_metadata(self) -> None:
        assert _is_json_metadata_line('{"t":16153,"c":[{"tx":"how"}]}') is True
        assert _is_json_metadata_line('{"c":[{"tx":"x"}],"t":0}') is True

    def test_invalid_json_in_braces(self) -> None:
        assert _is_json_metadata_line("{not valid json}") is False

    def test_json_object_without_c_field(self) -> None:
        assert _is_json_metadata_line('{"a":1}') is False

    def test_not_starting_with_brace(self) -> None:
        assert _is_json_metadata_line("[1,2,3]") is False

    def test_empty_string(self) -> None:
        assert _is_json_metadata_line("") is False

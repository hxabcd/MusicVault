from __future__ import annotations

from musicvault.adapters.processors.lyrics import (
    _build_translation_map,
    _is_json_metadata_line,
    _is_same_text,
    _normalize_time_tag,
    _parse_yrc_line,
    _sanitize_lyrics_text,
)


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


# ---- _is_same_text ---------------------------------------------------------


class TestIsSameText:
    def test_same(self) -> None:
        assert _is_same_text("Hello", "Hello") is True
        assert _is_same_text("  Hello  ", "Hello") is True

    def test_different(self) -> None:
        assert _is_same_text("Hello", "World") is False


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

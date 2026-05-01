from __future__ import annotations

import pytest
from musicvault.core.models import Track, DownloadedTrack


class TestTrackFromNcmPayload:
    def test_basic_fields(self) -> None:
        payload = {
            "id": 12345,
            "name": "Song Name",
            "ar": [{"name": "Artist A"}, {"name": "Artist B"}],
            "al": {"name": "Test Album", "picUrl": "https://example.com/cover.jpg"},
            "dt": 240000,
        }
        track = Track.from_ncm_payload(payload)
        assert track.id == 12345
        assert track.name == "Song Name"
        assert track.artists == ["Artist A", "Artist B"]
        assert track.album == "Test Album"
        assert track.cover_url == "https://example.com/cover.jpg"
        assert track.duration_ms == 240000

    def test_aliases_split(self) -> None:
        payload = {
            "id": 1,
            "name": "Original",
            "ar": [],
            "al": {"name": "Album"},
            "tns": ["English Name", "Another/Third"],
            "alia": ["Fourth、Fifth;Sixth；Seventh"],
        }
        track = Track.from_ncm_payload(payload)
        assert "English Name" in track.aliases
        assert "Another" in track.aliases
        assert "Third" in track.aliases
        assert "Fourth" in track.aliases
        assert "Fifth" in track.aliases
        assert "Sixth" in track.aliases
        assert "Seventh" in track.aliases

    def test_alias_becomes_empty_after_cleaning(self) -> None:
        # 别名仅含零宽字符时，清洗后为空字符串，应被跳过
        payload = {
            "id": 1,
            "name": "Test",
            "ar": [],
            "al": {"name": "A"},
            "tns": ["​‌"],
            "alia": ["RealAlias"],
        }
        track = Track.from_ncm_payload(payload)
        # 零宽字符的别名被跳过，只保留有效别名
        assert track.aliases == ["RealAlias"]

    def test_no_aliases(self) -> None:
        track = Track.from_ncm_payload(
            {"id": 1, "name": "X", "ar": [], "al": {"name": "A"}}
        )
        assert track.aliases == []

    def test_fallback_artist_and_album(self) -> None:
        payload = {"id": 1, "name": "X", "artists": [], "album": {"name": "Album"}}
        track = Track.from_ncm_payload(payload)
        assert track.artists == []
        assert track.album == "Album"

    def test_duration_ms_none(self) -> None:
        track = Track.from_ncm_payload(
            {"id": 1, "name": "X", "ar": [], "al": {"name": "A"}}
        )
        assert track.duration_ms is None

    def test_missing_name(self) -> None:
        track = Track.from_ncm_payload(
            {"id": 999, "ar": [], "al": {"name": "A"}}
        )
        assert track.name == "track_999"


class TestCleanMetadataText:
    def test_zero_width_chars_removed(self) -> None:
        result = Track._clean_metadata_text("hello​world‌!‍?⁠.﻿x­")
        assert result == "helloworld!?.x"

    def test_control_chars_excluded_except_whitespace(self) -> None:
        # \x00 \x01 直接删除（不是替换为空格），然后空白合并（\t→空格）
        result = Track._clean_metadata_text("a\x00b\x01c\nd\te")
        assert result == "abc\nd e"

    def test_multiple_spaces_compacted(self) -> None:
        result = Track._clean_metadata_text("hello     world  !")
        assert result == "hello world !"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        result = Track._clean_metadata_text("  hello  ")
        assert result == "hello"

    def test_private_use_area_chars_removed(self) -> None:
        result = Track._clean_metadata_text("abc￰￿xyz")
        assert result == "abcxyz"


class TestTrackProperties:
    def test_artist_text(self) -> None:
        track = Track(id=1, name="X", artists=["A", "B", "C"], album="Album")
        assert track.artist_text == "A/B/C"

    def test_artist_text_empty(self) -> None:
        track = Track(id=1, name="X", artists=[], album="Album")
        assert track.artist_text == "Unknown Artist"

    def test_alias_first(self) -> None:
        track = Track(id=1, name="X", artists=[], album="A", aliases=["Alias1", "Alias2"])
        assert track.alias == "Alias1"

    def test_alias_none(self) -> None:
        track = Track(id=1, name="X", artists=[], album="A")
        assert track.alias is None


class TestDownloadedTrack:
    def test_basic(self) -> None:
        track = Track(id=1, name="X", artists=[], album="A")
        dt = DownloadedTrack(track=track, source_file="x.mp3", is_ncm=True)
        assert dt.track.id == 1
        assert dt.source_file == "x.mp3"
        assert dt.is_ncm is True
        assert dt.playlist_ids == []

    def test_with_playlist_ids(self) -> None:
        track = Track(id=1, name="X", artists=[], album="A")
        dt = DownloadedTrack(
            track=track,
            source_file="x.mp3",
            is_ncm=False,
            playlist_ids=[100, 200],
        )
        assert dt.playlist_ids == [100, 200]

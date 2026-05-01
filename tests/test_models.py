from __future__ import annotations

import re

from musicvault.core.models import Track, DownloadedTrack
from musicvault.shared.utils import format_track_name


class TestTrackFromNcmPayload:
    def test_basic_fields(self) -> None:
        payload = {
            "id": 12345,
            "name": "Song Name",
            "ar": [{"name": "Artist A"}, {"name": "Artist B"}],
            "al": {"name": "Album X", "picUrl": "http://cover.jpg"},
            "dt": 240000,
        }
        track = Track.from_ncm_payload(payload)
        assert track.id == 12345
        assert track.name == "Song Name"
        assert track.artists == ["Artist A", "Artist B"]
        assert track.album == "Album X"
        assert track.cover_url == "http://cover.jpg"
        assert track.duration_ms == 240000

    def test_aliases_split(self) -> None:
        payload = {
            "id": 1,
            "name": "Song",
            "tns": ["Alias1/Alias2", "Alias3；Alias4"],
        }
        track = Track.from_ncm_payload(payload)
        assert "Alias1" in track.aliases
        assert "Alias2" in track.aliases
        assert "Alias3" in track.aliases
        assert "Alias4" in track.aliases

    def test_alias_becomes_empty_after_cleaning(self) -> None:
        payload = {"id": 1, "name": "Song", "alia": ["​"]}
        track = Track.from_ncm_payload(payload)
        assert track.aliases == []

    def test_no_aliases(self) -> None:
        payload = {"id": 1, "name": "Song"}
        track = Track.from_ncm_payload(payload)
        assert track.aliases == []

    def test_fallback_artist_and_album(self) -> None:
        payload = {"id": 1, "name": "Song"}
        track = Track.from_ncm_payload(payload)
        assert track.artists == []
        assert track.album == "Unknown Album"

    def test_duration_ms_none(self) -> None:
        payload = {"id": 1, "name": "Song"}
        track = Track.from_ncm_payload(payload)
        assert track.duration_ms is None

    def test_missing_name(self) -> None:
        payload = {"id": 99}
        track = Track.from_ncm_payload(payload)
        assert track.name == "track_99"

    def test_custom_alias_split_re(self) -> None:
        payload = {"id": 1, "name": "Song", "alia": ["A|B;C"]}
        track = Track.from_ncm_payload(payload, alias_split_re=re.compile(r"[|;]+"))
        assert set(track.aliases) == {"A", "B", "C"}


class TestCleanMetadataText:
    def test_zero_width_chars_removed(self) -> None:
        assert Track._clean_metadata_text("he​llo") == "hello"

    def test_control_chars_excluded_except_whitespace(self) -> None:
        assert Track._clean_metadata_text("ab\x00cd") == "abcd"

    def test_multiple_spaces_compacted(self) -> None:
        assert Track._clean_metadata_text("a    b") == "a b"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert Track._clean_metadata_text("  hello  ") == "hello"

    def test_private_use_area_chars_removed(self) -> None:
        assert Track._clean_metadata_text("a￰b") == "ab"


class TestTrackProperties:
    def test_artist_text(self) -> None:
        t = Track(id=1, name="S", artists=["A", "B"], album="X")
        assert t.artist_text == "A/B"

    def test_artist_text_empty(self) -> None:
        t = Track(id=1, name="S", artists=[], album="X")
        assert t.artist_text == "Unknown Artist"

    def test_alias_first(self) -> None:
        t = Track(id=1, name="S", artists=[], album="X", aliases=["A1", "A2"])
        assert t.alias == "A1"

    def test_alias_none(self) -> None:
        t = Track(id=1, name="S", artists=[], album="X")
        assert t.alias is None


class TestDownloadedTrack:
    def test_basic(self) -> None:
        t = Track(id=1, name="S", artists=[], album="X")
        dt = DownloadedTrack(track=t, source_file="x.mp3", is_ncm=False)
        assert dt.track is t
        assert dt.source_file == "x.mp3"
        assert not dt.is_ncm

    def test_with_playlist_ids(self) -> None:
        t = Track(id=1, name="S", artists=[], album="X")
        dt = DownloadedTrack(
            track=t,
            source_file="x.mp3",
            is_ncm=False,
            playlist_ids=[100, 200],
        )
        assert dt.playlist_ids == [100, 200]


class TestFormatTrackName:
    def _make_track(self, **kw):
        defaults = dict(id=1, name="Song", artists=["Artist"], album="Album")
        defaults.update(kw)
        return Track(**defaults)

    def test_basic_template(self) -> None:
        t = self._make_track()
        result = format_track_name("{artist} - {name}", t)
        assert result == "Artist - Song"

    def test_name_and_title_are_equivalent(self) -> None:
        t = self._make_track()
        assert format_track_name("{title}", t) == "Song"
        assert format_track_name("{name}", t) == "Song"

    def test_alias_placeholder(self) -> None:
        t = self._make_track(aliases=["Alias1", "Alias2"])
        assert format_track_name("{alias}", t) == "Alias1"

    def test_aliases_placeholder(self) -> None:
        t = self._make_track(aliases=["A1", "A2"])
        result = format_track_name("{aliases}", t)
        assert "A1" in result
        assert "A2" in result

    def test_prefix_with_alias(self) -> None:
        t = self._make_track(aliases=["AKA"])
        result = format_track_name("{prefix}{name} - {artist}", t, include_alias_prefix=True)
        assert result == "AKA Song - Artist"

    def test_prefix_without_alias(self) -> None:
        t = self._make_track()
        result = format_track_name("{prefix}{name} - {artist}", t, include_alias_prefix=True)
        assert result == "Song - Artist"

    def test_prefix_disabled(self) -> None:
        t = self._make_track(aliases=["AKA"])
        result = format_track_name("{prefix}{name} - {artist}", t, include_alias_prefix=False)
        assert result == "Song - Artist"

    def test_album_placeholder(self) -> None:
        t = self._make_track(album="Test Album")
        assert format_track_name("{album}", t) == "Test Album"

    def test_track_id_placeholder(self) -> None:
        t = self._make_track(id=42)
        assert format_track_name("{track_id}", t) == "42"

    def test_unknown_placeholder_kept(self) -> None:
        t = self._make_track()
        assert format_track_name("{unknown}", t) == "{unknown}"

    def test_invalid_chars_replaced(self) -> None:
        t = self._make_track(name="Song: Bad?", artists=["Artist <X>"])
        result = format_track_name("{artist} - {name}", t)
        assert ":" not in result
        assert "?" not in result
        assert "<" not in result

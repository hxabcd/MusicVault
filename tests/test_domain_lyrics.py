import pytest
from musicvault.domain.lyrics import LyricLine, LyricWord, lyrics_from_json, lyrics_to_json


def test_roundtrip_preserves_all_fields():
    lines = (
        LyricLine(
            start_ms=1000,
            duration_ms=3000,
            text="hello",
            words=(LyricWord(1000, "he"), LyricWord(1500, "llo")),
            translation="你好",
            romaji="haro",
        ),
    )
    assert lyrics_from_json(lyrics_to_json(lines)) == lines


def test_empty_lines_roundtrip():
    assert lyrics_from_json(lyrics_to_json(())) == ()


def test_from_json_tolerates_unknown_and_missing_fields():
    payload = '[{"start_ms":1,"duration_ms":0,"text":"x","words":[],"translation":"","romaji":"","unknown":1}]'
    assert lyrics_from_json(payload) == (LyricLine(1, 0, "x"),)


def test_from_json_rejects_malformed():
    with pytest.raises(ValueError):
        lyrics_from_json("not json")

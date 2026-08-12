from musicvault.adapters.processors.lyrics import convert_lyrics_payload
from musicvault.domain.lyrics import LyricLine, LyricWord


def test_yrc_conversion_with_words_and_translation():
    payload = {
        "yrc": "[1000,3000](1000,300,0)你(1500,400,0)好",
        "ytlrc": "[00:01.000]你好翻译",
        "yromalrc": "",
    }
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 1
    line = lines[0]
    assert line.start_ms == 1000
    assert line.duration_ms == 3000
    assert line.text == "你好"
    assert line.words == (LyricWord(1000, "你"), LyricWord(1500, "好"))
    assert line.translation == "你好翻译"
    assert line.romaji == ""


def test_standard_lrc_conversion():
    payload = {"lrc": "[00:01.000]hello\n[00:02.000]world", "tlyric": "[00:01.000]你好", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 2
    assert lines[0] == LyricLine(1000, 0, "hello", translation="你好")
    assert lines[1] == LyricLine(2000, 0, "world")


def test_repeated_timestamp_lines_split_into_rows():
    payload = {"lrc": "[00:01.000][01:31.000]hello", "tlyric": "", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert [line.start_ms for line in lines] == [1000, 91000]
    assert all(line.text == "hello" for line in lines)


def test_empty_payload_returns_empty():
    assert convert_lyrics_payload({}) == ()
    assert convert_lyrics_payload({"lrc": "", "yrc": "", "tlyric": "", "romalrc": ""}) == ()


def test_yrc_fuzzy_translation_alignment():
    payload = {
        "yrc": "[2000,2000](2000,300,0)早(2500,300,0)安",
        "ytlrc": "[00:01.900]おはよう",
        "yromalrc": "",
    }
    lines = convert_lyrics_payload(payload)
    assert lines[0].translation == "おはよう"


def test_metadata_json_lines_are_cleaned():
    payload = {"lrc": '{"t":1000,"c":[{"tx":"x"}]}\n[00:01.000]hello', "tlyric": "", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 1
    assert lines[0].text == "hello"

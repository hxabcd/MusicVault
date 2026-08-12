from musicvault.domain.lyrics import LyricLine, LyricWord
from musicvault.preset_api.render import enhanced_lrc, plain_text, standard_lrc


def test_standard_lrc_plain():
    lines = (LyricLine(1000, 0, "hello"), LyricLine(2000, 0, "world"))
    assert standard_lrc(lines) == "[00:01.000]hello\n[00:02.000]world"


def test_standard_lrc_with_translation_and_romaji():
    lines = (LyricLine(1000, 0, "hello", translation="你好", romaji="haro"),)
    assert standard_lrc(lines, include_translation=True, include_romaji=True) == (
        "[00:01.000]hello\n[00:01.000]你好\n[00:01.000]haro"
    )


def test_enhanced_lrc_with_words():
    lines = (LyricLine(1000, 3000, "hello", words=(LyricWord(1000, "he"), LyricWord(1500, "llo"))),)
    assert enhanced_lrc(lines) == "[00:01.000]he[00:01.500]llo[00:04.000]"


def test_enhanced_lrc_falls_back_without_words():
    lines = (LyricLine(1000, 0, "hello"),)
    assert enhanced_lrc(lines) == "[00:01.000]hello"


def test_enhanced_lrc_with_translation():
    lines = (LyricLine(1000, 3000, "你好", words=(LyricWord(1000, "你"), LyricWord(1200, "好")), translation="hello"),)
    assert enhanced_lrc(lines, include_translation=True) == ("[00:01.000]你[00:01.200]好[00:04.000]\n[00:01.000]hello")


def test_plain_text():
    lines = (LyricLine(1000, 0, "hello", translation="你好"), LyricLine(2000, 0, "world"))
    assert plain_text(lines) == "hello\nworld"


def test_empty_lines_produce_empty_string():
    assert standard_lrc(()) == ""
    assert enhanced_lrc(()) == ""
    assert plain_text(()) == ""

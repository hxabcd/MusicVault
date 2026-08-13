from musicvault.domain.lyrics import LyricLine, LyricWord
from musicvault.preset_api.render import enhanced_lrc_line, plain_text_line, standard_lrc_line


def test_standard_lrc_line_plain():
    line = LyricLine(1000, 0, "hello")
    assert standard_lrc_line(line) == "[00:01.000]hello"


def test_standard_lrc_line_with_translation_and_romaji():
    line = LyricLine(1000, 0, "hello", translation="你好", romaji="haro")
    assert standard_lrc_line(line, include_translation=True, include_romaji=True) == (
        "[00:01.000]hello\n[00:01.000]你好\n[00:01.000]haro"
    )


def test_enhanced_lrc_line_with_words():
    line = LyricLine(1000, 3000, "hello", words=(LyricWord(1000, "he"), LyricWord(1500, "llo")))
    assert enhanced_lrc_line(line) == "[00:01.000]he[00:01.500]llo[00:04.000]"


def test_enhanced_lrc_line_falls_back_without_words():
    line = LyricLine(1000, 0, "hello")
    assert enhanced_lrc_line(line) == "[00:01.000]hello"


def test_enhanced_lrc_line_with_translation():
    line = LyricLine(1000, 3000, "你好", words=(LyricWord(1000, "你"), LyricWord(1200, "好")), translation="hello")
    assert enhanced_lrc_line(line, include_translation=True) == (
        "[00:01.000]你[00:01.200]好[00:04.000]\n[00:01.000]hello"
    )


def test_plain_text_line():
    line = LyricLine(1000, 0, "hello", translation="你好")
    assert plain_text_line(line) == "hello"

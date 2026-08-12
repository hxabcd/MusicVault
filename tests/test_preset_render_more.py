"""preset_api render 补充单测：增强歌词的罗马音分支。

覆盖：enhanced_lrc 在 include_romaji 且行带 romaji 时的输出。
"""

from __future__ import annotations

from musicvault.domain.lyrics import LyricLine, LyricWord
from musicvault.preset_api.render import enhanced_lrc


def test_enhanced_lrc_with_romaji() -> None:
    """带逐字词与罗马音的行：逐字段 + 结束时间戳 + 翻译/罗马音行。"""
    lines = (
        LyricLine(
            start_ms=1000,
            duration_ms=3000,
            text="こんにちは",
            words=(LyricWord(1000, "こん"),),
            translation="你好",
            romaji="konnichiwa",
        ),
    )
    result = enhanced_lrc(lines, include_translation=True, include_romaji=True)
    assert result == "[00:01.000]こん[00:04.000]\n[00:01.000]你好\n[00:01.000]konnichiwa"


def test_enhanced_lrc_skips_empty_translation_and_romaji() -> None:
    """translation/romaji 为空串时不输出对应行。"""
    lines = (LyricLine(1000, 0, "hello"),)
    assert enhanced_lrc(lines, include_translation=True, include_romaji=True) == "[00:01.000]hello"

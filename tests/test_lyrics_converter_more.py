"""歌词转换边界补测（收尾阶段）。

覆盖：非法时间标签返回 None、翻译表含非法时间戳跳过、YRC/LRC 无效行跳过、
译文/罗马音与原文同文时清空。
"""

from __future__ import annotations

from musicvault.adapters.processors.lyrics import (
    _find_translation_fuzzy,
    _time_tag_to_ms,
    convert_lyrics_payload,
)


def test_time_tag_to_ms_rejects_malformed_tags() -> None:
    assert _time_tag_to_ms("00:01") is None  # 缺毫秒段
    assert _time_tag_to_ms("abc") is None
    assert _time_tag_to_ms("[00:01.000]") is None  # 带括号不匹配


def test_find_translation_fuzzy_skips_invalid_keys() -> None:
    # 分钟超 2 位的 key 无法解析为毫秒，应跳过而不是崩溃
    assert _find_translation_fuzzy(1000, {"999:99.999": "x"}) is None
    assert _find_translation_fuzzy(1000, {"00:01.000": "匹配"}) == "匹配"


def test_yrc_invalid_lines_are_skipped() -> None:
    payload = {"yrc": "不是 YRC 行\n[2000,2000](2000,300,0)早", "ytlrc": "", "yromalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 1
    assert lines[0].text == "早"


def test_yrc_same_text_translation_is_cleared() -> None:
    payload = {"yrc": "[2000,2000](2000,300,0)你好", "ytlrc": "[00:02.000]你好", "yromalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert lines[0].translation == ""


def test_yrc_same_text_romaji_is_cleared() -> None:
    payload = {"yrc": "[2000,2000](2000,300,0)你好", "ytlrc": "", "yromalrc": "[00:02.000]你好"}
    lines = convert_lyrics_payload(payload)
    assert lines[0].romaji == ""


def test_lrc_plain_lines_are_skipped() -> None:
    payload = {"lrc": "纯文本行无时间戳\n[00:01.000]hello", "tlyric": "", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 1
    assert lines[0].text == "hello"


def test_lrc_same_text_translation_is_cleared() -> None:
    payload = {"lrc": "[00:01.000]hello", "tlyric": "[00:01.000]hello", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert lines[0].translation == ""


def test_lrc_same_text_romaji_is_cleared() -> None:
    payload = {"lrc": "[00:01.000]hello", "tlyric": "", "romalrc": "[00:01.000]hello"}
    lines = convert_lyrics_payload(payload)
    assert lines[0].romaji == ""

from __future__ import annotations

import json
import re

from musicvault.domain.lyrics import LyricLine, LyricWord

# 标准/变体 LRC 时间标签，如 [00:22.200]、[00:22.20]、[00:22:20]
_TIME_TAG_RE = re.compile(r"\[(\d{1,2}:\d{2}(?:(?:[.:])\d{1,3})?)\]")
# 网易云 YRC 行头，如 [22200,3840]
_YRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
# YRC 逐词时间块，如 (22200,30,0)
_YRC_WORD_RE = re.compile(r"\(\d+,\d+,\d+\)")
# 拆出 YRC 中每个逐词片段的起始时间和文本
_YRC_WORD_TOKEN_RE = re.compile(r"\((\d+),(\d+),\d+\)([^()]*)")


def _build_translation_map(translated_lrc: str) -> dict[str, str]:
    # 构建"时间戳 -> 译文"索引
    mapping: dict[str, str] = {}
    for line in translated_lrc.splitlines():
        timestamps, lyric = _parse_line(line)
        if not timestamps or not lyric:
            continue
        for ts in timestamps:
            mapping[ts] = lyric
    return mapping


def _parse_line(line: str) -> tuple[list[str], str]:
    timestamps = [_normalize_time_tag(raw) for raw in _TIME_TAG_RE.findall(line)]
    if timestamps:
        lyric = _TIME_TAG_RE.sub("", line).strip()
        return timestamps, lyric

    # 兼容 YRC 行： [start,duration](wordStart,wordDur,...)字...
    match = _YRC_LINE_RE.match(line.strip())
    if not match:
        return [], ""
    start_ms = int(match.group(1))
    content = match.group(3)
    lyric = _YRC_WORD_RE.sub("", content).strip()
    return [_ms_to_time_tag(start_ms)], lyric


def _parse_yrc_line(line: str) -> tuple[int, int, list[tuple[int, str]], str] | None:
    # 返回：行起始时间、行时长、逐词(起始时间, 文本)、去时间后的整句文本。
    match = _YRC_LINE_RE.match(line.strip())
    if not match:
        return None
    start_ms = int(match.group(1))
    duration_ms = int(match.group(2))
    content = match.group(3)

    words: list[tuple[int, str]] = []
    for token in _YRC_WORD_TOKEN_RE.finditer(content):
        word_start_ms = int(token.group(1))
        text = token.group(3)
        if text:
            words.append((word_start_ms, text))
    plain_lyric = _YRC_WORD_RE.sub("", content).strip()
    return start_ms, duration_ms, words, plain_lyric


def _time_tag_to_ms(ts: str) -> int | None:
    match = re.match(r"(\d{1,2}):(\d{2})\.(\d{1,3})$", ts)
    if not match:
        return None
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    frac = match.group(3).ljust(3, "0")[:3]
    return minutes * 60000 + seconds * 1000 + int(frac)


def _find_translation_fuzzy(
    start_ms: int,
    translation_map: dict[str, str],
    tolerance_ms: int = 500,
) -> str | None:
    best_diff = tolerance_ms + 1
    best_text = None
    for ts_str, text in translation_map.items():
        ts_ms = _time_tag_to_ms(ts_str)
        if ts_ms is None:
            continue
        diff = abs(ts_ms - start_ms)
        if diff < best_diff:
            best_diff = diff
            best_text = text
    return best_text


def _is_same_text(base: str, translated: str) -> bool:
    return base.strip() == translated.strip()


def _ms_to_time_tag(ms: int) -> str:
    minutes = ms // 60000
    seconds = (ms % 60000) / 1000
    return f"{minutes:02d}:{seconds:06.3f}"


def _normalize_time_tag(raw: str) -> str:
    # 统一时间标签到 mm:ss.xxx，兼容 mm:ss:xx 这种网易云变体。
    if ":" not in raw:
        return raw
    parts = raw.split(":")
    if len(parts) < 2:  # pragma: no cover — ":" in raw 保证 split 至少 2 段
        return raw
    minutes = parts[0]
    seconds = parts[1]

    frac = ""
    if len(parts) == 3:
        frac = parts[2]
    elif "." in seconds:
        seconds, frac = seconds.split(".", 1)

    if not frac:
        return f"{int(minutes):02d}:{int(seconds):02d}.000"
    frac = frac[:3].ljust(3, "0")
    return f"{int(minutes):02d}:{int(seconds):02d}.{frac}"


def _sanitize_lyrics_text(text: str) -> str:
    # 去掉网易云返回中的 JSON 元信息行，避免污染最终歌词文件。
    sanitized_lines: list[str] = []
    for line in text.splitlines():
        if _is_json_metadata_line(line):
            continue
        sanitized_lines.append(line)
    return "\n".join(sanitized_lines)


def _is_json_metadata_line(line: str) -> bool:
    raw = line.strip()
    if not raw.startswith("{") or not raw.endswith("}"):
        return False
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):  # pragma: no cover — {…} JSON 必为 dict
        return False
    # 网易云逐词元数据行常见结构：{"t":...,"c":[{"tx":"..."}]}
    return "c" in obj and ("t" in obj or "tx" in obj)


def convert_lyrics_payload(payload: dict[str, str]) -> tuple[LyricLine, ...]:
    """将网易云原始歌词 payload 转换为统一结构化行列表。"""
    yrc = _sanitize_lyrics_text(payload.get("yrc") or "")
    if yrc:
        return _convert_yrc_lines(yrc, payload)
    lrc = _sanitize_lyrics_text(payload.get("lrc") or "")
    if not lrc:
        return ()
    return _convert_lrc_lines(lrc, payload)


def _convert_yrc_lines(yrc: str, payload: dict[str, str]) -> tuple[LyricLine, ...]:
    trans_map = _build_translation_map(payload.get("ytlrc") or "")
    romaji_map = _build_translation_map(payload.get("yromalrc") or "")
    lines: list[LyricLine] = []
    for raw_line in yrc.splitlines():
        parsed = _parse_yrc_line(raw_line)
        if not parsed:
            continue
        start_ms, duration_ms, words, text = parsed
        translation = _find_translation_fuzzy(start_ms, trans_map, tolerance_ms=200) or ""
        romaji = _find_translation_fuzzy(start_ms, romaji_map, tolerance_ms=200) or ""
        if _is_same_text(text, translation):
            translation = ""
        if _is_same_text(text, romaji):
            romaji = ""
        lines.append(
            LyricLine(
                start_ms=start_ms,
                duration_ms=duration_ms,
                text=text,
                words=tuple(LyricWord(start_ms=w_start, text=w_text) for w_start, w_text in words),
                translation=translation,
                romaji=romaji,
            )
        )
    return tuple(lines)


def _convert_lrc_lines(lrc: str, payload: dict[str, str]) -> tuple[LyricLine, ...]:
    trans_map = _build_translation_map(payload.get("tlyric") or "")
    romaji_map = _build_translation_map(payload.get("romalrc") or "")
    lines: list[LyricLine] = []
    for raw_line in lrc.splitlines():
        timestamps, text = _parse_line(raw_line)
        if not timestamps or not text:
            continue
        for raw_ts in timestamps:
            start_ms = _time_tag_to_ms(raw_ts)
            if start_ms is None:
                continue
            tag = _ms_to_time_tag(start_ms)
            translation = trans_map.get(tag) or _find_translation_fuzzy(start_ms, trans_map, tolerance_ms=200) or ""
            romaji = romaji_map.get(tag) or _find_translation_fuzzy(start_ms, romaji_map, tolerance_ms=200) or ""
            if _is_same_text(text, translation):
                translation = ""
            if _is_same_text(text, romaji):
                romaji = ""
            lines.append(LyricLine(start_ms=start_ms, duration_ms=0, text=text, translation=translation, romaji=romaji))
    return tuple(lines)

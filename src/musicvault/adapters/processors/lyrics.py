from __future__ import annotations

import json
import logging
import re
from pathlib import Path

# 标准/变体 LRC 时间标签，如 [00:22.200]、[00:22.20]、[00:22:20]
_TIME_TAG_RE = re.compile(r"\[(\d{1,2}:\d{2}(?:(?:[.:])\d{1,3})?)\]")
# 网易云 YRC 行头，如 [22200,3840]
_YRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
# YRC 逐词时间块，如 (22200,30,0)
_YRC_WORD_RE = re.compile(r"\(\d+,\d+,\d+\)")
# 拆出 YRC 中每个逐词片段的起始时间和文本
_YRC_WORD_TOKEN_RE = re.compile(r"\((\d+),(\d+),\d+\)([^()]*)")

logger = logging.getLogger(__name__)


def build_lossless_lyrics(
    payload: dict[str, str],
    include_translation: bool = True,
    translation_format: str = "separate",
) -> str:
    """生成无损歌词：原文行后追加同时间轴翻译行。"""
    yrc = _sanitize_lyrics_text(payload.get("yrc") or "")
    translated = _normalize_lrc_timestamps(_sanitize_lyrics_text(payload.get("tlyric") or ""))
    ytranslated = _sanitize_lyrics_text(payload.get("ytlyric") or "")
    if yrc:
        return _build_lossless_from_yrc(yrc, translated, include_translation, ytranslated, translation_format)

    base = _normalize_lrc_timestamps(_sanitize_lyrics_text(payload.get("lrc") or ""))
    if not base:
        return ""
    if not include_translation:
        return base
    return _merge_translation(base, translated, inline=(translation_format == "inline"))


def build_lossy_lyrics(
    payload: dict[str, str],
    include_translation: bool = True,
    translation_format: str = "inline",
) -> str:
    """生成有损歌词：同一行前置翻译，再接原文。"""
    base = _normalize_lrc_timestamps(_sanitize_lyrics_text(payload.get("lrc") or ""))
    if not base:
        return ""
    if not include_translation:
        return base
    translated = _normalize_lrc_timestamps(_sanitize_lyrics_text(payload.get("tlyric") or ""))
    return _merge_translation(base, translated, inline=(translation_format == "inline"))


def _merge_translation(base_lrc: str, translated_lrc: str, inline: bool) -> str:
    # 1. 把翻译歌词预处理成"时间戳 -> 译文"映射。
    translation_map = _build_translation_map(translated_lrc)
    if not translation_map:
        return base_lrc

    merged: list[str] = []
    # 2. 逐行扫描原歌词，仅对带时间戳的行尝试拼接翻译。
    for line in base_lrc.splitlines():
        timestamps, lyric = _parse_line(line)
        if not timestamps:
            merged.append(line)
            continue

        translated = _find_translation(timestamps, translation_map)
        if not translated or _is_same_text(lyric, translated):
            merged.append(line)
            continue

        # 3. 按目标格式输出（lossless 追加下一行 / lossy 同行前置翻译）。
        merged.append(line)
        if inline:
            prefix = "".join(f"[{ts}]" for ts in timestamps)
            merged[-1] = f"{prefix}{translated} {lyric}".rstrip()
        else:
            prefix = "".join(f"[{ts}]" for ts in timestamps)
            merged.append(f"{prefix}{translated}")
    return "\n".join(merged)


def _build_lossless_from_yrc(
    yrc_text: str,
    translated_lrc: str,
    include_translation: bool,
    ytranslated_lrc: str = "",
    translation_format: str = "separate",
) -> str:
    translation_map = _build_translation_map(ytranslated_lrc)
    if translated_lrc:
        lrc_map = _build_translation_map(translated_lrc)
        for ts, lyric in lrc_map.items():
            if ts not in translation_map:
                translation_map[ts] = lyric
    lines: list[str] = []
    for raw_line in yrc_text.splitlines():
        parsed = _parse_yrc_line(raw_line)
        if not parsed:
            if raw_line.strip():
                lines.append(raw_line)
            continue
        start_ms, duration_ms, words, plain_lyric = parsed
        end_ms = start_ms + duration_ms

        if not include_translation:
            lines.append(_render_yrc_enhanced_line(words, end_ms))
            continue

        start_tag = _ms_to_time_tag(start_ms)
        translated = translation_map.get(start_tag)
        if translated and not _is_same_text(plain_lyric, translated):
            end_tag = _ms_to_time_tag(end_ms)
            if translation_format == "inline":
                rendered = _render_yrc_enhanced_line(words, end_ms, translated)
                lines.append(rendered)
            else:
                lines.append(_render_yrc_enhanced_line(words, end_ms))
                lines.append(f"[{start_tag}]{translated}[{end_tag}]")
        else:
            lines.append(_render_yrc_enhanced_line(words, end_ms))
    return "\n".join(lines)


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


def _render_yrc_enhanced_line(words: list[tuple[int, str]], end_ms: int, translation: str = "") -> str:
    out = "".join(f"[{_ms_to_time_tag(start_ms)}]{text}" for start_ms, text in words)
    if translation:
        first_tag_end = out.index("]", 1) if "]" in out[1:] else len(out)
        out = f"{out[: first_tag_end + 1]}{translation} {out[first_tag_end + 1 :]}"
    return f"{out}[{_ms_to_time_tag(end_ms)}]"


def _find_translation(timestamps: list[str], translation_map: dict[str, str]) -> str:
    for ts in timestamps:
        translated = translation_map.get(ts)
        if translated:
            return translated
    return ""


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


def _normalize_lrc_timestamps(text: str) -> str:
    # 将文本中所有 LRC 时间标签统一为 mm:ss.xxx，保证输出稳定一致。
    def repl(match: re.Match[str]) -> str:
        return f"[{_normalize_time_tag(match.group(1))}]"

    return _TIME_TAG_RE.sub(repl, text)


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


def write_gb18030_lrc(
    target_audio: Path,
    lyric_text: str,
    encodings: tuple[str, ...] = ("gb18030", "utf-8-sig"),
) -> Path:
    """为目标音频写入 `.lrc` 文件，按配置编码顺序尝试写入。"""
    lrc_path = target_audio.with_suffix(".lrc")
    content = lyric_text or ""

    # 避免 `errors=ignore` 导致静默丢字：
    # 1) 按配置顺序尝试编码（默认 GB18030 -> UTF-8 with BOM）
    # 2) 全部失败时保底 UTF-8 replace，避免流程中断。
    fallback_encodings = tuple(encoding for encoding in encodings if str(encoding).strip())
    if not fallback_encodings:
        fallback_encodings = ("gb18030", "utf-8-sig")
    first_encoding = fallback_encodings[0]
    for encoding in fallback_encodings:
        try:
            lrc_path.write_bytes(content.encode(encoding))
            if encoding != first_encoding:
                logger.warning("歌词编码已按回退顺序切换：%s，文件=%s", encoding, lrc_path.name)
            return lrc_path
        except UnicodeEncodeError:
            continue

    # 理论上不会走到这里；保底避免写文件失败。
    lrc_path.write_bytes(
        content.encode("utf-8", errors="replace")
    )  # pragma: no cover — 回退列表含 utf-8-sig，前面必成功
    logger.warning("歌词编码回退到 utf-8(replace)：%s", lrc_path.name)  # pragma: no cover
    return lrc_path  # pragma: no cover

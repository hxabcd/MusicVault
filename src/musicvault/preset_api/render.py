from __future__ import annotations

from musicvault.domain.lyrics import LyricLine


def ms_to_tag(ms: int) -> str:
    minutes = ms // 60000
    seconds = (ms % 60000) / 1000
    return f"{minutes:02d}:{seconds:06.3f}"


def standard_lrc_line(
    line: LyricLine,
    *,
    include_translation: bool = False,
    include_romaji: bool = False,
) -> str:
    tag = f"[{ms_to_tag(line.start_ms)}]"
    parts = [f"{tag}{line.text}"]
    if include_translation and line.translation:
        parts.append(f"{tag}{line.translation}")
    if include_romaji and line.romaji:
        parts.append(f"{tag}{line.romaji}")
    return "\n".join(parts)


def enhanced_lrc_line(
    line: LyricLine,
    *,
    include_translation: bool = False,
    include_romaji: bool = False,
) -> str:
    parts: list[str] = []
    if line.words:
        out = "".join(f"[{ms_to_tag(w.start_ms)}]{w.text}" for w in line.words)
        if line.duration_ms > 0:
            out += f"[{ms_to_tag(line.start_ms + line.duration_ms)}]"
        parts.append(out)
    else:
        parts.append(f"[{ms_to_tag(line.start_ms)}]{line.text}")
    tag = f"[{ms_to_tag(line.start_ms)}]"
    if include_translation and line.translation:
        parts.append(f"{tag}{line.translation}")
    if include_romaji and line.romaji:
        parts.append(f"{tag}{line.romaji}")
    return "\n".join(parts)


def plain_text_line(line: LyricLine) -> str:
    return line.text

from __future__ import annotations

from musicvault.domain.lyrics import LyricLine


def _ms_to_tag(ms: int) -> str:
    minutes = ms // 60000
    seconds = (ms % 60000) / 1000
    return f"{minutes:02d}:{seconds:06.3f}"


def standard_lrc(
    lines: tuple[LyricLine, ...],
    *,
    include_translation: bool = False,
    include_romaji: bool = False,
) -> str:
    result: list[str] = []
    for line in lines:
        tag = f"[{_ms_to_tag(line.start_ms)}]"
        result.append(f"{tag}{line.text}")
        if include_translation and line.translation:
            result.append(f"{tag}{line.translation}")
        if include_romaji and line.romaji:
            result.append(f"{tag}{line.romaji}")
    return "\n".join(result)


def enhanced_lrc(
    lines: tuple[LyricLine, ...],
    *,
    include_translation: bool = False,
    include_romaji: bool = False,
) -> str:
    result: list[str] = []
    for line in lines:
        if line.words:
            out = "".join(f"[{_ms_to_tag(w.start_ms)}]{w.text}" for w in line.words)
            if line.duration_ms > 0:
                out += f"[{_ms_to_tag(line.start_ms + line.duration_ms)}]"
            result.append(out)
        else:
            result.append(f"[{_ms_to_tag(line.start_ms)}]{line.text}")
        tag = f"[{_ms_to_tag(line.start_ms)}]"
        if include_translation and line.translation:
            result.append(f"{tag}{line.translation}")
        if include_romaji and line.romaji:
            result.append(f"{tag}{line.romaji}")
    return "\n".join(result)


def plain_text(lines: tuple[LyricLine, ...]) -> str:
    return "\n".join(line.text for line in lines)

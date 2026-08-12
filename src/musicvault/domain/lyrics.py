from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LyricWord:
    start_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class LyricLine:
    start_ms: int
    duration_ms: int
    text: str
    words: tuple[LyricWord, ...] = ()
    translation: str = ""
    romaji: str = ""


def lyrics_to_json(lines: tuple[LyricLine, ...]) -> str:
    return json.dumps(
        [
            {
                "start_ms": line.start_ms,
                "duration_ms": line.duration_ms,
                "text": line.text,
                "words": [{"start_ms": w.start_ms, "text": w.text} for w in line.words],
                "translation": line.translation,
                "romaji": line.romaji,
            }
            for line in lines
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def lyrics_from_json(payload: str) -> tuple[LyricLine, ...]:
    raw = json.loads(payload)
    if not isinstance(raw, list):
        raise ValueError("lyrics payload 必须是行数组")
    lines: list[LyricLine] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"歌词行格式错误：{item}")
        words = tuple(LyricWord(start_ms=int(w["start_ms"]), text=str(w["text"])) for w in item.get("words") or ())
        lines.append(
            LyricLine(
                start_ms=int(item["start_ms"]),
                duration_ms=int(item.get("duration_ms", 0)),
                text=str(item.get("text", "")),
                words=words,
                translation=str(item.get("translation", "")),
                romaji=str(item.get("romaji", "")),
            )
        )
    return tuple(lines)

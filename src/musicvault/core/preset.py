from __future__ import annotations

import re
from dataclasses import dataclass

_VALID_QUALITIES = frozenset({"standard", "higher", "exhigh", "hires", "lossless"})
_VALID_FORMATS = frozenset({"flac", "mp3", "aac", "ogg", "opus"})
_VALID_TRANSLATION_FORMATS = frozenset({"separate", "inline", "notimestamp"})
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


@dataclass(slots=True)
class Preset:
    name: str
    quality: str = "hires"
    format: str | None = None
    bitrate: str | None = None
    filename_template: str = "{artist} - {name}"
    embed_cover: bool = True
    cover_max_size: int = 0
    embed_lyrics: bool = True
    metadata_fields: tuple[str, ...] = ()
    use_karaoke: bool = False
    include_translation: bool = True
    translation_format: str = "separate"
    include_romaji: bool = False
    write_lrc_file: bool = True
    lrc_encodings: tuple[str, ...] = ("utf-8",)

    def __post_init__(self) -> None:
        if not _VALID_NAME_RE.match(self.name):
            raise ValueError(
                f"Invalid preset name '{self.name}': must start with letter/digit, "
                f"contain only letters, digits, underscores, hyphens"
            )
        if self.quality not in _VALID_QUALITIES:
            raise ValueError(
                f"preset '{self.name}': quality must be one of {sorted(_VALID_QUALITIES)}, got '{self.quality}'"
            )
        if self.format is not None and self.format not in _VALID_FORMATS:
            raise ValueError(
                f"preset '{self.name}': format must be one of {sorted(_VALID_FORMATS)}, got '{self.format}'"
            )
        if self.translation_format not in _VALID_TRANSLATION_FORMATS:
            raise ValueError(
                f"preset '{self.name}': translation_format must be one of "
                f"{sorted(_VALID_TRANSLATION_FORMATS)}, got '{self.translation_format}'"
            )

    @property
    def audio_spec(self) -> tuple[str | None, str | None]:
        return (self.format, self.bitrate)


def audio_spec_key(fmt: str | None, bitrate: str | None) -> str:
    if fmt is None:
        return "ORIGINAL"
    fmt_upper = fmt.upper()
    if bitrate:
        return f"{fmt_upper}-{bitrate}"
    return fmt_upper


def build_audio_specs(presets: list[Preset]) -> set[tuple[str | None, str | None]]:
    return {p.audio_spec for p in presets}


def validate_presets(presets: list[Preset]) -> None:
    if not presets:
        raise ValueError("Config must have at least one preset")
    seen: set[str] = set()
    for p in presets:
        if p.name in seen:
            raise ValueError(f"duplicate preset name: '{p.name}'")
        seen.add(p.name)


def default_presets() -> list[Preset]:
    return [
        Preset(
            name="archive",
            quality="hires",
            format="flac",
            filename_template="{artist} - {name}",
            embed_cover=True,
            embed_lyrics=True,
            use_karaoke=True,
            include_translation=True,
            translation_format="separate",
            write_lrc_file=False,
        ),
        Preset(
            name="portable",
            quality="hires",
            format="mp3",
            bitrate="192k",
            filename_template="{alias} {name} - {artist}",
            embed_cover=False,
            embed_lyrics=False,
            use_karaoke=False,
            include_translation=True,
            translation_format="inline",
            write_lrc_file=True,
            lrc_encodings=("utf-8",),
        ),
    ]

from __future__ import annotations

from musicvault.preset_api.render import enhanced_lrc
from musicvault.preset_api.v1 import (
    AudioFormat,
    BasePreset,
    MetadataSpec,
    PresetRegistration,
    PresetRegistry,
    Quality,
)


class ArchivePreset(BasePreset):
    """无损归档 preset：FLAC + hires + 全量元数据 + 增强歌词（翻译/罗马音）。"""

    quality = Quality.HIRES
    format = AudioFormat.FLAC
    metadata = MetadataSpec.full()

    def build_lyrics(self, lines):
        return enhanced_lrc(lines, include_translation=True, include_romaji=True)


def register_builtin_presets(registry: PresetRegistry) -> None:
    registry.register_preset(PresetRegistration(name="archive", factory=ArchivePreset, source="builtin:archive"))

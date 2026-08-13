from __future__ import annotations

from musicvault.preset_api.render import enhanced_lrc_line
from musicvault.preset_api.v1 import (
    AudioFormat,
    BasePreset,
    MetadataSpec,
    PresetRegistration,
    PresetRegistry,
    Quality,
)


class ArchivePreset(BasePreset):
    """无损归档 preset：FLAC + hires + 全量元数据 + 增强歌词"""

    quality = Quality.HIRES
    format = AudioFormat.FLAC
    metadata = MetadataSpec.full()

    def build_lyric_line(self, line):
        return enhanced_lrc_line(line, include_translation=True)


def register_builtin_presets(registry: PresetRegistry) -> None:
    registry.register_preset(PresetRegistration(name="archive", factory=ArchivePreset, source="builtin:archive"))

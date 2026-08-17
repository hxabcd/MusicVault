from __future__ import annotations

import pytest

from musicvault.domain.lyrics import LyricLine
from musicvault.preset_api.v1 import (
    AudioFormat,
    BasePreset,
    LyricEmbed,
    LyricEncoding,
    MetadataField,
    MetadataSpec,
    PresetLoadError,
    PresetRegistration,
    PresetRegistry,
    Quality,
    audio_spec_key,
)


def test_registry_disabled_preset_is_filtered_when_requested() -> None:
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration("enabled", lambda: object(), source="a.py"))
    registry.register_preset(PresetRegistration("disabled", lambda: object(), source="b.py", enabled=False))

    assert {item.name for item in registry.preset_registrations()} == {"disabled", "enabled"}
    assert {item.name for item in registry.preset_registrations(enabled_only=True)} == {"enabled"}


def test_quality_maximum():
    assert Quality.maximum([Quality.HIGHER, Quality.HIRES, Quality.EXHIGH]) is Quality.HIRES
    assert Quality.maximum([Quality.HIRES, Quality.LOSSLESS]) is Quality.HIRES
    assert Quality.maximum([]) is Quality.HIRES


def test_quality_int_enum_ordered_and_level():
    assert Quality.HIRES.value == 5
    assert Quality.LOSSLESS.value == 4
    assert Quality.HIRES > Quality.LOSSLESS
    assert Quality.HIRES.level == "hires"
    assert Quality.LOSSLESS.level == "lossless"


def test_metadata_field_flags():
    assert MetadataField.NONE.value == 0
    assert not MetadataField.NONE
    assert MetadataField.BASIC == (MetadataField.TITLE | MetadataField.ARTIST | MetadataField.ALBUM)
    assert MetadataField.ALL == (
        MetadataField.BASIC
        | MetadataField.YEAR
        | MetadataField.TRACK_NUMBER
        | MetadataField.DISC_NUMBER
        | MetadataField.GENRE
        | MetadataField.ALBUM_ARTIST
        | MetadataField.COMPOSER
        | MetadataField.LYRICIST
        | MetadataField.COMMENT
    )
    assert MetadataField.TITLE in MetadataField.BASIC
    assert MetadataField.YEAR not in MetadataField.BASIC
    # 位掩码语义：两个成员 OR 后同时包含两者
    combo = MetadataField.YEAR | MetadataField.GENRE
    assert MetadataField.YEAR in combo
    assert MetadataField.GENRE in combo


def test_metadata_spec_presets_and_override():
    assert MetadataSpec.full().embed_cover is True
    assert MetadataSpec.none().embed_cover is False
    assert MetadataSpec.none().fields == MetadataField.NONE
    assert MetadataSpec.basic().embed_cover is True
    assert MetadataSpec.basic().fields == MetadataField.BASIC
    assert MetadataSpec.basic(embed_cover=False).embed_cover is False
    assert MetadataSpec.full().fields == MetadataField.ALL


def test_base_preset_defaults():
    preset = BasePreset()
    assert preset.quality is Quality.HIRES
    assert preset.format is None
    assert preset.lyrics_encoding is LyricEncoding.UTF_8


def test_lyric_embed_enum_values():
    assert LyricEmbed.NONE.value == 0
    assert LyricEmbed.OVERRIDE.value == 1
    assert LyricEmbed.SEPARATE.value == 2


def test_base_preset_lyric_embed_defaults_none():
    preset = BasePreset()
    assert preset.lyric_embed is LyricEmbed.NONE
    assert preset.metadata == MetadataSpec.basic()
    assert preset.build_lyric_line(LyricLine(1000, 0, "hello")) == "[00:01.000]hello"


def test_asset_spec_reflects_lyric_embed():
    class _Plain(BasePreset):
        format = AudioFormat.FLAC
        bitrate = "192k"

    # 普通 / OVERRIDE preset：资产 spec 与 audio_spec_key 一致（OVERRIDE 已内嵌 canonical，无需区分）
    assert _Plain().asset_spec == "FLAC-192k"

    class _Override(_Plain):
        lyric_embed = LyricEmbed.OVERRIDE

    assert _Override().asset_spec == "FLAC-192k"

    # SEPARATE preset：独立副本以 :embedded 变体 spec 注册，target 按 preset 声明的 spec 透明命中副本
    class _Separate(_Plain):
        lyric_embed = LyricEmbed.SEPARATE

    assert _Separate().asset_spec == "FLAC-192k:embedded"


def test_base_preset_subclass_override():
    class MyPreset(BasePreset):
        quality = Quality.LOSSLESS
        format = AudioFormat.FLAC

        def build_lyric_line(self, line):
            del line
            return "custom"

    preset = MyPreset()
    assert preset.quality is Quality.LOSSLESS
    assert preset.build_lyric_line(LyricLine(1000, 0, "hello")) == "custom"


def test_audio_spec_key():
    assert audio_spec_key(None, None) == "ORIGINAL"
    assert audio_spec_key(AudioFormat.FLAC, None) == "FLAC"
    assert audio_spec_key(AudioFormat.MP3, "192k") == "MP3-192k"


def test_registrations_validate_name():
    with pytest.raises(PresetLoadError):
        PresetRegistration(name="bad name", factory=object)
    with pytest.raises(PresetLoadError):
        PresetRegistration(name="", factory=object)

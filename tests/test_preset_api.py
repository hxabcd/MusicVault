from __future__ import annotations


import pytest

from musicvault.domain.lyrics import LyricLine
from musicvault.preset_api.v1 import (
    AudioFormat,
    BasePreset,
    LyricEncoding,
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
    assert Quality.maximum([]) is Quality.HIRES


def test_metadata_spec_presets_and_override():
    assert MetadataSpec.full().embed_cover is True
    assert MetadataSpec.none().embed_cover is False
    assert MetadataSpec.none().fields == ()
    assert MetadataSpec.basic().embed_cover is True
    assert MetadataSpec.basic().fields == ()
    assert MetadataSpec.basic(embed_cover=False).embed_cover is False


def test_base_preset_defaults():
    preset = BasePreset()
    assert preset.quality is Quality.HIRES
    assert preset.format is None
    assert preset.lyrics_encodings == (LyricEncoding.UTF_8,)
    assert preset.metadata == MetadataSpec.basic()
    assert preset.build_lyrics((LyricLine(1000, 0, "hello"),)) == "[00:01.000]hello"


def test_base_preset_subclass_override():
    class MyPreset(BasePreset):
        quality = Quality.LOSSLESS
        format = AudioFormat.FLAC

        def build_lyrics(self, lines):
            del lines
            return "custom"

    preset = MyPreset()
    assert preset.quality is Quality.LOSSLESS
    assert preset.build_lyrics(()) == "custom"


def test_audio_spec_key():
    assert audio_spec_key(None, None) == "ORIGINAL"
    assert audio_spec_key(AudioFormat.FLAC, None) == "FLAC"
    assert audio_spec_key(AudioFormat.MP3, "192k") == "MP3-192k"


def test_registrations_validate_name():
    with pytest.raises(PresetLoadError):
        PresetRegistration(name="bad name", factory=object)
    with pytest.raises(PresetLoadError):
        PresetRegistration(name="", factory=object)

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.domain.lyrics import LyricLine
from musicvault.preset_api.v1 import (
    API_VERSION,
    AudioFormat,
    BasePreset,
    LyricEncoding,
    MetadataSpec,
    PresetLoadError,
    PresetRegistration,
    PresetRegistry,
    Quality,
    TargetRegistration,
    audio_spec_key,
)


def test_registry_loads_script_and_keeps_source_metadata(tmp_path: Path) -> None:
    script = tmp_path / "one.py"
    script.write_text(
        "from musicvault.preset_api.v1 import API_VERSION\n"
        "class Sync:\n"
        "    def prepare(self, context): pass\n"
        "    def sync_item(self, track, context): pass\n"
        "    def finalize(self, context): pass\n"
        "def register(registry):\n"
        "    registry.register('external', Sync, api_version=API_VERSION)\n",
        encoding="utf-8",
    )

    registry = PresetRegistry()
    registry.load_directories([tmp_path])
    registration = registry.get("external")

    assert registration.source == str(script)
    assert registration.api_version == API_VERSION
    assert registration.create().__class__.__name__ == "Sync"


def test_registry_rejects_duplicate_names_with_both_sources(tmp_path: Path) -> None:
    for filename in ("a.py", "b.py"):
        (tmp_path / filename).write_text(
            "from musicvault.preset_api.v1 import API_VERSION\n"
            "class Sync:\n"
            "    def prepare(self, context): pass\n"
            "    def sync_item(self, track, context): pass\n"
            "    def finalize(self, context): pass\n"
            "def register(registry):\n"
            "    registry.register('same', Sync, api_version=API_VERSION)\n",
            encoding="utf-8",
        )

    with pytest.raises(PresetLoadError, match="same") as error:
        PresetRegistry().load_directories([tmp_path])
    assert "a.py" in str(error.value)
    assert "b.py" in str(error.value)


def test_registry_rejects_incompatible_api_version() -> None:
    registry = PresetRegistry()
    with pytest.raises(PresetLoadError, match="API"):
        registry.register(
            PresetRegistration(
                name="old",
                factory=lambda: object(),
                api_version="v0",
                source="old.py",
            )
        )


def test_registry_script_registers_multiple_presets(tmp_path: Path) -> None:
    script = tmp_path / "multi.py"
    script.write_text(
        "from musicvault.preset_api.v1 import API_VERSION\n"
        "class SyncA:\n"
        "    def prepare(self, context): pass\n"
        "    def sync_item(self, track, context): pass\n"
        "    def finalize(self, context): pass\n"
        "class SyncB:\n"
        "    def prepare(self, context): pass\n"
        "    def sync_item(self, track, context): pass\n"
        "    def finalize(self, context): pass\n"
        "def register(registry):\n"
        "    registry.register('multi-a', SyncA, api_version=API_VERSION)\n"
        "    registry.register('multi-b', SyncB, api_version=API_VERSION)\n",
        encoding="utf-8",
    )

    registry = PresetRegistry()
    registry.load_directories([tmp_path])

    assert {item.name for item in registry.registrations()} == {"multi-a", "multi-b"}
    assert registry.get("multi-a").create().__class__.__name__ == "SyncA"
    assert registry.get("multi-b").create().__class__.__name__ == "SyncB"


def test_registry_missing_register_function_reports_script_path(tmp_path: Path) -> None:
    script = tmp_path / "broken.py"
    script.write_text(
        "class Sync:\n"
        "    def prepare(self, context): pass\n"
        "    def sync_item(self, track, context): pass\n"
        "    def finalize(self, context): pass\n",
        encoding="utf-8",
    )

    with pytest.raises(PresetLoadError) as error:
        PresetRegistry().load_directories([tmp_path])

    assert "register" in str(error.value)
    assert str(script) in str(error.value)


def test_registry_multiple_directories_load_order_is_deterministic(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    for directory, name in ((first_dir, "alpha"), (second_dir, "beta")):
        directory.mkdir()
        (directory / "sync.py").write_text(
            "from musicvault.preset_api.v1 import API_VERSION\n"
            "class Sync:\n"
            "    def prepare(self, context): pass\n"
            "    def sync_item(self, track, context): pass\n"
            "    def finalize(self, context): pass\n"
            f"def register(registry):\n"
            f"    registry.register('{name}', Sync, api_version=API_VERSION)\n",
            encoding="utf-8",
        )

    forward = PresetRegistry()
    forward.load_directories([first_dir, second_dir])
    reverse = PresetRegistry()
    reverse.load_directories([second_dir, first_dir])

    assert [item.name for item in forward.registrations()] == ["alpha", "beta"]
    assert [item.name for item in reverse.registrations()] == ["alpha", "beta"]


def test_registry_disabled_preset_is_filtered_when_requested() -> None:
    registry = PresetRegistry()
    registry.register(PresetRegistration("enabled", lambda: object(), source="a.py"))
    registry.register(PresetRegistration("disabled", lambda: object(), source="b.py", enabled=False))

    assert {item.name for item in registry.registrations()} == {"disabled", "enabled"}
    assert {item.name for item in registry.registrations(enabled_only=True)} == {"enabled"}


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
        TargetRegistration(name="", factory=object)

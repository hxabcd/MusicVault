from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.preset_api.v1 import (
    API_VERSION,
    PresetLoadError,
    PresetRegistry,
    PresetRegistration,
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

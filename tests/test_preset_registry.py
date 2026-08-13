import pytest
from musicvault.preset_api.v1 import (
    PresetLoadError,
    PresetRegistration,
    PresetRegistry,
)


def test_register_and_create_preset():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="a", factory=dict))
    assert registry.create_preset("a") == {}


def test_duplicate_preset_names_rejected():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="x", factory=dict, source="a.py"))
    with pytest.raises(PresetLoadError, match="同名") as error:
        registry.register_preset(PresetRegistration(name="x", factory=dict, source="b.py"))
    assert "a.py" in str(error.value)
    assert "b.py" in str(error.value)


def test_incompatible_api_version_rejected():
    registry = PresetRegistry()
    with pytest.raises(PresetLoadError, match="API"):
        registry.register_preset(
            PresetRegistration(
                name="old",
                factory=lambda: object(),
                api_version="v0",
                source="old.py",
            )
        )


def test_unknown_preset_create_raises():
    registry = PresetRegistry()
    with pytest.raises(PresetLoadError, match="未找到"):
        registry.create_preset("missing")

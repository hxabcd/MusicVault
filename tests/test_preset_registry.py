import pytest
from musicvault.preset_api.v1 import (
    PresetLoadError,
    PresetRegistration,
    PresetRegistry,
    TargetRegistration,
)


def test_register_and_create_preset():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="a", factory=dict))
    assert registry.create_preset("a") == {}


def test_target_dependency_injection():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="a", factory=dict))
    captured: dict = {}

    def factory(presets):
        captured["presets"] = presets
        return object()

    registry.register_target(TargetRegistration(name="t", factory=factory, depends_on=("a",)))
    registry.create_target("t")
    assert captured["presets"] == {"a": {}}


def test_missing_dependency_raises():
    registry = PresetRegistry()
    registry.register_target(TargetRegistration(name="t", factory=lambda p: p, depends_on=("nope",)))
    with pytest.raises(PresetLoadError, match="nope"):
        registry.create_target("t")


def test_duplicate_names_rejected_across_kinds():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="x", factory=dict))
    with pytest.raises(PresetLoadError):
        registry.register_target(TargetRegistration(name="x", factory=dict))


def test_legacy_register_maps_to_target():
    registry = PresetRegistry()
    registry.register("t", factory=lambda: object())
    assert [r.name for r in registry.target_registrations()] == ["t"]

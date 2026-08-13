"""script_loader 单测：preset 目录与 sync_target 目录分开加载。

覆盖：双加载器分发、source 元数据、同名拒绝、缺少 register、脚本异常包装、
依赖缺失包装、目录排序确定性、_ 前缀跳过、错误类型脚本放入错误目录失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.application.script_loader import load_preset_directories, load_target_directories
from musicvault.preset_api.v1 import PresetLoadError, PresetRegistry
from musicvault.target_api.v1 import PresetLoadError as TargetLoadError, TargetRegistry

TARGET_SCRIPT = (
    "from musicvault.target_api.v1 import API_VERSION, TargetRegistration\n"
    "class Sync:\n"
    "    def prepare(self, context): pass\n"
    "    def sync_item(self, track, context): pass\n"
    "    def finalize(self, context): pass\n"
    "def register(registry):\n"
    "    registry.register_target(TargetRegistration(name='{name}', factory=Sync, api_version=API_VERSION))\n"
)

PRESET_SCRIPT = (
    "from musicvault.preset_api.v1 import API_VERSION, PresetRegistration\n"
    "class P:\n"
    "    pass\n"
    "def register(registry):\n"
    "    registry.register_preset(PresetRegistration(name='{name}', factory=P, api_version=API_VERSION))\n"
)


def test_loads_target_script_with_source_metadata(tmp_path: Path) -> None:
    script = tmp_path / "one.py"
    script.write_text(TARGET_SCRIPT.format(name="external"), encoding="utf-8")

    targets = TargetRegistry()
    load_target_directories([tmp_path], targets)

    registration = targets.target_registrations()[0]
    assert registration.name == "external"
    assert registration.source == str(script)
    assert registration.create().__class__.__name__ == "Sync"


def test_loads_preset_script_with_source_metadata(tmp_path: Path) -> None:
    script = tmp_path / "one.py"
    script.write_text(PRESET_SCRIPT.format(name="external"), encoding="utf-8")

    presets = PresetRegistry()
    load_preset_directories([tmp_path], presets)

    registration = presets.preset_registrations()[0]
    assert registration.name == "external"
    assert registration.source == str(script)
    assert registration.create().__class__.__name__ == "P"


def test_separate_directories_load_each_kind(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    target_dir = tmp_path / "targets"
    preset_dir.mkdir()
    target_dir.mkdir()
    (preset_dir / "p.py").write_text(PRESET_SCRIPT.format(name="my_preset"), encoding="utf-8")
    (target_dir / "t.py").write_text(TARGET_SCRIPT.format(name="my_target"), encoding="utf-8")

    presets = PresetRegistry()
    targets = TargetRegistry()
    load_preset_directories([preset_dir], presets)
    load_target_directories([target_dir], targets)

    assert [r.name for r in presets.preset_registrations()] == ["my_preset"]
    assert [r.name for r in targets.target_registrations()] == ["my_target"]
    assert presets.preset_registrations()[0].source == str(preset_dir / "p.py")
    assert targets.target_registrations()[0].source == str(target_dir / "t.py")


def test_wrong_kind_script_in_directory_fails(tmp_path: Path) -> None:
    """preset 目录放入 sync_target 脚本：register_target 不存在 → 包装为 PresetLoadError。"""
    (tmp_path / "t.py").write_text(TARGET_SCRIPT.format(name="my_target"), encoding="utf-8")

    presets = PresetRegistry()
    with pytest.raises(PresetLoadError, match="register_target"):
        load_preset_directories([tmp_path], presets)


def test_rejects_duplicate_names_with_both_sources(tmp_path: Path) -> None:
    for filename in ("a.py", "b.py"):
        (tmp_path / filename).write_text(TARGET_SCRIPT.format(name="same"), encoding="utf-8")

    with pytest.raises(TargetLoadError, match="same") as error:
        targets = TargetRegistry()
        load_target_directories([tmp_path], targets)
    assert "a.py" in str(error.value)
    assert "b.py" in str(error.value)


def test_missing_register_function_reports_script_path(tmp_path: Path) -> None:
    script = tmp_path / "broken.py"
    script.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(PresetLoadError) as error:
        presets = PresetRegistry()
        load_preset_directories([tmp_path], presets)

    assert "register" in str(error.value)
    assert str(script) in str(error.value)


def test_script_raising_runtime_error_is_wrapped(tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text("def register(registry):\n    raise ValueError('boom')\n", encoding="utf-8")

    with pytest.raises(PresetLoadError, match="boom") as error:
        presets = PresetRegistry()
        load_preset_directories([tmp_path], presets)

    assert str(script) in str(error.value)


def test_script_missing_dependency_is_wrapped(tmp_path: Path) -> None:
    script = tmp_path / "deps.py"
    script.write_text("import nonexistent_pkg_xyz\n", encoding="utf-8")

    with pytest.raises(PresetLoadError, match="nonexistent_pkg_xyz") as error:
        presets = PresetRegistry()
        load_preset_directories([tmp_path], presets)

    assert str(script) in str(error.value)


def test_script_preset_load_error_is_preserved(tmp_path: Path) -> None:
    """register() 内部抛 PresetLoadError 时不包裹。"""
    script = tmp_path / "reject.py"
    script.write_text(
        "from musicvault.preset_api.v1 import PresetLoadError\n"
        "def register(registry):\n"
        "    raise PresetLoadError('脚本主动拒绝')\n",
        encoding="utf-8",
    )

    with pytest.raises(PresetLoadError, match="脚本主动拒绝") as error:
        presets = PresetRegistry()
        load_preset_directories([tmp_path], presets)

    assert str(script) not in str(error.value)  # 原错误透传而非包裹


def test_script_spec_unavailable_raises(tmp_path: Path, monkeypatch) -> None:
    """spec 无法构造（loader 为 None）→ PresetLoadError。"""
    script = tmp_path / "missing.py"
    script.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "musicvault.application.script_loader.importlib.util.spec_from_file_location",
        lambda _name, _script: None,
    )

    with pytest.raises(PresetLoadError, match="无法加载"):
        presets = PresetRegistry()
        load_preset_directories([tmp_path], presets)


def test_multiple_directories_load_order_is_deterministic(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    for directory, name in ((first_dir, "alpha"), (second_dir, "beta")):
        directory.mkdir()
        (directory / "sync.py").write_text(TARGET_SCRIPT.format(name=name), encoding="utf-8")

    forward_targets = TargetRegistry()
    load_target_directories([first_dir, second_dir], forward_targets)
    reverse_targets = TargetRegistry()
    load_target_directories([second_dir, first_dir], reverse_targets)

    forward = [r.name for r in forward_targets.target_registrations()]
    reverse = [r.name for r in reverse_targets.target_registrations()]
    assert forward == reverse == ["alpha", "beta"]


def test_underscore_prefixed_scripts_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "_skip.py").write_text("raise AssertionError('不应加载')\n", encoding="utf-8")

    presets = PresetRegistry()
    targets = TargetRegistry()
    load_preset_directories([tmp_path], presets)
    load_target_directories([tmp_path], targets)

    assert presets.preset_registrations() == ()
    assert targets.target_registrations() == ()


def test_missing_directory_is_skipped(tmp_path: Path) -> None:
    presets = PresetRegistry()
    targets = TargetRegistry()
    load_preset_directories([tmp_path / "nope"], presets)
    load_target_directories([tmp_path / "nope"], targets)

    assert presets.preset_registrations() == ()
    assert targets.target_registrations() == ()

from __future__ import annotations

from pathlib import Path

from musicvault.application.bootstrap import build_runtime
from musicvault.core.config import Config


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def test_build_runtime_persists_registered_presets(tmp_path: Path) -> None:
    """build_runtime 应把 preset 与 sync_target 两类 registration 都写入 preset_registry（带 kind）。"""
    cfg = _make_cfg(tmp_path)
    runtime = build_runtime(cfg)

    registered = runtime.state.list_registered_presets()
    assert len(registered) == len(runtime.presets.preset_registrations()) + len(runtime.presets.target_registrations())

    by_name = {item.name: item for item in registered}
    hardlink = by_name["hardlink"]
    assert hardlink.source == "builtin:hardlink"
    assert hardlink.api_version == "v1"
    assert hardlink.enabled is True
    assert hardlink.script_hash is None
    assert hardlink.kind == "target"
    assert by_name["archive"].kind == "preset"


def test_build_runtime_persists_external_preset_directory(tmp_path: Path) -> None:
    """从外部 preset 目录加载的 preset 也应写入 preset_registry，source 为脚本路径。"""
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    (preset_dir / "my_preset.py").write_text(
        "from musicvault.preset_api.v1 import PresetRegistration\n"
        "def register(registry):\n"
        "    registry.register(PresetRegistration(name='my_preset', factory=lambda: None))\n",
        encoding="utf-8",
    )
    cfg = _make_cfg(tmp_path)
    cfg.preset_directories = (str(preset_dir),)

    runtime = build_runtime(cfg)

    by_name = {item.name: item for item in runtime.state.list_registered_presets()}
    my_preset = by_name["my_preset"]
    assert my_preset.source == str(preset_dir / "my_preset.py")
    assert my_preset.enabled is True


def test_build_runtime_twice_does_not_duplicate_rows(tmp_path: Path) -> None:
    """register_preset 的 ON CONFLICT(name) 保证重复加载不产生重复记录。"""
    cfg = _make_cfg(tmp_path)
    build_runtime(cfg)
    build_runtime(cfg)

    registered = build_runtime(cfg).state.list_registered_presets()
    names = [item.name for item in registered]
    assert len(names) == len(set(names))

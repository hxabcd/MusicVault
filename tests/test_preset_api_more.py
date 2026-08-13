"""preset_api v1 补充单测：注册表错误分支与工厂边界。

覆盖：PresetRegistration.create 工厂分支、register_preset/create_preset 的异常路径。
"""

from __future__ import annotations

import pytest

from musicvault.preset_api.v1 import (
    PresetLoadError,
    PresetRegistration,
    PresetRegistry,
)


# -- PresetRegistration.create 工厂分支 --------------------------------------


def test_preset_registration_create_callable() -> None:
    """factory 是 callable → 调用结果返回。"""
    registration = PresetRegistration(name="f", factory=lambda: "made")
    assert registration.create() == "made"


def test_preset_registration_create_unusable_factory_raises() -> None:
    """factory 不可调用且非类 → PresetLoadError。"""
    registration = PresetRegistration(name="bad", factory=42)
    with pytest.raises(PresetLoadError, match="不可调用"):
        registration.create()


# -- 注册表异常路径 ----------------------------------------------------------


def test_register_preset_incompatible_api_version() -> None:
    """register_preset 校验 API 版本。"""
    registry = PresetRegistry()
    with pytest.raises(PresetLoadError, match="API"):
        registry.register_preset(PresetRegistration(name="x", factory=dict, api_version="v0"))


def test_register_preset_duplicate_name() -> None:
    """register_preset 发现同名 preset 报错并保留双方来源。"""
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="x", factory=dict, source="a.py"))
    with pytest.raises(PresetLoadError, match="同名") as error:
        registry.register_preset(PresetRegistration(name="x", factory=dict, source="b.py"))
    assert "a.py" in str(error.value)
    assert "b.py" in str(error.value)


def test_create_preset_missing_raises() -> None:
    with pytest.raises(PresetLoadError, match="未找到"):
        PresetRegistry().create_preset("nope")

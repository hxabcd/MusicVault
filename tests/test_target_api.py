"""target_api v1 单测：TargetRegistry、TargetRegistration、TargetContext 与内置 hardlink 注册。

覆盖：register_target/target_registrations/create_target（依赖注入与缺失校验）、
同名拒绝、API 版本校验、TargetRegistration.create 工厂分支、TargetContext 边界、
register_builtin_targets 注册。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.domain.models import SourceSnapshot, Track
from musicvault.domain.operations import OperationStatus
from musicvault.target_api.builtins import register_builtin_targets
from musicvault.target_api.v1 import (
    API_VERSION,
    PresetLoadError,
    TargetContext,
    TargetRegistration,
    TargetRegistry,
)


class RecordingTarget:
    """记录 copy/link/write_text 调用参数的目标 fake。"""

    def __init__(self) -> None:
        self.copies: list[tuple[Path, Path]] = []

    def copy(self, source, destination) -> None:
        self.copies.append((Path(source), Path(destination)))

    def link(self, source, destination) -> None:
        del source, destination

    def write_text(self, destination, content, encoding="utf-8") -> None:
        del destination, content, encoding


def _context() -> TargetContext:
    snapshot = SourceSnapshot.from_data((), (), ())
    return TargetContext(snapshot=snapshot, target=RecordingTarget())


# -- TargetRegistry 注册与枚举 -------------------------------------------------


def test_register_and_enumerate_targets() -> None:
    registry = TargetRegistry()
    registration = TargetRegistration(name="t", factory=lambda p: object())
    registry.register_target(registration)
    assert registry.target_registrations() == (registration,)
    assert registry.target_registrations(enabled_only=True) == (registration,)


def test_register_target_rejects_duplicate_names() -> None:
    registry = TargetRegistry()
    registry.register_target(TargetRegistration(name="x", factory=lambda p: object()))
    with pytest.raises(PresetLoadError, match="x"):
        registry.register_target(TargetRegistration(name="x", factory=lambda p: object()))


def test_register_target_rejects_incompatible_api_version() -> None:
    registry = TargetRegistry()
    with pytest.raises(PresetLoadError, match="API"):
        registry.register_target(
            TargetRegistration(
                name="old",
                factory=lambda p: object(),
                api_version="v0",
                source="old.py",
            )
        )


# -- create_target 依赖注入 ----------------------------------------------------


def test_create_target_injects_depended_presets() -> None:
    registry = TargetRegistry()
    captured: dict = {}

    def factory(presets):
        captured["presets"] = presets
        return object()

    registry.register_target(TargetRegistration(name="t", factory=factory, depends_on=("a", "b")))
    created = registry.create_target("t", presets={"a": 1, "b": 2})
    assert created is not None
    assert captured["presets"] == {"a": 1, "b": 2}


def test_create_target_missing_dependency_raises() -> None:
    registry = TargetRegistry()
    registry.register_target(TargetRegistration(name="t", factory=lambda p: p, depends_on=("nope",)))
    with pytest.raises(PresetLoadError, match="nope"):
        registry.create_target("t", presets={})


def test_create_target_unknown_name_raises() -> None:
    registry = TargetRegistry()
    with pytest.raises(PresetLoadError, match="未找到"):
        registry.create_target("missing", presets={})


# -- TargetRegistration.create 工厂分支 ----------------------------------------


def test_target_registration_create_variants() -> None:
    """sync_target factory 三类形态：实例/可调用/不可调用。"""

    class Sync:
        def prepare(self, context):
            pass

        def sync_item(self, track, context):
            pass

        def finalize(self, context):
            pass

    sync = Sync()
    assert TargetRegistration(name="s", factory=sync).create() is sync
    assert TargetRegistration(name="f", factory=lambda: "t").create() == "t"
    with pytest.raises(PresetLoadError, match="不可调用"):
        TargetRegistration(name="b", factory=42).create()


def test_target_registration_default_target_descriptor() -> None:
    registration = TargetRegistration(name="t", factory=lambda p: object())
    assert registration.target is not None
    assert registration.target.identifier == "t"


# -- TargetContext 边界 --------------------------------------------------------


def test_context_copy_records_operation(tmp_path: Path) -> None:
    """copy 走 executor 并记录 SUCCEEDED 结果。"""
    source = tmp_path / "src.txt"
    source.write_text("x", encoding="utf-8")
    target = RecordingTarget()
    snapshot = SourceSnapshot.from_data((), (), ())
    context = TargetContext(snapshot=snapshot, target=target)

    result = context.copy(source, tmp_path / "dst.txt")

    assert result.status is OperationStatus.SUCCEEDED
    assert target.copies == [(source, tmp_path / "dst.txt")]


def test_context_lyrics_file_returns_none_without_root() -> None:
    context = _context()
    assert context.lyrics_file(1, "archive") is None


def test_context_lyrics_file_resolves_existing_file(tmp_path: Path) -> None:
    snapshot = SourceSnapshot.from_data((), (), ())
    context = TargetContext(snapshot=snapshot, target=RecordingTarget(), media_store_root=tmp_path)
    lyrics = tmp_path / "7" / "7.archive.lrc"
    lyrics.parent.mkdir(parents=True)
    lyrics.write_text("x", encoding="utf-8")
    assert context.lyrics_file(7, "archive") == lyrics


def test_context_tracks_playlists_assets_from_snapshot() -> None:
    track = Track(id=1, name="n", artists=[], album="")
    snapshot = SourceSnapshot.from_data((track,), (), ())
    context = TargetContext(snapshot=snapshot, target=RecordingTarget())
    assert context.tracks == (track,)
    assert context.playlists == ()
    assert context.media_assets == ()


# -- register_builtin_targets -------------------------------------------------


def test_register_builtin_targets_registers_hardlink(tmp_path: Path) -> None:
    registry = TargetRegistry()
    register_builtin_targets(registry, tmp_path / "library")
    names = [item.name for item in registry.target_registrations()]
    assert names == ["hardlink"]
    registration = registry.target_registrations()[0]
    assert registration.depends_on == ("archive",)
    assert registration.source == "builtin:hardlink"
    assert registration.api_version == API_VERSION


def test_register_builtin_targets_creates_distributor(tmp_path: Path) -> None:
    registry = TargetRegistry()
    register_builtin_targets(registry, tmp_path / "library")

    class Archive:
        format = "flac"
        bitrate = None

    distributor = registry.create_target("hardlink", presets={"archive": Archive()})
    assert distributor.preset is not None
    assert distributor.preset_name == "archive"
    assert distributor.target_root == tmp_path / "library"

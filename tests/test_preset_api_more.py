"""preset_api v1 补充单测：注册表错误分支、脚本加载异常、上下文方法边界。

覆盖：PresetRegistration/TargetRegistration.create 工厂分支、PresetContext
copy/属性/lyrics_file 边界、register/register_preset/register_target/get 的
异常路径、load_directories 跳过逻辑、脚本加载失败三类错误。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.domain.models import MediaAsset, SourceSnapshot, Track
from musicvault.domain.operations import OperationStatus
from musicvault.preset_api.v1 import (
    API_VERSION,
    PresetContext,
    PresetLoadError,
    PresetRegistration,
    PresetRegistry,
    TargetRegistration,
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


def _context() -> PresetContext:
    snapshot = SourceSnapshot.from_data((), (), ())
    return PresetContext(snapshot=snapshot, target=RecordingTarget())


# -- PresetRegistration.create 工厂分支 --------------------------------------


def test_preset_registration_create_object_with_methods() -> None:
    """factory 是带三生命周期方法的实例 → 原样返回。"""

    class Sync:
        def prepare(self, context):
            pass

        def sync_item(self, track, context):
            pass

        def finalize(self, context):
            pass

    sync = Sync()
    registration = PresetRegistration(name="s", factory=sync)
    assert registration.create() is sync


def test_preset_registration_create_callable() -> None:
    """factory 是 callable → 调用结果返回。"""
    registration = PresetRegistration(name="f", factory=lambda: "made")
    assert registration.create() == "made"


def test_preset_registration_create_unusable_factory_raises() -> None:
    """factory 不可调用且无生命周期方法 → PresetLoadError。"""
    registration = PresetRegistration(name="bad", factory=42)
    with pytest.raises(PresetLoadError, match="不可调用"):
        registration.create()


# -- TargetRegistration.create 工厂分支 --------------------------------------


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


# -- PresetContext 边界 ------------------------------------------------------


def test_context_copy_records_operation(tmp_path: Path) -> None:
    """copy 走 executor 并记录 SUCCEEDED 结果。"""
    source = tmp_path / "src.txt"
    source.write_text("x", encoding="utf-8")
    target = RecordingTarget()
    snapshot = SourceSnapshot.from_data((), (), ())
    context = PresetContext(snapshot=snapshot, target=target)

    result = context.copy(source, tmp_path / "dst.txt")

    assert result.status is OperationStatus.SUCCEEDED
    assert target.copies == [(source, tmp_path / "dst.txt")]


def test_context_copy_failure_is_recorded() -> None:
    """callback 抛异常 → FAILED 结果而非向上传播。"""

    class BoomTarget(RecordingTarget):
        def copy(self, source, destination) -> None:
            del source, destination
            raise ValueError("模拟失败")

    context = PresetContext(snapshot=SourceSnapshot.from_data((), (), ()), target=BoomTarget())
    result = context.copy(Path("a"), Path("b"))
    assert result.status is OperationStatus.FAILED
    assert "模拟失败" in result.error


def test_context_properties_expose_snapshot() -> None:
    """tracks / media_assets 属性透传快照内容。"""
    track = Track(id=1, name="歌", artists=[], album="")
    asset = MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=Path("x.flac"))
    snapshot = SourceSnapshot.from_data((track,), (), (asset,))
    context = PresetContext(snapshot=snapshot, target=RecordingTarget())

    assert context.tracks == (track,)
    assert context.media_assets == (asset,)
    assert context.playlists == ()


def test_lyrics_file_without_root_returns_none() -> None:
    """media_store_root 未配置 → lyrics_file 返回 None。"""
    context = _context()
    assert context.lyrics_file(1, "archive") is None


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


def test_create_target_missing_raises() -> None:
    with pytest.raises(PresetLoadError, match="未找到"):
        PresetRegistry().create_target("nope")


def test_register_with_registration_and_source_raises() -> None:
    """register 传入现成 TargetRegistration 时不能再指定 source。"""
    registry = PresetRegistry()
    with pytest.raises(TypeError, match="不能重复指定"):
        registry.register(TargetRegistration(name="t", factory=dict), source="x.py")


def test_get_missing_raises() -> None:
    with pytest.raises(PresetLoadError, match="未找到"):
        PresetRegistry().get("nope")


# -- 脚本目录加载 ------------------------------------------------------------


def test_load_directories_ignores_missing_directory(tmp_path: Path) -> None:
    """目录不存在时静默跳过。"""
    registry = PresetRegistry()
    assert registry.load_directories([tmp_path / "missing"]) == ()


def test_load_directories_skips_underscore_scripts(tmp_path: Path) -> None:
    """_ 前缀脚本不加载（即使内容有语法错误也不执行）。"""
    (tmp_path / "_internal.py").write_text("raise AssertionError('不应加载')\n", encoding="utf-8")
    registry = PresetRegistry()
    assert registry.load_directories([tmp_path]) == ()


def test_load_script_missing_file_raises(tmp_path: Path, monkeypatch) -> None:
    """spec 无法构造（loader 为 None）→ PresetLoadError。"""
    monkeypatch.setattr(
        "musicvault.preset_api.v1.importlib.util.spec_from_file_location",
        lambda _name, _script: None,
    )
    registry = PresetRegistry()
    with pytest.raises(PresetLoadError, match="无法加载"):
        registry._load_script(tmp_path / "missing.py")


def test_load_script_missing_dependency(tmp_path: Path) -> None:
    """脚本 import 不存在的模块 → 依赖缺失错误。"""
    script = tmp_path / "dep.py"
    script.write_text("import definitely_missing_module_xyz\n", encoding="utf-8")
    with pytest.raises(PresetLoadError, match="依赖缺失") as error:
        PresetRegistry().load_directories([tmp_path])
    assert "definitely_missing_module_xyz" in str(error.value)


def test_load_script_runtime_error(tmp_path: Path) -> None:
    """脚本执行抛出普通异常 → 加载失败错误（保留脚本路径）。"""
    script = tmp_path / "boom.py"
    script.write_text("raise NameError('未定义变量')\n", encoding="utf-8")
    with pytest.raises(PresetLoadError, match="加载失败") as error:
        PresetRegistry().load_directories([tmp_path])
    assert str(script) in str(error.value)


def test_load_script_preset_load_error_is_preserved(tmp_path: Path) -> None:
    """register() 内部抛 PresetLoadError 时不包裹。"""
    script = tmp_path / "reject.py"
    script.write_text(
        "from musicvault.preset_api.v1 import PresetLoadError, API_VERSION\n"
        "def register(registry):\n"
        "    raise PresetLoadError('脚本主动拒绝')\n",
        encoding="utf-8",
    )
    with pytest.raises(PresetLoadError, match="脚本主动拒绝"):
        PresetRegistry().load_directories([tmp_path])


def test_register_updates_loading_source_on_registration_object(tmp_path: Path) -> None:
    """register() 对已有 TargetRegistration 注入当前加载来源。"""
    registry = PresetRegistry()
    script = tmp_path / "src.py"
    script.write_text(
        "from musicvault.preset_api.v1 import TargetRegistration, API_VERSION\n"
        "def register(registry):\n"
        "    registry.register(TargetRegistration(name='obj', factory=dict, api_version=API_VERSION))\n",
        encoding="utf-8",
    )
    registry.load_directories([tmp_path])
    assert registry.get("obj").source == str(script)
    assert API_VERSION == "v1"

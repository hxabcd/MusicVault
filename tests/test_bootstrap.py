"""bootstrap composition root 测试：preset 注入、音质推导、内置注册（Task 13）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.application import bootstrap
from musicvault.application.bootstrap import (
    build_distribute_pipeline,
    build_pipeline,
    build_runtime,
    build_source_client,
)
from musicvault.core.config import Config
from musicvault.domain.operations import OperationStatus
from musicvault.preset_api.builtins import ArchivePreset
from musicvault.preset_api.v1 import Quality


class _FakeSdkResponse:
    status = 200

    def __init__(self, body: dict) -> None:
        self.body = body


class _FakeSdk:
    """极简 fake：记录 song_url_v1 收到的 level 参数，供音质断言。"""

    last_level: str | None = None

    def __init__(self, _=None) -> None:
        self.calls: list[dict] = []

    def song_url_v1(self, **kwargs):
        type(self).last_level = kwargs.get("level")
        self.calls.append(kwargs)
        return _FakeSdkResponse({"data": [{"id": 1, "url": None}]})


def _fake_sdk(monkeypatch) -> None:
    _FakeSdk.last_level = None
    monkeypatch.setattr("musicvault.adapters.providers.netease_client.NeteaseCloudMusicApi", _FakeSdk)


# -- build_source_client：config 字符串 → Quality 枚举（Task 8 修复） -------------------


def test_build_source_client_converts_config_quality_to_enum(monkeypatch) -> None:
    """真实路径：config.download_quality 字符串转 Quality 枚举后构造 NeteaseClient，
    调用 get_tracks_download_urls 不再抛 AttributeError。"""
    _fake_sdk(monkeypatch)
    cfg = Config(workspace="ws", download_quality="lossless")

    client = build_source_client(cfg)
    urls = client.get_tracks_download_urls([1])

    assert urls == {1: None}
    assert _FakeSdk.last_level == "lossless"


def test_build_source_client_falls_back_to_hires_on_unknown_quality(monkeypatch) -> None:
    """非法音质字符串回退 Quality.HIRES，不向 SDK 传 str。"""
    _fake_sdk(monkeypatch)
    cfg = Config(workspace="ws", download_quality="ultra")

    client = build_source_client(cfg)
    client.get_tracks_download_urls([1])

    assert _FakeSdk.last_level == "hires"


def test_build_source_client_accepts_explicit_quality(monkeypatch) -> None:
    """显式传入的 Quality 覆盖 config 字符串。"""
    _fake_sdk(monkeypatch)
    cfg = Config(workspace="ws", download_quality="hires")

    client = build_source_client(cfg, download_quality=Quality.LOSSLESS)
    client.get_tracks_download_urls([1])

    assert _FakeSdk.last_level == "lossless"


# -- build_pipeline：注册表 preset 实例索引注入（Task 12 修复） -------------------------


def test_build_pipeline_injects_registry_preset_instances(tmp_path: Path) -> None:
    """build_pipeline 从注册表构造 BasePreset 实例索引并注入 ProcessUseCase，
    不再有 cfg.presets 领域 Preset 回退路径（Task 17 已移除）。"""
    cfg = Config(workspace=str(tmp_path / "ws"))

    service = build_pipeline(cfg, source=MagicMock(), dry_run=True)

    assert set(service.process_service.presets) == {"archive"}
    assert isinstance(service.process_service.presets["archive"], ArchivePreset)


# -- build_runtime / build_distribute_pipeline：内置注册与 preset 索引传递 ----------------


def test_build_runtime_builtin_target_root_is_library_dir(tmp_path: Path) -> None:
    """Task 7 遗留修复：内置 hardlink target 的目标根 = library/（不再嵌套 playlist_links/），
    default_playlist_name 透传。"""
    cfg = Config(workspace=str(tmp_path / "ws"), default_playlist_name="其他")

    runtime = build_runtime(cfg)
    distributor = runtime.targets.create_target(
        "hardlink", presets={"archive": runtime.presets.create_preset("archive")}
    )

    assert distributor.target_root == cfg.library_dir
    assert distributor.default_name == "其他"


def test_build_runtime_builtin_scripts_disabled(tmp_path: Path) -> None:
    """builtin_scripts_enabled=False 时 build_runtime 不注册内置 archive/hardlink。"""
    cfg = Config(workspace=str(tmp_path / "ws"), builtin_scripts_enabled=False)

    runtime = build_runtime(cfg)

    assert runtime.presets.preset_registrations() == ()
    assert runtime.targets.target_registrations() == ()
    # preset 注册只存在于内存注册表（动态发现），不再写入 SQLite
    assert runtime.source_state.create_snapshot().tracks == ()


def test_build_distribute_pipeline_passes_preset_instances_to_engine(tmp_path: Path, monkeypatch) -> None:
    """distribute 链路：DistributePipeline.run 从注册表构造 presets 索引并注入 SyncEngine.run。"""
    cfg = Config(workspace=str(tmp_path / "ws"))
    captured: dict = {}

    def fake_run(self, snapshot, registrations, *, selected=None, presets=None):
        del self, snapshot, registrations, selected
        captured["presets"] = presets
        from musicvault.application.sync_engine import SyncRunResult

        return SyncRunResult(snapshot_hash="a" * 64, presets=(), status=OperationStatus.SUCCEEDED)

    monkeypatch.setattr(bootstrap.SyncEngine, "run", fake_run)

    pipeline = build_distribute_pipeline(cfg)
    pipeline.run()

    assert set(captured["presets"]) == {"archive"}
    assert isinstance(captured["presets"]["archive"], ArchivePreset)

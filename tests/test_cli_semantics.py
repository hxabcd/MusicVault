"""CLI 退出码语义与幂等重复执行测试。

覆盖：target-sync 结果失败返回退出码 1、运行期异常返回 2（退出码语义区分）；
同一资产连续两次 sync_item/link 到同一目标，第二次幂等跳过、不产生重复目标。
"""

from __future__ import annotations

import types
from pathlib import Path

from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.application.sync_engine import PresetRunResult, SyncEngine, SyncRunResult
from musicvault.cli.main import main
from musicvault.domain.models import SourceSnapshot, Track
from musicvault.domain.operations import OperationStatus
from musicvault.preset_api.v1 import PresetRegistration


def _snapshot() -> SourceSnapshot:
    return SourceSnapshot.from_data(
        tracks=(Track(id=1, name="一", artists=["甲"], album="专辑", raw={}),),
        playlists=(),
        media_assets=(),
    )


def _fake_signal_module() -> types.ModuleType:
    """替换 cli.main 中的 signal 模块，避免测试注册真实 SIGINT 处理器。"""
    fake = types.ModuleType("signal")
    fake.SIGINT = 2
    fake.signal = lambda signum, handler: None
    return fake


# -- CLI 退出码 ------------------------------------------------------------


class _FailedTargetSyncPipeline:
    """模拟目标同步整体失败的 target-sync pipeline。"""

    def run(self, *, selected: set[str] | None = None) -> SyncRunResult:
        return SyncRunResult(
            snapshot_hash="a" * 64,
            presets=(PresetRunResult(name="demo", source="test", status=OperationStatus.FAILED, error="模拟失败"),),
            status=OperationStatus.FAILED,
        )


def test_target_sync_failed_result_returns_exit_code_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr(
        "musicvault.application.bootstrap.build_target_sync_pipeline",
        lambda cfg, dry_run=False: _FailedTargetSyncPipeline(),
    )

    code = main(["target-sync", "--config", str(tmp_path / "config.json")])

    assert code == 1


def test_target_sync_runtime_error_returns_exit_code_2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    def _boom(cfg: object, dry_run: bool = False) -> None:
        raise RuntimeError("模拟加载失败")

    monkeypatch.setattr("musicvault.application.bootstrap.build_target_sync_pipeline", _boom)

    code = main(["target-sync", "--config", str(tmp_path / "config.json")])

    assert code == 2


# -- 幂等重复执行 ----------------------------------------------------------


class _LinkingSynchronizer:
    """每个曲目把共享源文件链接到自身目标。"""

    def __init__(self, source: Path) -> None:
        self.source = source

    def prepare(self, context: object) -> None:
        pass

    def sync_item(self, track: Track, context) -> None:
        context.link(self.source, Path(f"track-{track.id}.txt"))

    def finalize(self, context: object) -> None:
        pass


def test_repeated_sync_to_same_target_is_idempotent(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = tmp_path / "source.txt"
    source.write_text("内容", encoding="utf-8")
    registration = PresetRegistration("linker", lambda: _LinkingSynchronizer(source), source="test")

    first = SyncEngine(target=target).run(_snapshot(), [registration])
    second = SyncEngine(target=target).run(_snapshot(), [registration])

    # 两次运行均成功；第二次同内容幂等跳过，不产生重复目标文件
    assert first.status == OperationStatus.SUCCEEDED
    assert second.status == OperationStatus.SUCCEEDED
    assert (tmp_path / "root" / "track-1.txt").read_text(encoding="utf-8") == "内容"
    assert sorted(path.name for path in (tmp_path / "root").iterdir()) == ["track-1.txt"]

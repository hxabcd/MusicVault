"""SyncEngine 失败路径边界补测（收尾阶段）。

覆盖：prepare/sync_item/finalize 返回失败 OperationResult 的三种路径、
sync_item 内失败 operation 的聚合上报、failed_presets 过滤。
"""

from __future__ import annotations

from pathlib import Path

from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.application.sync_engine import SyncEngine
from musicvault.domain.models import SourceSnapshot, Track
from musicvault.domain.operations import OperationResult, OperationStatus
from musicvault.preset_api.v1 import TargetRegistration


def _snapshot() -> SourceSnapshot:
    return SourceSnapshot.from_data(
        tracks=(Track(id=1, name="一", artists=["甲"], album="专辑", raw={}),),
        playlists=(),
        media_assets=(),
    )


class PrepareReturnsFailedResult:
    """prepare 返回失败 OperationResult 而非抛出异常。"""

    def prepare(self, _) -> OperationResult:
        return OperationResult(name="prepare", status=OperationStatus.FAILED, error="准备校验失败")

    def sync_item(self, *_) -> None:
        raise AssertionError("prepare 失败后不应处理曲目")

    def finalize(self, _) -> None:
        raise AssertionError("prepare 失败后不应 finalize")


class ItemReturnsFailedResult:
    """sync_item 返回失败 OperationResult 而非抛出异常。"""

    def prepare(self, _) -> None:
        pass

    def sync_item(self, *_) -> OperationResult:
        return OperationResult(name="item", status=OperationStatus.FAILED, error="曲目校验失败")

    def finalize(self, _) -> None:
        pass


class ItemOperationFails:
    """sync_item 通过 custom_operation 登记一个失败的 operation。"""

    def prepare(self, _) -> None:
        pass

    def sync_item(self, track, context) -> None:
        context.custom_operation(f"item-{track.id}", lambda: 1 / 0)

    def finalize(self, _) -> None:
        pass


class FinalizeReturnsFailedResult:
    """finalize 返回失败 OperationResult 而非抛出异常。"""

    def prepare(self, _) -> None:
        pass

    def sync_item(self, *_) -> None:
        pass

    def finalize(self, _) -> OperationResult:
        return OperationResult(name="finalize", status=OperationStatus.FAILED, error="整理校验失败")


class Succeeding:
    """全流程成功，作为 failed_presets 过滤测试的对照组。"""

    def prepare(self, _) -> None:
        pass

    def sync_item(self, *_) -> None:
        pass

    def finalize(self, _) -> None:
        pass


def test_prepare_returns_failed_result_marks_preset_failed(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(
        _snapshot(), [TargetRegistration("p", lambda _: PrepareReturnsFailedResult(), source="t")]
    )

    preset = result.presets[0]
    assert preset.status == OperationStatus.FAILED
    assert preset.error == "准备校验失败"
    assert preset.item_results == ()


def test_sync_item_returns_failed_result_marks_item_failed(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(
        _snapshot(), [TargetRegistration("p", lambda _: ItemReturnsFailedResult(), source="t")]
    )

    preset = result.presets[0]
    assert preset.status == OperationStatus.FAILED
    assert preset.item_results[0].status == OperationStatus.FAILED
    assert preset.item_results[0].error == "曲目校验失败"


def test_sync_item_failed_operation_is_aggregated(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(
        _snapshot(), [TargetRegistration("p", lambda _: ItemOperationFails(), source="t")]
    )

    preset = result.presets[0]
    assert preset.item_results[0].status == OperationStatus.FAILED
    assert preset.item_results[0].error  # 失败 operation 聚合出的错误文案
    assert preset.status == OperationStatus.FAILED
    assert any(op.status == OperationStatus.FAILED for op in preset.operations)


def test_finalize_returns_failed_result_keeps_items_successful(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(
        _snapshot(), [TargetRegistration("p", lambda _: FinalizeReturnsFailedResult(), source="t")]
    )

    preset = result.presets[0]
    assert preset.status == OperationStatus.FAILED
    assert preset.error == "整理校验失败"
    assert preset.item_results[0].status == OperationStatus.SUCCEEDED


def test_failed_presets_filters_only_failed(tmp_path: Path) -> None:
    registrations = [
        TargetRegistration("bad", lambda _: PrepareReturnsFailedResult(), source="t"),
        TargetRegistration("good", lambda _: Succeeding(), source="t"),
    ]

    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(_snapshot(), registrations)

    failed = result.failed_presets
    assert [p.name for p in failed] == ["bad"]
    assert result.status == OperationStatus.FAILED

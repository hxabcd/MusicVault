from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from musicvault.domain.models import SourceSnapshot
from musicvault.domain.operations import OperationResult
from musicvault.preset_api.v1 import PresetContext, PresetRegistration
from musicvault.ports.target import TargetOperations


@dataclass(frozen=True, slots=True)
class ItemSyncResult:
    track_id: int
    status: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PresetRunResult:
    name: str
    source: str
    status: str
    item_results: tuple[ItemSyncResult, ...] = ()
    operations: tuple[OperationResult, ...] = ()
    error: str | None = None

    @property
    def success_count(self) -> int:
        return sum(item.status == "succeeded" for item in self.item_results)

    @property
    def failed_count(self) -> int:
        return sum(item.status == "failed" for item in self.item_results)

    @property
    def skipped_count(self) -> int:
        return sum(item.status == "skipped" for item in self.item_results)


@dataclass(frozen=True, slots=True)
class SyncRunResult:
    snapshot_hash: str
    presets: tuple[PresetRunResult, ...]
    status: str

    @property
    def failed_presets(self) -> tuple[PresetRunResult, ...]:
        return tuple(preset for preset in self.presets if preset.status == "failed")


class SyncEngine:
    """运行 TargetSynchronizer 生命周期并隔离 preset 失败。"""

    def __init__(self, target: TargetOperations, *, dry_run: bool = False) -> None:
        self.target = target
        self.dry_run = dry_run

    def run(
        self,
        snapshot: SourceSnapshot,
        registrations: Iterable[PresetRegistration],
        *,
        selected: set[str] | None = None,
    ) -> SyncRunResult:
        results: list[PresetRunResult] = []
        for registration in registrations:
            if selected is not None and registration.name not in selected:
                continue
            if not registration.enabled:
                results.append(
                    PresetRunResult(
                        name=registration.name,
                        source=registration.source,
                        status="skipped",
                        error="preset 已禁用",
                    )
                )
                continue
            results.append(self._run_preset(snapshot, registration))
        status = "succeeded" if all(result.status != "failed" for result in results) else "failed"
        return SyncRunResult(snapshot_hash=snapshot.snapshot_hash, presets=tuple(results), status=status)

    def _run_preset(self, snapshot: SourceSnapshot, registration: PresetRegistration) -> PresetRunResult:
        try:
            synchronizer = registration.create()
            context = PresetContext(
                snapshot=snapshot,
                target=self.target,
                dry_run=self.dry_run,
                target_descriptor=registration.target,
            )
            prepare_operation_start = len(context.operations)
            prepare_result = synchronizer.prepare(context)
            prepare_error = _operation_error(context.operations[prepare_operation_start:])
            if (isinstance(prepare_result, OperationResult) and not prepare_result.ok) or prepare_error:
                return PresetRunResult(
                    name=registration.name,
                    source=registration.source,
                    status="failed",
                    operations=context.operations,
                    error=(prepare_result.error if isinstance(prepare_result, OperationResult) else None)
                    or prepare_error
                    or "preset 准备失败",
                )
        except Exception as error:  # noqa: BLE001 - preset 初始化失败必须终止当前 preset
            return PresetRunResult(
                name=registration.name,
                source=registration.source,
                status="failed",
                error=str(error),
            )

        item_results: list[ItemSyncResult] = []
        for track in snapshot.tracks:
            item_operation_start = len(context.operations)
            try:
                item_result = synchronizer.sync_item(track, context)
                operation_error = _operation_error(context.operations[item_operation_start:])
                if isinstance(item_result, OperationResult) and not item_result.ok:
                    item_results.append(ItemSyncResult(track.id, "failed", item_result.error))
                elif operation_error:
                    item_results.append(ItemSyncResult(track.id, "failed", operation_error))
                else:
                    item_results.append(ItemSyncResult(track.id, "succeeded"))
            except Exception as error:  # noqa: BLE001 - 单项失败不阻塞后续曲目
                item_results.append(ItemSyncResult(track.id, "failed", str(error)))

        finalize_error: str | None = None
        finalize_operation_start = len(context.operations)
        try:
            finalize_result = synchronizer.finalize(context)
            if isinstance(finalize_result, OperationResult) and not finalize_result.ok:
                finalize_error = finalize_result.error or "preset 整理失败"
            finalize_error = finalize_error or _operation_error(context.operations[finalize_operation_start:])
        except Exception as error:  # noqa: BLE001 - 记录整理失败并保留其他结果
            finalize_error = str(error)

        failed = any(item.status == "failed" for item in item_results) or finalize_error is not None
        return PresetRunResult(
            name=registration.name,
            source=registration.source,
            status="failed" if failed else "succeeded",
            item_results=tuple(item_results),
            operations=context.operations,
            error=finalize_error,
        )


def run_snapshot(
    snapshot: SourceSnapshot,
    registrations: Iterable[PresetRegistration],
    target: TargetOperations,
    *,
    dry_run: bool = False,
    selected: set[str] | None = None,
) -> SyncRunResult:
    """便于 composition root 使用的薄封装。"""
    return SyncEngine(target, dry_run=dry_run).run(snapshot, registrations, selected=selected)


def _operation_error(operations: tuple[OperationResult, ...]) -> str | None:
    for operation in operations:
        if operation.status == "failed":
            return operation.error or f"操作失败：{operation.name}"
    return None

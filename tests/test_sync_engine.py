from __future__ import annotations

from pathlib import Path

from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.application.sync_engine import SyncEngine
from musicvault.core.models import Track
from musicvault.domain.models import SourceSnapshot
from musicvault.preset_api.v1 import PresetRegistration


def _snapshot() -> SourceSnapshot:
    return SourceSnapshot.from_data(
        tracks=(
            Track(id=1, name="一", artists=["甲"], album="专辑", raw={}),
            Track(id=2, name="二", artists=["乙"], album="专辑", raw={}),
        ),
        playlists=(),
        media_assets=(),
    )


class WritingSynchronizer:
    def prepare(self, context) -> None:
        context.write_text(Path("output.txt"), "ok")

    def sync_item(self, track, context) -> None:
        if track.id == 2:
            raise ValueError("单项失败")

    def finalize(self, context) -> None:
        context.custom_operation(
            "custom-check",
            lambda: "done",
            input_data={"track_count": len(context.snapshot.tracks)},
        )


class PrepareFailureSynchronizer:
    def prepare(self, context) -> None:
        raise RuntimeError("准备失败")

    def sync_item(self, track, context) -> None:
        raise AssertionError("prepare 失败后不应处理曲目")

    def finalize(self, context) -> None:
        raise AssertionError("prepare 失败后不应 finalize")


def test_engine_shares_snapshot_and_isolates_item_failures(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path)
    registration = PresetRegistration("writer", WritingSynchronizer, source="test")

    result = SyncEngine(target=target).run(_snapshot(), [registration])

    preset_result = result.presets[0]
    assert result.snapshot_hash == _snapshot().snapshot_hash
    assert preset_result.failed_count == 1
    assert preset_result.success_count == 1
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "ok"
    assert any(operation.name == "custom-check" for operation in preset_result.operations)


def test_engine_prepare_failure_does_not_run_items_or_other_presets(tmp_path: Path) -> None:
    registrations = [
        PresetRegistration("broken", PrepareFailureSynchronizer, source="broken"),
        PresetRegistration("writer", WritingSynchronizer, source="writer"),
    ]

    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(_snapshot(), registrations)

    assert result.presets[0].status == "failed"
    assert result.presets[0].item_results == ()
    assert result.presets[1].success_count == 1


def test_dry_run_does_not_run_standard_or_custom_side_effects(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path), dry_run=True).run(
        _snapshot(), [PresetRegistration("writer", WritingSynchronizer, source="test")]
    )

    assert not (tmp_path / "output.txt").exists()
    assert all(operation.status == "planned" for operation in result.presets[0].operations)

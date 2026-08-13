from __future__ import annotations

from pathlib import Path

from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.application.sync_engine import SyncEngine
from musicvault.domain.models import Track
from musicvault.domain.models import SourceSnapshot
from musicvault.target_api.v1 import TargetRegistration


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

    def sync_item(self, track, _) -> None:
        if track.id == 2:
            raise ValueError("单项失败")

    def finalize(self, context) -> None:
        context.custom_operation(
            "custom-check",
            lambda: "done",
            input_data={"track_count": len(context.snapshot.tracks)},
        )


class PrepareFailureSynchronizer:
    def prepare(self, _) -> None:
        raise RuntimeError("准备失败")

    def sync_item(self, *_) -> None:
        raise AssertionError("prepare 失败后不应处理曲目")

    def finalize(self, _) -> None:
        raise AssertionError("prepare 失败后不应 finalize")


def test_engine_shares_snapshot_and_isolates_item_failures(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path)
    registration = TargetRegistration("writer", lambda _: WritingSynchronizer(), source="test")

    result = SyncEngine(target=target).run(_snapshot(), [registration])

    preset_result = result.presets[0]
    assert result.snapshot_hash == _snapshot().snapshot_hash
    assert preset_result.failed_count == 1
    assert preset_result.success_count == 1
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "ok"
    assert any(operation.name == "custom-check" for operation in preset_result.operations)


def test_engine_prepare_failure_does_not_run_items_but_other_presets_continue(tmp_path: Path) -> None:
    registrations = [
        TargetRegistration("broken", lambda _: PrepareFailureSynchronizer(), source="broken"),
        TargetRegistration("writer", lambda _: WritingSynchronizer(), source="writer"),
    ]

    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(_snapshot(), registrations)

    assert result.presets[0].status == "failed"
    assert result.presets[0].item_results == ()
    assert result.presets[1].success_count == 1


def test_dry_run_does_not_run_standard_or_custom_side_effects(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path), dry_run=True).run(
        _snapshot(), [TargetRegistration("writer", lambda _: WritingSynchronizer(), source="test")]
    )

    assert not (tmp_path / "output.txt").exists()
    assert all(operation.status == "planned" for operation in result.presets[0].operations)


class SucceedingSynchronizer:
    def prepare(self, context) -> None:
        context.write_text(Path("output.txt"), "ok")

    def sync_item(self, *_) -> None:
        pass

    def finalize(self, _) -> None:
        pass


class FinalizeFailureSynchronizer:
    def prepare(self, context) -> None:
        context.write_text(Path("output.txt"), "ok")

    def sync_item(self, *_) -> None:
        pass

    def finalize(self, _) -> None:
        raise RuntimeError("整理失败")


class MarkerSynchronizer:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def prepare(self, context) -> None:
        context.write_text(Path(f"{self.marker}.txt"), "ok")

    def sync_item(self, *_) -> None:
        pass

    def finalize(self, _) -> None:
        pass


def test_engine_one_preset_failure_does_not_stop_others_and_overall_is_failed(tmp_path: Path) -> None:
    registrations = [
        TargetRegistration("broken", lambda _: PrepareFailureSynchronizer(), source="broken"),
        TargetRegistration("good", lambda _: SucceedingSynchronizer(), source="good"),
    ]

    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(_snapshot(), registrations)

    assert result.status == "failed"
    assert result.presets[0].status == "failed"
    assert result.presets[0].item_results == ()
    assert result.presets[1].status == "succeeded"
    assert result.presets[1].success_count == 2
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "ok"


def test_engine_finalize_failure_keeps_successful_items_and_marks_failed(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(
        _snapshot(),
        [TargetRegistration("finalize-broken", lambda _: FinalizeFailureSynchronizer(), source="test")],
    )

    preset_result = result.presets[0]
    assert preset_result.status == "failed"
    assert preset_result.success_count == 2
    assert preset_result.failed_count == 0
    assert "整理失败" in (preset_result.error or "")
    assert (tmp_path / "output.txt").exists()


def test_engine_disabled_preset_is_skipped_without_running(tmp_path: Path) -> None:
    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(
        _snapshot(),
        [TargetRegistration("disabled", lambda _: SucceedingSynchronizer(), source="test", enabled=False)],
    )

    preset_result = result.presets[0]
    assert preset_result.status == "skipped"
    assert preset_result.error == "preset 已禁用"
    assert not (tmp_path / "output.txt").exists()
    assert result.status == "succeeded"


def test_engine_selected_subset_runs_only_requested_presets(tmp_path: Path) -> None:
    registrations = [
        TargetRegistration("alpha", lambda _: MarkerSynchronizer("alpha"), source="test"),
        TargetRegistration("beta", lambda _: MarkerSynchronizer("beta"), source="test"),
    ]

    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(_snapshot(), registrations, selected={"alpha"})

    assert [item.name for item in result.presets] == ["alpha"]
    assert (tmp_path / "alpha.txt").exists()
    assert not (tmp_path / "beta.txt").exists()


def test_engine_injects_presets_into_factory(tmp_path: Path) -> None:
    """presets 索引按 depends_on 声明注入 target factory（Task 13 核心）。"""
    received: dict[str, object] = {}

    def factory(presets):
        received.update(presets)
        return SucceedingSynchronizer()

    registration = TargetRegistration(name="injector", factory=factory, depends_on=("a",))
    preset_object = object()

    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(
        _snapshot(), [registration], presets={"a": preset_object}
    )

    assert received == {"a": preset_object}
    assert result.status == "succeeded"


def test_engine_missing_preset_dependency_marks_failed_without_calling_factory(tmp_path: Path) -> None:
    """depends_on 未在 presets 索引中提供 → 该 target FAILED，factory 不被调用。"""
    called = False

    def factory(_):
        nonlocal called
        called = True
        return SucceedingSynchronizer()

    registration = TargetRegistration(name="dep", factory=factory, depends_on=("missing",))
    result = SyncEngine(target=FilesystemTarget(tmp_path)).run(_snapshot(), [registration], presets={})

    preset_result = result.presets[0]
    assert preset_result.status == "failed"
    assert "依赖的 preset 未提供" in (preset_result.error or "")
    assert result.status == "failed"
    assert called is False


def test_engine_media_store_root_reaches_context(tmp_path: Path) -> None:
    """SyncEngine.media_store_root 透传到 TargetContext（内置 hardlink 的歌词文件依赖）。"""
    seen: list[Path | None] = []

    class CapturingSynchronizer:
        def prepare(self, context) -> None:
            seen.append(context.media_store_root)

        def sync_item(self, *_) -> None:
            pass

        def finalize(self, _) -> None:
            pass

    registration = TargetRegistration("capture", lambda _: CapturingSynchronizer(), source="test")
    engine = SyncEngine(target=FilesystemTarget(tmp_path), media_store_root=Path("media_root"))
    engine.run(_snapshot(), [registration])

    assert seen == [Path("media_root")]

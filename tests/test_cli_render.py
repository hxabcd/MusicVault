"""CLI 渲染层（render.py）测试。

直接调用各渲染函数，用 capfd 捕获 Rich console（stderr）输出断言文本；
覆盖空/非空各分支与 BatchProgressAdapter 生命周期。
"""

from __future__ import annotations

from musicvault.application.pipeline_use_case import PipelineResult
from musicvault.application.sync_engine import ItemSyncResult, PresetRunResult, SyncRunResult
from musicvault.cli.render import (
    BatchProgressAdapter,
    render_distribute_result,
    render_dry_run_plan,
    render_pipeline_result,
    render_sync_summary,
)
from musicvault.domain.models import Track
from musicvault.domain.operations import OperationStatus


def _track(track_id: int, name: str, artists: list[str]) -> Track:
    return Track(id=track_id, name=name, artists=artists, album="", raw={})


def _plan() -> dict:
    """完整 dry-run 计划：所有分段都有内容。"""
    return {
        "with_url": [_track(1, "歌一", ["甲"])],
        "no_url": [_track(2, "歌二", ["乙"])],
        "pruned": [3, 4],
        "moves": [("lib/旧", "lib/新")],
        "renames": [("", "旧目录", "新目录")],
        "stale_index": 5,
        "track_count": 2,
        "playlist_count": 1,
    }


def _distribute_result() -> SyncRunResult:
    """含成功/失败项的 distribute 结果。"""
    return SyncRunResult(
        snapshot_hash="a" * 64,
        status=OperationStatus.SUCCEEDED,
        presets=(
            PresetRunResult(
                name="hardlink",
                source="builtin",
                status=OperationStatus.SUCCEEDED,
                item_results=(
                    ItemSyncResult(1, OperationStatus.SUCCEEDED),
                    ItemSyncResult(2, OperationStatus.FAILED, "模拟失败"),
                ),
            ),
        ),
    )


def _all_output(capfd) -> str:
    captured = capfd.readouterr()
    return captured.out + captured.err


# -- render_sync_summary ----------------------------------------------------------


def test_render_sync_summary_with_stats(capfd) -> None:
    """有新增/清理数量时展示 +/- 统计。"""
    render_sync_summary(track_count=10, playlist_count=2, added=3, pruned=2)

    out = _all_output(capfd)
    assert "从 2 个歌单同步 10 首" in out
    assert "+3 首" in out
    assert "-2 首" in out


def test_render_sync_summary_no_change(capfd) -> None:
    """无新增/清理时展示「无变化」。"""
    render_sync_summary(track_count=0, playlist_count=0, added=0, pruned=0)

    assert "无变化" in _all_output(capfd)


# -- render_dry_run_plan ------------------------------------------------------------


def test_render_dry_run_plan_full(capfd) -> None:
    """完整计划：各分段均输出。"""
    render_dry_run_plan(_plan())

    out = _all_output(capfd)
    assert "将下载" in out and "歌一" in out
    assert "无可用直链将跳过" in out and "歌二" in out
    assert "将清理远端已删除曲目" in out
    assert "歌单目录将重命名" in out and "旧目录 → 新目录" in out
    assert "歌单归属调整" in out
    assert "将清理 5 条" in out


def test_render_dry_run_plan_empty(capfd) -> None:
    """空计划：只输出「将下载 0 首」占位行。"""
    render_dry_run_plan({})

    out = _all_output(capfd)
    assert "将下载 0 首（无新增曲目）" in out


# -- render_pipeline_result -----------------------------------------------------------


def test_render_pipeline_result_dry_run_with_plan_and_distribute(capfd) -> None:
    """dry-run：输出预览、后处理提示与随附的 distribute 结果。"""
    result = PipelineResult(dry_run_plan=_plan(), distribute=_distribute_result())

    render_pipeline_result(result, dry_run=True)

    out = _all_output(capfd)
    assert "dry-run 预览" in out
    assert "随后将进入后处理：新下载的 1 首曲目" in out
    assert "dry-run 结束" in out
    assert "hardlink" in out


def test_render_pipeline_result_dry_run_no_new_tracks(capfd) -> None:
    """dry-run 无新下载：不输出后处理提示。"""
    plan = {"with_url": [], "track_count": 0, "playlist_count": 0}
    result = PipelineResult(dry_run_plan=plan)

    render_pipeline_result(result, dry_run=True)

    out = _all_output(capfd)
    assert "随后将进入后处理" not in out
    assert "dry-run 结束" in out


def test_render_pipeline_result_dry_run_without_plan_falls_back(capfd) -> None:
    """dry-run 但无计划：回退到普通汇总渲染。"""
    result = PipelineResult(track_count=5, playlist_count=1)

    render_pipeline_result(result, dry_run=True)

    assert "个歌单同步 5 首" in _all_output(capfd)


def test_render_pipeline_result_normal(capfd) -> None:
    """普通路径：同步汇总、处理完成数与完成标记。"""
    result = PipelineResult(downloaded=3, processed=4, pruned=1, track_count=5, playlist_count=2)

    render_pipeline_result(result, dry_run=False)

    out = _all_output(capfd)
    assert "从 2 个歌单同步 5 首" in out
    assert "处理完成 4 首" in out
    assert "完成" in out


def test_render_pipeline_result_normal_with_distribute(capfd) -> None:
    """普通路径带 distribute 结果：追加渲染各 preset 汇总。"""
    result = PipelineResult(processed=1, distribute=_distribute_result())

    render_pipeline_result(result, dry_run=False)

    out = _all_output(capfd)
    assert "hardlink: OperationStatus.SUCCEEDED" in out
    assert "成功 1，失败 1，操作 0" in out


def test_render_pipeline_result_only_distribute(capfd) -> None:
    """only_distribute：跳过同步汇总，只渲染分发结果。"""
    result = PipelineResult(distribute=_distribute_result())

    render_pipeline_result(result, dry_run=False, only_distribute=True)

    out = _all_output(capfd)
    assert "个歌单同步" not in out
    assert "hardlink: OperationStatus.SUCCEEDED" in out
    assert "成功 1，失败 1，操作 0" in out
    assert "完成" in out


# -- render_distribute_result --------------------------------------------------------


def test_render_distribute_result_empty_presets(capfd) -> None:
    """无 preset 结果：不输出任何行。"""
    result = SyncRunResult(snapshot_hash="c" * 64, presets=(), status=OperationStatus.SUCCEEDED)

    render_distribute_result(result)

    assert _all_output(capfd) == ""


def test_render_distribute_result_multiple_presets(capfd) -> None:
    """多个 preset：逐行输出名称/状态/成功/失败/操作数。"""
    result = SyncRunResult(
        snapshot_hash="d" * 64,
        status=OperationStatus.FAILED,
        presets=(
            PresetRunResult(name="archive", source="builtin", status=OperationStatus.SUCCEEDED),
            PresetRunResult(
                name="hardlink",
                source="builtin",
                status=OperationStatus.FAILED,
                error="模拟失败",
                item_results=(
                    ItemSyncResult(1, OperationStatus.SUCCEEDED),
                    ItemSyncResult(2, OperationStatus.SKIPPED),
                    ItemSyncResult(3, OperationStatus.FAILED, "err"),
                ),
            ),
        ),
    )

    render_distribute_result(result)

    out = _all_output(capfd)
    assert "archive: OperationStatus.SUCCEEDED，成功 0，失败 0，操作 0" in out
    assert "hardlink: OperationStatus.FAILED，成功 1，失败 1，操作 0" in out


# -- BatchProgressAdapter ---------------------------------------------------------------


def test_batch_progress_adapter_lifecycle(capfd) -> None:
    """适配器 begin/advance/end 全生命周期无异常，结束后释放 batch。"""
    adapter = BatchProgressAdapter()

    adapter.begin(total=2, phase="测试阶段")
    assert adapter._batch is not None
    adapter.advance(success=True, idx=1, item_name="曲目一")
    adapter.advance(success=False, idx=2, item_name="曲目二")
    adapter.end()
    assert adapter._batch is None

    assert "测试阶段" in _all_output(capfd)

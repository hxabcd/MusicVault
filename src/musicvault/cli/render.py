"""CLI 渲染层：把用例结构化结果渲染为终端输出（Rich 仅存在于本层与 cli 其余部分）。"""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.table import Table

from musicvault.application.pipeline_use_case import PipelineResult
from musicvault.application.sync_engine import SyncRunResult
from musicvault.domain.models import Track
from musicvault.domain.operations import OperationStatus
from musicvault.preset_api.v1 import PresetRegistration, TargetRegistration
from musicvault.shared.tui_progress import BatchProgress, console, ok


class BatchProgressAdapter:
    """把 BatchProgress 适配为 ProgressReporter；每次 begin 重建进度条。"""

    def __init__(self) -> None:
        self._batch: BatchProgress | None = None

    def begin(self, total: int, phase: str) -> None:
        self._batch = BatchProgress(total=total, phase=phase)
        self._batch.__enter__()

    def advance(self, *, success: bool, idx: int, item_name: str) -> None:
        assert self._batch is not None
        self._batch.advance(success, idx, item_name)

    def end(self) -> None:
        assert self._batch is not None
        self._batch.__exit__(None, None, None)
        self._batch = None


def render_sync_summary(track_count: int, playlist_count: int, added: int, pruned: int) -> None:
    """「从 N 个歌单同步 M 首」摘要（原 SyncUseCase.run_sync 内部打印）。"""
    stats: list[str] = []
    if added:
        stats.append(f"[green]+{added} 首[/green]")
    if pruned:
        stats.append(f"[red]-{pruned} 首[/red]")
    console.print(f"  从 [cyan]{playlist_count}[/cyan] 个歌单同步 [cyan]{track_count}[/cyan] 首")
    console.print("    " + " | ".join(stats) if stats else "    [dim]无变化[/dim]")


def render_dry_run_plan(plan: dict) -> None:
    """dry-run 计划预览（原 SyncUseCase._print_dry_run_plan）。"""
    with_url: list[Track] = plan.get("with_url") or []
    no_url: list[Track] = plan.get("no_url") or []
    pruned: list[Track] = plan.get("pruned") or []
    moves: list[Track] = plan.get("moves") or []
    renames: list[tuple[Track, str, str]] = plan.get("renames") or []
    stale_index: int = plan.get("stale_index") or 0

    if with_url:
        _render_track_list(with_url, header="将下载", color="green")
    else:
        console.print("  [dim]将下载 0 首（无新增曲目）[/dim]")

    if no_url:
        _render_track_list(no_url, header="无可用直链将跳过", color="yellow")

    if pruned:
        console.print(f"  [red]将清理远端已删除曲目[/red] [cyan]{len(pruned)}[/cyan] 首：{', '.join(map(str, pruned))}")

    if renames:
        console.print("  [cyan]歌单目录将重命名：[/cyan]")
        for _, old, new in renames:
            console.print(f"    [dim]-[/dim] {old} → {new}")

    if moves:
        console.print(f"  [cyan]歌单归属调整：[/cyan][cyan]{len(moves)}[/cyan] 首曲目的 library 链接将移动")

    if stale_index:
        console.print(f"  [yellow]将清理 {stale_index} 条本地文件缺失的过期索引[/yellow]")


def _render_track_list(tracks: Sequence[Track], *, header: str, color: str) -> None:
    """dry-run 曲目列表：Rich 表格（序号 + 曲目）。"""
    console.print(f"  [{color}]{header}[/{color}] [cyan]{len(tracks)}[/cyan] 首：")
    table = Table(show_header=False, box=None, padding=(0, 1), collapse_padding=True)
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column()
    for i, track in enumerate(tracks, 1):
        table.add_row(f"{i}.", f"{track.artist_text} - {track.name}")
    console.print(table, highlight=False)


def render_pipeline_result(result: PipelineResult, *, dry_run: bool, only_distribute: bool = False) -> None:
    """pipeline 运行结束后的汇总输出。

    only_distribute 时 sync 阶段未执行，只渲染分发结果。
    """
    if dry_run and result.dry_run_plan:
        console.print(
            f"  从 [cyan]{result.dry_run_plan.get('playlist_count', '?')}[/cyan] 个歌单同步 "
            f"[cyan]{result.dry_run_plan.get('track_count', '?')}[/cyan] 首（[bold yellow]dry-run 预览[/bold yellow]）"
        )
        render_dry_run_plan(result.dry_run_plan)
        n_new = len((result.dry_run_plan or {}).get("with_url") or [])
        if n_new:
            console.print(f"  [dim]随后将进入后处理：新下载的 {n_new} 首曲目（转码/元数据/歌词/硬链接）[/dim]")
        console.print("  [bold yellow]dry-run 结束：未下载、未修改任何文件[/bold yellow]")
        if result.distribute is not None:
            render_distribute_result(result.distribute)
        return
    if not only_distribute:
        render_sync_summary(result.track_count, result.playlist_count, result.downloaded, result.pruned)
        if result.processed:
            console.print(f"  [green]处理完成 {result.processed} 首[/green]")
    if result.distribute is not None:
        render_distribute_result(result.distribute)
    ok("完成")


_STATUS_TEXT: dict[OperationStatus, str] = {
    OperationStatus.PLANNED: "[cyan]计划中[/cyan]",
    OperationStatus.SUCCEEDED: "[green]成功[/green]",
    OperationStatus.FAILED: "[red]失败[/red]",
    OperationStatus.SKIPPED: "[yellow]跳过[/yellow]",
}


def render_distribute_result(result: SyncRunResult) -> None:
    """distribute 结果逐 preset 汇总（distribute 命令与 sync 分发阶段共用）。"""
    if not result.presets:
        return
    table = Table(header_style="bold cyan", box=box.SIMPLE_HEAD)
    table.add_column("目标", style="bold", no_wrap=True)
    table.add_column("状态", justify="center")
    table.add_column("成功", justify="right")
    table.add_column("失败", justify="right")
    table.add_column("操作", justify="right")
    for preset_result in result.presets:
        table.add_row(
            preset_result.name,
            _STATUS_TEXT.get(preset_result.status, str(preset_result.status)),
            str(preset_result.success_count),
            str(preset_result.failed_count),
            str(len(preset_result.operations)),
        )
    console.print(table)


def _state_text(enabled: bool) -> str:
    """注册状态着色：启用绿色、禁用红色。"""
    return "[green]启用[/green]" if enabled else "[red]禁用[/red]"


def render_presets(preset_registrations: Sequence[PresetRegistration]) -> None:
    """preset list 命令：以 Rich 表格列出 preset 注册项。"""
    if not preset_registrations:
        console.print("  [dim]未发现 preset[/dim]")
        return
    table = Table(title="可用 Preset", header_style="bold cyan", box=box.SIMPLE_HEAD)
    table.add_column("名称", style="bold", no_wrap=True)
    table.add_column("状态", justify="center")
    table.add_column("API", justify="center")
    table.add_column("来源")
    for registration in preset_registrations:
        table.add_row(
            registration.name,
            _state_text(registration.enabled),
            registration.api_version,
            registration.source,
        )
    console.print(table)


def render_targets(target_registrations: Sequence[TargetRegistration]) -> None:
    """target list 命令：以 Rich 表格列出 sync_target 注册项。"""
    if not target_registrations:
        console.print("  [dim]未发现 sync_target[/dim]")
        return
    table = Table(title="可用 sync_target", header_style="bold cyan", box=box.SIMPLE_HEAD)
    table.add_column("名称", style="bold", no_wrap=True)
    table.add_column("状态", justify="center")
    table.add_column("API", justify="center")
    table.add_column("来源")
    for registration in target_registrations:
        table.add_row(
            registration.name,
            _state_text(registration.enabled),
            registration.api_version,
            registration.source,
        )
    console.print(table)

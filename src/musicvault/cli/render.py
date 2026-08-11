"""CLI 渲染层：把用例结构化结果渲染为终端输出（Rich 仅存在于本层与 cli 其余部分）。"""

from __future__ import annotations

from musicvault.application.pipeline_use_case import PipelineResult
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
    with_url: list = plan.get("with_url") or []
    no_url: list = plan.get("no_url") or []
    pruned: list = plan.get("pruned") or []
    moves: list = plan.get("moves") or []
    renames: list = plan.get("renames") or []
    stale_index: int = plan.get("stale_index") or 0

    if with_url:
        console.print(f"  [green]将下载[/green] [cyan]{len(with_url)}[/cyan] 首：")
        for i, t in enumerate(with_url, 1):
            console.print(f"    [dim]{i:>3}.[/dim] {t.artist_text} - {t.name}")
    else:
        console.print("  [dim]将下载 0 首（无新增曲目）[/dim]")

    if no_url:
        console.print(f"  [yellow]无可用直链将跳过[/yellow] [cyan]{len(no_url)}[/cyan] 首：")
        for i, t in enumerate(no_url, 1):
            console.print(f"    [dim]{i:>3}.[/dim] {t.artist_text} - {t.name}")

    if pruned:
        console.print(f"  [red]将清理远端已删除曲目[/red] [cyan]{len(pruned)}[/cyan] 首：{', '.join(map(str, pruned))}")

    if renames:
        console.print("  [cyan]歌单目录将重命名：[/cyan]")
        for _pid, old, new in renames:
            console.print(f"    [dim]-[/dim] {old} → {new}")

    if moves:
        console.print(f"  [cyan]歌单归属调整：[/cyan][cyan]{len(moves)}[/cyan] 首曲目的 library 链接将移动")

    if stale_index:
        console.print(f"  [yellow]将清理 {stale_index} 条本地文件缺失的过期索引[/yellow]")


def render_pipeline_result(result: PipelineResult, *, dry_run: bool, command: str) -> None:
    """pipeline 运行结束后的汇总输出。"""
    if dry_run and command != "process":
        if result.dry_run_plan:
            console.print(
                f"  从 [cyan]{result.dry_run_plan.get('playlist_count', '?')}[/cyan] 个歌单同步 "
                f"[cyan]{result.dry_run_plan.get('track_count', '?')}[/cyan] 首（[bold yellow]dry-run 预览[/bold yellow]）"
            )
            render_dry_run_plan(result.dry_run_plan)
        n_new = len((result.dry_run_plan or {}).get("with_url") or [])
        if n_new and command != "pull":
            console.print(f"  [dim]随后将进入后处理：新下载的 {n_new} 首曲目（转码/元数据/歌词/硬链接）[/dim]")
        console.print("  [bold yellow]dry-run 结束：未下载、未修改任何文件[/bold yellow]")
        return
    if command != "process" or result.track_count:
        render_sync_summary(result.track_count, result.playlist_count, result.downloaded, result.pruned)
    if command != "pull":
        if result.processed:
            console.print(f"  [green]处理完成 {result.processed} 首[/green]")
    ok("完成")


def render_link_result(counts: tuple[int, int], *, dry_run: bool) -> None:
    """link（--only-link）结果输出。"""
    linked, playlist_count = counts
    if dry_run:
        console.print(
            f"  [bold yellow]dry-run 预览[/bold yellow]：将创建 [cyan]{linked}[/cyan] 个硬链接"
            f"（涉及 [cyan]{playlist_count}[/cyan] 个歌单）"
        )
        return
    if linked:
        console.print(f"  链接完成：[cyan]{linked}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单")
    else:
        console.print("[dim]所有 library 链接均已就绪[/dim]")

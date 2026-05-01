"""Graceful TUI progress display inspired by pnpm / uv / claude code.

Uses Rich to render live-updating progress bars and status lines
that are clean, minimal, and aesthetically pleasing.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.style import Style
from rich.table import Table
from rich.text import Text

# Use stderr so progress output doesn't interfere with stdout pipes / redirects.
console = Console(stderr=True)


class BatchProgress:
    """Live progress bar for batch operations (download, process, etc.).

    Renders a two-line display: the current item name on top, and a
    real-time progress bar (spinner + phase + bar + percentage + elapsed)
    on the bottom.  When the batch finishes, the live display is removed
    and a compact summary line is printed — just like pnpm / uv.

    Usage::

        with BatchProgress(total=27, phase="下载中") as bp:
            for item in items:
                ok = do_work(item)
                bp.advance(ok, idx, item.name)
    """

    def __init__(self, total: int, phase: str) -> None:
        if total < 1:
            raise ValueError(f"total must be >= 1, got {total}")

        self.total = total
        self.phase = phase
        self.done = 0
        self.failed = 0
        self._start = 0.0  # set on __enter__

        self._filename = "准备中..."
        self._completed = 0

        self._spinner = Spinner("dots", style=Style(color="cyan"))
        self._bar = ProgressBar(
            total=total,
            completed=0,
            width=None,
            style=Style(color="grey50", dim=True),
            complete_style=Style(color="cyan"),
            finished_style=Style(color="green"),
        )

        self._live = Live(self._render(), console=console, transient=True)

    # ---- context manager interface -------------------------------------------

    def __enter__(self) -> BatchProgress:
        self._start = time.perf_counter()
        self._live.start()
        return self

    def __exit__(self, *exc_args: object) -> None:
        self._live.stop()
        elapsed = time.perf_counter() - self._start
        _print_batch_summary(self.phase, self.done, self.total, self.failed, elapsed)

    # ---- public helpers ------------------------------------------------------

    def advance(self, success: bool, idx: int, item_name: str) -> None:
        """Advance the bar by one item.

        Parameters
        ----------
        success:
            Whether the item was processed successfully.
        idx:
            1-based index of the item (for display only).
        item_name:
            Short display name of the current item.
        """
        if success:
            self.done += 1
        else:
            self.failed += 1

        self._filename = item_name
        self._completed = self.done + self.failed
        self._bar.completed = self._completed
        self._live.update(self._render())

    # ---- render --------------------------------------------------------------

    def _render(self) -> Group:
        elapsed = time.perf_counter() - self._start
        elapsed_delta = timedelta(seconds=int(elapsed))

        # Line 1: spinner + phase count + bar + percentage + elapsed
        phase_text = Text.from_markup(f"[bold cyan]{self.phase}[/bold cyan]  [dim]{self._completed}/{self.total}[/dim]")
        if self.failed:
            phase_text.append(f"  ✗{self.failed}", style="red")

        pct = Text(f"{self._bar.percentage_completed:>3.0f}%", style=Style(color="cyan", dim=True))
        elapsed_text = Text(str(elapsed_delta), style="dim")

        grid = Table.grid(padding=(0, 1))
        grid.add_column()  # spinner
        grid.add_column(no_wrap=True)  # phase
        grid.add_column(ratio=1)  # bar — fills remaining width
        grid.add_column()  # percentage
        grid.add_column()  # elapsed
        grid.add_row(self._spinner, phase_text, self._bar, pct, elapsed_text)

        # Line 2: current item name
        bottom = Text(f"  └─ {self._filename}")

        return Group(grid, bottom)


# ── Single-operation spinner ──────────────────────────────────────────────────


@contextmanager
def status(description: str) -> Iterator[None]:
    """Context manager that shows a spinner while the task runs.

    On success the spinner is replaced by a green ``●``; on failure by a
    red ``✘``.  The line stays in the terminal permanently.

    Usage::

        with status("正在获取歌单"):
            tracks = api.get_playlist(...)
    """
    progress = Progress(
        SpinnerColumn("dots", style=Style(color="cyan")),
        TextColumn("{task.description}"),
        console=console,
        transient=True,
    )
    progress.add_task(f" {description}", total=None)
    progress.start()
    try:
        yield
    except BaseException:
        progress.stop()
        console.print(f"[red]✘[/red] {description}")
        raise
    else:
        progress.stop()
        console.print(f"[green]●[/green] {description}")


# ── Plain status line (no spinner, just a ✓/✘ prefix) ────────────────────────


def ok(message: str) -> None:
    """Print a green ``●`` prefixed status line."""
    console.print(f"[green]●[/green] {message}")


def fail(message: str) -> None:
    """Print a red ``✘`` prefixed status line."""
    console.print(f"[red]✘[/red] {message}")


def info(message: str) -> None:
    """Print a dim info line (no prefix)."""
    console.print(f"  [dim]{message}[/dim]")


# ── Internals ─────────────────────────────────────────────────────────────────


def _print_batch_summary(
    phase: str,
    done: int,
    total: int,
    failed: int,
    elapsed: float,
) -> None:
    """Print a one-line summary after a batch completes."""
    parts = [f"● [bold]{phase}[/bold]"]
    parts.append(f"[dim][cyan]{done}[/cyan]/[cyan]{total}[/cyan] 项[/dim]")
    if failed:
        parts.append(f"[red]失败={failed}[/red]")
    parts.append(f"[dim][cyan]{elapsed:.1f}[/cyan]s[/dim]")
    console.print("  ".join(parts), highlight=False)

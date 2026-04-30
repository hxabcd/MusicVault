"""Graceful TUI progress display inspired by pnpm / uv / claude code.

Uses Rich to render live-updating progress bars and status lines
that are clean, minimal, and aesthetically pleasing.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.style import Style

# Use stderr so progress output doesn't interfere with stdout pipes / redirects.
console = Console(stderr=True)


class BatchProgress:
    """Live progress bar for batch operations (download, process, etc.).

    Displays a real-time updating bar with spinner, percentage, item name,
    failure count, and elapsed time.  When the batch finishes, the live bar
    is removed and a compact summary line is printed — just like pnpm / uv.

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
        self._start = time.perf_counter()

        self._progress = Progress(
            SpinnerColumn("dots", style=Style(color="cyan")),
            TextColumn("{task.description}"),
            BarColumn(
                bar_width=None,
                style=Style(color="grey50", dim=True),
                complete_style=Style(color="cyan"),
                finished_style=Style(color="green"),
            ),
            TextColumn(
                "[progress.percentage]{task.percentage:>3.0f}%",
                style=Style(color="cyan", dim=True),
            ),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        self._task = self._progress.add_task(
            f"[white]准备中...[/white]\n[bold cyan]{phase}[/bold cyan]  [dim]0/{total}[/dim]",
            total=total,
        )

    # ---- context manager interface -------------------------------------------

    def __enter__(self) -> BatchProgress:
        self._progress.start()
        return self

    def __exit__(self, *exc_args: object) -> None:
        self._progress.stop()
        # Print a permanent summary after the transient bar is cleared.
        elapsed = time.perf_counter() - self._start
        _print_batch_summary(self.phase, self.done, self.failed, elapsed)

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

        completed = self.done + self.failed
        desc = f"[white]{item_name}[/white]\n[bold cyan]{self.phase}[/bold cyan]  [dim]{completed}/{self.total}[/dim]"
        if self.failed:
            desc += f"  [red]✗{self.failed}[/red]"
        self._progress.update(self._task, advance=1, description=desc)


# ── Single-operation spinner ──────────────────────────────────────────────────


@contextmanager
def status(description: str) -> Iterator[None]:
    """Context manager that shows a spinner while the task runs.

    On success the spinner is replaced by a green ``✔``; on failure by a
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
        console.print(f" [red]✘[/red] {description}")
        raise
    else:
        progress.stop()
        console.print(f" [green]✔[/green] {description}")


# ── Plain status line (no spinner, just a ✓/✘ prefix) ────────────────────────


def ok(message: str) -> None:
    """Print a green ``✔`` prefixed status line."""
    console.print(f" [green]✔[/green] {message}")


def fail(message: str) -> None:
    """Print a red ``✘`` prefixed status line."""
    console.print(f" [red]✘[/red] {message}")


def info(message: str) -> None:
    """Print a dim info line (no prefix)."""
    console.print(f"  [dim]{message}[/dim]")


# ── Internals ─────────────────────────────────────────────────────────────────


def _print_batch_summary(
    phase: str,
    done: int,
    failed: int,
    elapsed: float,
) -> None:
    """Print a one-line summary after a batch completes."""
    parts = [f"  [green]✔[/green] [bold]{phase}[/bold]"]
    parts.append(f"[dim]{done}项[/dim]")
    if failed:
        parts.append(f"[red]失败={failed}[/red]")
    parts.append(f"[dim]{elapsed:.1f}s[/dim]")
    console.print("  ".join(parts))

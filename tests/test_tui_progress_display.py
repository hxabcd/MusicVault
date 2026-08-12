"""shared/tui_progress.py 进度展示测试。

覆盖：BatchProgress（构造校验、上下文管理、成功/失败推进计数、
Rich Group 渲染结构与 ETA 格式分支）、status 上下文管理器
（成功/异常路径）、ok/fail/info 状态行与 transient_section 终端控制序列。

Rich 输出断言采用捕获到 StringIO 的文本子串（非终端 Console 不渲染动画）。
"""

from __future__ import annotations

import io
import time
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.table import Table
from rich.text import Text

from musicvault.shared import tui_progress
from musicvault.shared.tui_progress import BatchProgress


def _capture_console(monkeypatch) -> io.StringIO:
    """把模块级 console 替换为写入 StringIO 的非终端 Console。"""
    stream = io.StringIO()
    monkeypatch.setattr(tui_progress, "console", Console(file=stream))
    return stream


class TestBatchProgressInit:
    def test_total_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="total must be >= 1"):
            BatchProgress(total=0, phase="下载中")

    def test_total_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="total must be >= 1"):
            BatchProgress(total=-3, phase="下载中")

    def test_initial_state(self) -> None:
        bp = BatchProgress(total=3, phase="处理中")
        assert bp.total == 3
        assert bp.phase == "处理中"
        assert bp.done == 0
        assert bp.failed == 0
        assert bp._completed == 0
        assert bp._filename == "准备中..."
        assert bp._bar.total == 3
        assert bp._bar.completed == 0


class TestBatchProgressContextManager:
    def test_enter_returns_self_and_records_start(self, monkeypatch) -> None:
        _capture_console(monkeypatch)
        bp = BatchProgress(total=2, phase="下载中")
        entered = bp.__enter__()
        assert entered is bp
        assert bp._start > 0
        bp.__exit__(None, None, None)

    def test_exit_prints_summary_with_failures(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        bp = BatchProgress(total=2, phase="下载中")
        with bp:
            bp.advance(True, 1, "第一首.mp3")
            bp.advance(False, 2, "第二首.mp3")
        text = stream.getvalue()
        assert "下载中" in text
        assert "1/2 项" in text
        assert "失败=1" in text

    def test_exit_prints_summary_without_failures(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        bp = BatchProgress(total=2, phase="处理中")
        with bp:
            bp.advance(True, 1, "一.mp3")
            bp.advance(True, 2, "二.mp3")
        text = stream.getvalue()
        assert "处理中" in text
        assert "2/2 项" in text
        assert "失败" not in text


class TestBatchProgressAdvance:
    def test_success_increments_done(self) -> None:
        bp = BatchProgress(total=3, phase="下载中")
        bp.advance(True, 1, "一.mp3")
        assert bp.done == 1
        assert bp.failed == 0
        assert bp._completed == 1
        assert bp._filename == "一.mp3"
        assert bp._bar.completed == 1

    def test_failure_increments_failed(self) -> None:
        bp = BatchProgress(total=3, phase="下载中")
        bp.advance(False, 1, "坏.mp3")
        assert bp.done == 0
        assert bp.failed == 1
        assert bp._completed == 1
        assert bp._bar.completed == 1

    def test_mixed_progress_reaches_total(self) -> None:
        bp = BatchProgress(total=5, phase="下载中")
        for i, ok in enumerate((True, False, True, True, False), 1):
            bp.advance(ok, i, f"第{i}首.mp3")
        assert bp.done == 3
        assert bp.failed == 2
        assert bp._completed == 5
        assert bp._bar.completed == 5

    def test_advance_beyond_total_does_not_crash(self) -> None:
        bp = BatchProgress(total=2, phase="下载中")
        bp.advance(True, 1, "一.mp3")
        bp.advance(True, 2, "二.mp3")
        bp.advance(True, 3, "三.mp3")
        assert bp._completed == 3

    def test_update_called_with_rendered_group(self, monkeypatch) -> None:
        bp = BatchProgress(total=3, phase="下载中")
        captured: list[object] = []
        monkeypatch.setattr(bp._live, "update", captured.append)
        bp.advance(True, 1, "一.mp3")
        assert len(captured) == 1
        assert captured[0].renderables  # noqa: B018 -- 仅确认是可渲染对象（Group）


class TestBatchProgressRender:
    def _bp(self, *, completed: int = 0, failed: int = 0) -> BatchProgress:
        bp = BatchProgress(total=3, phase="下载中")
        bp._start = time.perf_counter() - 5.0
        bp._completed = completed
        bp._bar.completed = completed
        bp.failed = failed
        return bp

    def test_render_group_structure(self) -> None:
        bp = self._bp(completed=1)
        group = bp._render()
        assert len(group.renderables) == 2
        grid, bottom = group.renderables
        assert isinstance(grid, Table)
        assert isinstance(bottom, Text)
        assert bottom.plain == "  └─ 准备中..."

    def test_render_contains_phase_counts_and_elapsed(self) -> None:
        stream = io.StringIO()
        bp = self._bp(completed=1)
        Console(file=stream).print(bp._render())
        text = stream.getvalue()
        assert "下载中" in text
        assert "1/3" in text
        assert "33%" in text
        assert "0:00:05" in text
        assert "准备中..." in text

    def test_render_shows_failure_count(self) -> None:
        stream = io.StringIO()
        bp = self._bp(completed=2, failed=1)
        Console(file=stream).print(bp._render())
        assert "✗1" in stream.getvalue()

    def test_render_shows_current_filename(self) -> None:
        stream = io.StringIO()
        bp = self._bp(completed=1)
        bp._filename = "当前歌曲.mp3"
        Console(file=stream).print(bp._render())
        assert "当前歌曲.mp3" in stream.getvalue()


class TestRenderEta:
    def _bp(self, completed: int) -> BatchProgress:
        bp = BatchProgress(total=10, phase="下载中")
        bp._completed = completed
        bp._bar.completed = completed
        return bp

    def test_too_early_placeholder(self) -> None:
        assert self._bp(completed=1)._render_eta(10.0).plain == "~--"

    def test_zero_completed_placeholder(self) -> None:
        assert self._bp(completed=0)._render_eta(10.0).plain == "~--"

    def test_seconds(self) -> None:
        # 速率 = 4/2 = 2s/项，剩余 8 项 → 16s
        assert self._bp(completed=2)._render_eta(4.0).plain == "~16s"

    def test_minutes(self) -> None:
        # 速率 = 90/2 = 45s/项，剩余 8 项 → 360s → 6m00s
        assert self._bp(completed=2)._render_eta(90.0).plain == "~6m00s"

    def test_minutes_with_seconds(self) -> None:
        # 剩余 361s → 6m01s
        assert self._bp(completed=2)._render_eta(90.25).plain == "~6m01s"

    def test_hours(self) -> None:
        # 速率 = 3600/2 = 1800s/项，剩余 8 项 → 14400s → 4h00m
        assert self._bp(completed=2)._render_eta(3600.0).plain == "~4h00m"

    def test_hours_with_minutes(self) -> None:
        # 剩余 7300s → 2h01m
        assert self._bp(completed=2)._render_eta(1825.0).plain == "~2h01m"

    def test_minute_boundary(self) -> None:
        # 剩余恰好 60s → 进入分钟档
        assert self._bp(completed=2)._render_eta(15.0).plain == "~1m00s"

    def test_hour_boundary(self) -> None:
        # 剩余恰好 3600s → 进入小时档
        assert self._bp(completed=2)._render_eta(900.0).plain == "~1h00m"


class TestStatus:
    def test_success_prints_green_dot(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        with tui_progress.status("正在获取歌单"):
            pass
        text = stream.getvalue()
        assert "●" in text
        assert "正在获取歌单" in text

    def test_failure_prints_red_cross_and_reraises(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        with pytest.raises(RuntimeError, match="模拟失败"):
            with tui_progress.status("处理中"):
                raise RuntimeError("模拟失败")
        text = stream.getvalue()
        assert "✘" in text
        assert "处理中" in text


class TestStatusLines:
    def test_ok(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        tui_progress.ok("下载完成")
        assert stream.getvalue() == "● 下载完成\n"

    def test_fail(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        tui_progress.fail("下载失败")
        assert stream.getvalue() == "✘ 下载失败\n"

    def test_info(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        tui_progress.info("正在处理")
        assert stream.getvalue() == "  正在处理\n"


class TestTransientSection:
    def _fake_sys(self, is_tty: bool) -> tuple[SimpleNamespace, list[str]]:
        writes: list[str] = []
        stderr = SimpleNamespace(isatty=lambda: is_tty, write=writes.append, flush=lambda: None)
        return SimpleNamespace(stderr=stderr), writes

    def test_tty_writes_save_and_restore(self, monkeypatch) -> None:
        fake, writes = self._fake_sys(True)
        monkeypatch.setattr(tui_progress, "_sys", fake)
        with tui_progress.transient_section():
            assert writes == ["\033[s"]
        assert writes == ["\033[s", "\033[u\033[J"]

    def test_non_tty_writes_nothing(self, monkeypatch) -> None:
        fake, writes = self._fake_sys(False)
        monkeypatch.setattr(tui_progress, "_sys", fake)
        with tui_progress.transient_section():
            assert writes == []
        assert writes == []

    def test_exception_still_clears(self, monkeypatch) -> None:
        fake, writes = self._fake_sys(True)
        monkeypatch.setattr(tui_progress, "_sys", fake)
        with pytest.raises(RuntimeError, match="中断"):
            with tui_progress.transient_section():
                raise RuntimeError("中断")
        assert writes == ["\033[s", "\033[u\033[J"]

    def test_exception_non_tty_writes_nothing(self, monkeypatch) -> None:
        fake, writes = self._fake_sys(False)
        monkeypatch.setattr(tui_progress, "_sys", fake)
        with pytest.raises(RuntimeError):
            with tui_progress.transient_section():
                raise RuntimeError("失败")
        assert writes == []


class TestPrintBatchSummary:
    def test_with_failures(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        tui_progress._print_batch_summary("下载中", done=2, total=3, failed=1, elapsed=3.25)
        assert stream.getvalue() == "● 下载中  2/3 项  失败=1  3.2s\n"

    def test_without_failures(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        tui_progress._print_batch_summary("处理中", done=3, total=3, failed=0, elapsed=0.5)
        assert stream.getvalue() == "● 处理中  3/3 项  0.5s\n"

    def test_zero_done(self, monkeypatch) -> None:
        stream = _capture_console(monkeypatch)
        tui_progress._print_batch_summary("下载中", done=0, total=1, failed=0, elapsed=0.0)
        assert stream.getvalue() == "● 下载中  0/1 项  0.0s\n"

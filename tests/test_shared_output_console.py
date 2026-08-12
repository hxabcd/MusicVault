"""shared/output.py 用户向输出函数测试。

覆盖：success/error/warn/info 的渲染文本与 highlight 参数；
通过替换模块级 console（真实 Rich Console 写入 StringIO，
以及记录调用参数的假 console）验证 console 可替换性。
"""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console

from musicvault.shared import output


class _RecordingConsole:
    """记录 print 调用参数的假 console。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def print(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def _string_console() -> Console:
    """返回写入 StringIO 的非终端 Console，便于断言纯文本输出。"""
    return Console(file=io.StringIO())


class TestSuccess:
    def test_rendered_text(self, monkeypatch) -> None:
        console = _string_console()
        monkeypatch.setattr(output, "console", console)
        output.success("下载完成")
        assert console.file.getvalue() == "● 下载完成\n"

    def test_markup_highlight_disabled(self, monkeypatch) -> None:
        recorder = _RecordingConsole()
        monkeypatch.setattr(output, "console", recorder)
        output.success("已添加歌单：[bold]{name}[/bold]")
        (args, kwargs) = recorder.calls[0]
        assert args == ("[green]●[/green] 已添加歌单：[bold]{name}[/bold]",)
        assert kwargs == {"highlight": False}


class TestError:
    def test_rendered_text(self, monkeypatch) -> None:
        console = _string_console()
        monkeypatch.setattr(output, "console", console)
        output.error("缺少 cookie")
        assert console.file.getvalue() == "● 缺少 cookie\n"

    def test_markup_highlight_disabled(self, monkeypatch) -> None:
        recorder = _RecordingConsole()
        monkeypatch.setattr(output, "console", recorder)
        output.error("缺少 [bold]cookie[/bold]")
        (args, kwargs) = recorder.calls[0]
        assert args == ("[red]●[/red] 缺少 [bold]cookie[/bold]",)
        assert kwargs == {"highlight": False}


class TestWarn:
    def test_rendered_text(self, monkeypatch) -> None:
        console = _string_console()
        monkeypatch.setattr(output, "console", console)
        output.warn("未检测到 ffmpeg")
        assert console.file.getvalue() == "● 未检测到 ffmpeg\n"

    def test_markup_highlight_disabled(self, monkeypatch) -> None:
        recorder = _RecordingConsole()
        monkeypatch.setattr(output, "console", recorder)
        output.warn("版本 [dim]过旧[/dim]")
        (args, kwargs) = recorder.calls[0]
        assert args == ("[yellow]●[/yellow] 版本 [dim]过旧[/dim]",)
        assert kwargs == {"highlight": False}


class TestInfo:
    def test_rendered_text(self, monkeypatch) -> None:
        console = _string_console()
        monkeypatch.setattr(output, "console", console)
        output.info("将同步 5 个歌单")
        assert console.file.getvalue() == "  将同步 5 个歌单\n"

    def test_markup_highlight_disabled(self, monkeypatch) -> None:
        recorder = _RecordingConsole()
        monkeypatch.setattr(output, "console", recorder)
        output.info("路径 [bold]/x[/bold]")
        (args, kwargs) = recorder.calls[0]
        assert args == ("  路径 [bold]/x[/bold]",)
        assert kwargs == {"highlight": False}


class TestConsoleReplaceability:
    def test_all_functions_use_module_console(self, monkeypatch) -> None:
        # 替换模块级 console 后四个函数都输出到新 console
        stream = io.StringIO()
        monkeypatch.setattr(output, "console", Console(file=stream))
        output.success("成功")
        output.error("错误")
        output.warn("警告")
        output.info("信息")
        text = stream.getvalue()
        assert "成功" in text
        assert "错误" in text
        assert "警告" in text
        assert "信息" in text

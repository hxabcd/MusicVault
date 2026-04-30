"""用户向输出模块。

与 `logging` 明确分离：本模块所有函数面向终端用户，
在一般模式和 verbose 模式下始终显示。

用法::

    from musicvault.shared.output import success, error, warn, info

    success("下载完成")
    error("缺少 cookie")
    warn("未检测到 ffmpeg")
    info("将同步 5 个歌单")
"""

from __future__ import annotations

from musicvault.shared.tui_progress import console


def success(msg: str) -> None:
    """绿色 ✔ 前缀的成功消息。"""
    console.print(f" [green]✔[/green] {msg}")


def error(msg: str) -> None:
    """红色 ✘ 前缀的错误消息。"""
    console.print(f"[red]✘ {msg}[/red]")


def warn(msg: str) -> None:
    """黄色 ⚠ 前缀的警告消息。"""
    console.print(f"[yellow]⚠ {msg}[/yellow]")


def info(msg: str) -> None:
    """灰色 dim 信息行（无前缀）。"""
    console.print(f"  {msg}")

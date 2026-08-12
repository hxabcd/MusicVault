"""shared.output 补充单测：用户向输出的成功/警告消息渲染。"""

from __future__ import annotations

from musicvault.shared.output import error, info, success, warn


def test_output_functions_render_prefix_markers(capsys) -> None:
    """success/error/warn 带彩色圆点前缀，info 为缩进文本。"""
    success("下载完成")
    warn("未检测到 ffmpeg")
    error("缺少 cookie")
    info("将同步 5 个歌单")

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "下载完成" in combined
    assert "未检测到 ffmpeg" in combined
    assert "缺少 cookie" in combined
    assert "将同步 5 个歌单" in combined

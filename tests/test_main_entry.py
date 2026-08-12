"""`python -m musicvault` 模块入口（__main__.py）测试。

入口模块在导入时即执行 `raise SystemExit(main())`，故用 runpy 以
__main__ 名义运行，验证 SystemExit 退出码与帮助输出。
"""

from __future__ import annotations

import runpy
import types

import pytest


def _fake_signal_module() -> types.ModuleType:
    """替换 cli.main 中的 signal 模块，避免测试注册真实 SIGINT 处理器。"""
    fake = types.ModuleType("signal")
    setattr(fake, "SIGINT", 2)
    setattr(fake, "signal", lambda *_: None)
    return fake


def test_main_entry_no_args_prints_help_and_exits_zero(monkeypatch, capsys) -> None:
    """无参数运行入口：打印帮助并以 0 退出。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("sys.argv", ["musicvault"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("musicvault.__main__", run_name="__main__")

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "usage" in (captured.out + captured.err).lower()


def test_main_entry_help_subcommand_exits_zero(monkeypatch, capsys) -> None:
    """help 子命令入口：打印帮助并以 0 退出。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("sys.argv", ["musicvault", "help"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("musicvault.__main__", run_name="__main__")

    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()

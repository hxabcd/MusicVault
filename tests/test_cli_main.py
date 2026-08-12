"""CLI 命令收敛测试（Task 15）。

sync 的 --no-distribute/--only-distribute 互斥选项、pull/process 删除、
target-sync 改名 distribute（--preset/--dry-run 保留）、presets 双列表输出。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from musicvault.cli.main import build_parser, main


def test_sync_parser_has_distribute_options() -> None:
    """sync 同时支持 --no-distribute 与 --only-distribute。"""
    parser = build_parser()
    args = parser.parse_args(["sync", "--no-distribute"])
    assert args.no_distribute is True
    assert args.only_distribute is False

    args = parser.parse_args(["sync", "--only-distribute"])
    assert args.only_distribute is True
    assert args.no_distribute is False


def test_sync_distribute_options_are_mutually_exclusive() -> None:
    """--no-distribute 与 --only-distribute 同用报 argparse 错误（SystemExit）。"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "--no-distribute", "--only-distribute"])


def test_pull_and_process_subcommands_removed() -> None:
    """pull/process 子命令已删除：解析报 SystemExit。"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["pull"])
    with pytest.raises(SystemExit):
        parser.parse_args(["process"])


def test_target_sync_subcommand_removed() -> None:
    """旧名 target-sync 已改名 distribute：解析报 SystemExit。"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["target-sync"])


def test_distribute_subcommand_exists() -> None:
    """distribute 子命令存在，--preset/--dry-run 保留。"""
    parser = build_parser()
    args = parser.parse_args(["distribute", "--preset", "hardlink", "--dry-run"])
    assert args.command == "distribute"
    assert args.preset == ["hardlink"]
    assert args.dry_run is True


def _fake_signal_module() -> types.ModuleType:
    """替换 cli.main 中的 signal 模块，避免测试注册真实 SIGINT 处理器。"""
    fake = types.ModuleType("signal")
    setattr(fake, "SIGINT", 2)
    setattr(fake, "signal", lambda *_: None)
    return fake


def test_preset_list_command_lists_presets(tmp_path: Path, monkeypatch, capfd) -> None:
    """preset list 只列出 preset 注册项（内置 archive），不含 sync_target。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    ws = tmp_path / "ws"

    code = main(["preset", "list", "--config", str(tmp_path / "config.json"), "--workspace", str(ws)])

    assert code == 0
    captured = capfd.readouterr()
    out = captured.out + captured.err
    assert "archive" in out
    assert "hardlink" not in out


def test_target_list_command_lists_targets(tmp_path: Path, monkeypatch, capfd) -> None:
    """target list 只列出 sync_target 注册项（内置 hardlink），不含 preset。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    ws = tmp_path / "ws"

    code = main(["target", "list", "--config", str(tmp_path / "config.json"), "--workspace", str(ws)])

    assert code == 0
    captured = capfd.readouterr()
    out = captured.out + captured.err
    assert "hardlink" in out
    assert "archive" not in out

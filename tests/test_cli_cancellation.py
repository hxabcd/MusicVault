"""CLI 层 KeyboardInterrupt 处理测试。

主流程（sync / process 等 pipeline 路径）中断返回退出码 130 且不抛裸异常；
二维码扫码等待阶段取消登录返回退出码 2。
"""

from __future__ import annotations

import types
from pathlib import Path

from musicvault.cli.main import main


class _InterruptingService:
    """模拟运行中途被 Ctrl+C 打断的 pipeline 服务。"""

    def run_pipeline(self, **kwargs: object) -> None:
        raise KeyboardInterrupt

    def link_only(self, **kwargs: object) -> None:
        raise KeyboardInterrupt


def _fake_signal_module() -> types.ModuleType:
    """替换 cli.main 中的 signal 模块，避免测试注册真实 SIGINT 处理器。"""
    fake = types.ModuleType("signal")
    fake.SIGINT = 2
    fake.signal = lambda signum, handler: None
    return fake


def test_sync_keyboard_interrupt_returns_130(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr(
        "musicvault.application.bootstrap.build_pipeline", lambda cfg, dry_run=False: _InterruptingService()
    )

    code = main(["sync", "--config", str(tmp_path / "config.json"), "--cookie", "fake-cookie"])

    assert code == 130


def test_process_keyboard_interrupt_returns_130(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr(
        "musicvault.application.bootstrap.build_pipeline", lambda cfg, dry_run=False: _InterruptingService()
    )

    code = main(["process", "--config", str(tmp_path / "config.json"), "--cookie", "fake-cookie"])

    assert code == 130


def test_interactive_login_keyboard_interrupt_cancels_with_exit_code_2(tmp_path: Path, monkeypatch) -> None:
    # 二维码扫码等待阶段 Ctrl+C → 优雅取消登录并返回退出码 2，不抛裸异常
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "1")
    monkeypatch.setattr("musicvault.cli.main._render_qrcode", lambda url: "[qr]")

    class _FakeApi:
        def get_qrcode_unikey(self) -> str:
            return "unikey"

        def get_qrcode_url(self, unikey: str) -> str:
            return "https://example.com/qr"

        def check_qrcode(self, unikey: str) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda cfg: _FakeApi())

    code = main(["sync", "--config", str(tmp_path / "config.json")])

    assert code == 2


def test_interactive_login_menu_keyboard_interrupt_returns_2(tmp_path: Path, monkeypatch) -> None:
    # 登录菜单（首个 input）处 Ctrl+C → 优雅取消登录并返回退出码 2，不抛裸异常
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    def _interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)
    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda cfg: object())

    code = main(["sync", "--config", str(tmp_path / "config.json")])

    assert code == 2

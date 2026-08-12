"""CLI 层 KeyboardInterrupt 处理测试。

主流程（sync / sync --only-distribute 等 pipeline 路径）中断返回退出码 130
且不抛裸异常；二维码扫码等待阶段取消登录返回退出码 2。
"""

from __future__ import annotations

import types
from pathlib import Path

from musicvault.cli.main import main


class _InterruptingService:
    """模拟运行中途被 Ctrl+C 打断的 pipeline 服务（cookie 位置参数 + 关键字选项）。"""

    def run_pipeline(self, cookie: str, **kwargs: object) -> None:
        del cookie, kwargs
        raise KeyboardInterrupt


def _stub_build_pipeline(cfg, dry_run: bool = False) -> _InterruptingService:
    del cfg, dry_run
    return _InterruptingService()


def _fake_signal_module() -> types.ModuleType:
    """替换 cli.main 中的 signal 模块，避免测试注册真实 SIGINT 处理器。"""
    fake = types.ModuleType("signal")
    setattr(fake, "SIGINT", 2)
    setattr(fake, "signal", lambda *_: None)
    return fake


def test_sync_keyboard_interrupt_returns_130(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", _stub_build_pipeline)

    code = main(["sync", "--config", str(tmp_path / "config.json"), "--cookie", "fake-cookie"])

    assert code == 130


def test_sync_only_distribute_keyboard_interrupt_returns_130(tmp_path: Path, monkeypatch) -> None:
    # --only-distribute 走同一 pipeline 路径，Ctrl+C 同样优雅返回 130
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", _stub_build_pipeline)

    code = main(["sync", "--config", str(tmp_path / "config.json"), "--cookie", "fake-cookie", "--only-distribute"])

    assert code == 130


def test_interactive_login_keyboard_interrupt_cancels_with_exit_code_2(tmp_path: Path, monkeypatch) -> None:
    # 二维码扫码等待阶段 Ctrl+C → 优雅取消登录并返回退出码 2，不抛裸异常
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("builtins.input", lambda *_: "1")
    monkeypatch.setattr("musicvault.cli.main._render_qrcode", lambda _: "[qr]")

    class _FakeApi:
        def get_qrcode_unikey(self) -> str:
            return "unikey"

        def get_qrcode_url(self, _: str) -> str:
            return "https://example.com/qr"

        def check_qrcode(self, _: str) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda _: _FakeApi())

    code = main(["sync", "--config", str(tmp_path / "config.json")])

    assert code == 2


def test_interactive_login_menu_keyboard_interrupt_returns_2(tmp_path: Path, monkeypatch) -> None:
    # 登录菜单（首个 input）处 Ctrl+C → 优雅取消登录并返回退出码 2，不抛裸异常
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    def _interrupt(_: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)
    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda _: object())

    code = main(["sync", "--config", str(tmp_path / "config.json")])

    assert code == 2

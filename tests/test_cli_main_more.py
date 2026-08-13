"""CLI 主入口 main() 补充路径测试。

覆盖：无参数帮助、help 子命令、init（--cookie / 已登录 / 配置自动生成）、
presets 加载失败退出码 2、distribute 成功与 --workspace/--preset 传递、
sync 成功（fake pipeline）与 --no-distribute/--only-distribute/--dry-run/
--force/--workspace 参数传递、sync 首次登录引导分支、add/list 命令转发、
_ensure_cookie 非交互路径、_configure_logs 与双击 Ctrl+C 信号处理器。
交互式二维码登录不测。
"""

from __future__ import annotations

import argparse
import logging
import runpy
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from musicvault.application.pipeline_use_case import PipelineResult
from musicvault.application.sync_engine import SyncRunResult
from musicvault.adapters.processors.downloader import RetryBudgetExceeded
from musicvault.cli import main as main_module
from musicvault.cli.main import main
from musicvault.core.config import Config
from musicvault.domain.models import Playlist
from musicvault.domain.operations import OperationStatus


def _fake_signal_module() -> types.ModuleType:
    """替换 cli.main 中的 signal 模块，避免测试注册真实 SIGINT 处理器。"""
    fake = types.ModuleType("signal")
    setattr(fake, "SIGINT", 2)
    setattr(fake, "signal", lambda *_: None)
    return fake


class _FakePlaylistUseCase:
    """main 转发场景用的内存 fake PlaylistUseCase（add/list 所需能力）。"""

    def __init__(self, playlists: list[Playlist]) -> None:
        self.playlists = {pl.id: pl for pl in playlists}

    def list_playlists(self) -> list[Playlist]:
        return list(self.playlists.values())

    def has_playlist(self, playlist_id: int) -> bool:
        return playlist_id in self.playlists

    def add_playlist(self, playlist_id: int, name: str = "") -> None:
        self.playlists[playlist_id] = Playlist(playlist_id, name, ())


class _PipelineRecorder:
    """记录 build_pipeline 入参与 run_pipeline 调用参数，返回固定结果。"""

    def __init__(self) -> None:
        self.cfg: Config | None = None
        self.dry_run = False
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.result = PipelineResult()

    def build(self, cfg: Config, dry_run: bool = False) -> "_PipelineRecorder":
        self.cfg = cfg
        self.dry_run = dry_run
        return self

    def run_pipeline(self, cookie: str, **kwargs: object) -> PipelineResult:
        self.calls.append((cookie, kwargs))
        return self.result


class _FakeDistributePipeline:
    """distribute 成功路径 fake：记录 selected 并返回成功结果。"""

    def __init__(self) -> None:
        self.selected: set[str] | None = None

    def run(self, *, selected: set[str] | None = None) -> SyncRunResult:
        self.selected = selected
        return SyncRunResult(snapshot_hash="b" * 64, presets=(), status=OperationStatus.SUCCEEDED)


def _all_output(capfd) -> str:
    captured = capfd.readouterr()
    return captured.out + captured.err


# -- 帮助路径 ----------------------------------------------------------------


def test_main_no_args_prints_help_and_returns_0(monkeypatch, capfd) -> None:
    """无参数：打印帮助并返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    assert main([]) == 0
    assert "usage" in capfd.readouterr().out.lower()


def test_main_help_subcommand_prints_help(monkeypatch, capfd) -> None:
    """help 子命令（无 subcommand）：打印帮助并返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    assert main(["help"]) == 0
    assert "usage" in capfd.readouterr().out.lower()


def test_main_help_with_subcommand_exits_zero(monkeypatch) -> None:
    """help <子命令>：经 argparse --help 输出子命令帮助并以 0 退出。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    with pytest.raises(SystemExit) as exc:
        main(["help", "sync"])
    assert exc.value.code == 0


# -- init ---------------------------------------------------------------------


def test_init_with_cookie_saves_config(tmp_path: Path, monkeypatch, caplog) -> None:
    """init --cookie：写入配置文件并返回 0；缺失配置被自动生成后加载。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    cfg_file = tmp_path / "config.json"

    with caplog.at_level(logging.INFO, logger="musicvault.cli.main"):
        code = main(["init", "--config", str(cfg_file), "--cookie", "ck-123", "-v"])

    assert code == 0
    assert '"cookie": "ck-123"' in cfg_file.read_text(encoding="utf-8")
    assert any("已加载配置文件" in record.message for record in caplog.records)


def test_init_already_logged_in_returns_0(tmp_path: Path, monkeypatch, capfd) -> None:
    """init 且配置已有 cookie：提示已登录并返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"cookie": "existing", "workspace": "./workspace"}', encoding="utf-8")

    code = main(["init", "--config", str(cfg_file)])

    assert code == 0
    assert "已登录" in _all_output(capfd)


# -- presets / distribute -------------------------------------------------------


def test_preset_list_load_failure_returns_2(tmp_path: Path, monkeypatch, capfd) -> None:
    """preset list 加载失败：输出错误并返回 2。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    def _boom(cfg: object) -> None:
        del cfg
        raise RuntimeError("脚本语法错误")

    monkeypatch.setattr("musicvault.application.bootstrap.build_runtime", _boom)

    code = main(["preset", "list", "--config", str(tmp_path / "config.json")])

    assert code == 2
    assert "加载失败" in _all_output(capfd)


def test_distribute_success_returns_0(tmp_path: Path, monkeypatch, capfd) -> None:
    """distribute 成功：渲染结果并输出完成信息，返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    holder: dict = {"cfg": None}
    pipeline = _FakeDistributePipeline()

    def _build(cfg: Config, *, dry_run: bool = False) -> _FakeDistributePipeline:
        holder["cfg"] = cfg
        del dry_run
        return pipeline

    monkeypatch.setattr("musicvault.application.bootstrap.build_distribute_pipeline", _build)

    code = main(["distribute", "--config", str(tmp_path / "config.json")])

    assert code == 0
    assert "分发完成" in _all_output(capfd)


def test_distribute_workspace_and_preset_selection(tmp_path: Path, monkeypatch) -> None:
    """distribute --workspace 切换工作目录，--preset 传入 selected。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    holder: dict = {"cfg": None}
    pipeline = _FakeDistributePipeline()

    def _build(cfg: Config, *, dry_run: bool = False) -> _FakeDistributePipeline:
        holder["cfg"] = cfg
        del dry_run
        return pipeline

    monkeypatch.setattr("musicvault.application.bootstrap.build_distribute_pipeline", _build)
    ws = tmp_path / "ws"

    code = main(
        ["distribute", "--config", str(tmp_path / "config.json"), "--workspace", str(ws), "--preset", "hardlink"]
    )

    assert code == 0
    assert str(holder["cfg"].workspace) == str(ws)
    assert pipeline.selected == {"hardlink"}


# -- sync -----------------------------------------------------------------------


def test_sync_success_returns_0(tmp_path: Path, monkeypatch, capfd) -> None:
    """sync 成功：pipeline 结果渲染汇总并返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    recorder = _PipelineRecorder()
    recorder.result = PipelineResult(downloaded=3, processed=2, pruned=1, track_count=10, playlist_count=1)
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", recorder.build)

    code = main(["sync", "--config", str(tmp_path / "config.json"), "--cookie", "ck"])

    assert code == 0
    assert recorder.calls[0][0] == "ck"
    assert recorder.calls[0][1]["distribute"] is True
    out = _all_output(capfd)
    assert "同步 10 首" in out
    assert "完成" in out


def test_sync_no_distribute_flag_passes_through(tmp_path: Path, monkeypatch) -> None:
    """sync --no-distribute：run_pipeline 收到 distribute=False。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    recorder = _PipelineRecorder()
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", recorder.build)

    code = main(["sync", "--config", str(tmp_path / "config.json"), "--cookie", "ck", "--no-distribute"])

    assert code == 0
    assert recorder.calls[0][1]["distribute"] is False
    assert recorder.calls[0][1]["only_distribute"] is False


def test_sync_only_distribute_dry_run_force_workspace(tmp_path: Path, monkeypatch) -> None:
    """sync --only-distribute/--dry-run/--force/--workspace：全部传递到 pipeline 与配置。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    recorder = _PipelineRecorder()
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", recorder.build)
    ws = tmp_path / "ws"

    code = main(
        [
            "sync",
            "--config",
            str(tmp_path / "config.json"),
            "--cookie",
            "ck",
            "--only-distribute",
            "--dry-run",
            "--force",
            "--workspace",
            str(ws),
        ]
    )

    assert code == 0
    assert recorder.calls[0][1]["only_distribute"] is True
    assert recorder.dry_run is True
    assert str(recorder.cfg.workspace) == str(ws)
    assert recorder.cfg.force is True


def test_sync_retry_budget_exceeded_returns_error(tmp_path: Path, monkeypatch, capfd) -> None:
    """sync 熔断中止：CLI 将异常转换为错误消息并返回非零退出码。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())

    class _FailingPipeline:
        def build(self, cfg, dry_run=False):
            return self

        def run_pipeline(self, cookie, **kwargs):
            raise RetryBudgetExceeded(6)

    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", _FailingPipeline().build)

    code = main(["sync", "--config", str(tmp_path / "config.json"), "--cookie", "ck"])

    assert code == 2
    out = _all_output(capfd)
    assert "同步失败" in out
    assert "连续重试" in out


def test_sync_first_login_without_playlists_shows_guidance(tmp_path: Path, monkeypatch, capfd) -> None:
    """sync 首次登录且无歌单：输出「下一步操作」引导并返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("musicvault.cli.main._ensure_cookie", lambda _args, _cfg: ("ck", True))
    use_case = _FakePlaylistUseCase([])
    monkeypatch.setattr("musicvault.application.bootstrap.build_playlist_use_case", lambda _cfg: use_case)

    code = main(["sync", "--config", str(tmp_path / "config.json")])

    assert code == 0
    assert "下一步操作" in _all_output(capfd)


def test_sync_first_login_with_existing_playlists_skips_guidance(tmp_path: Path, monkeypatch, capfd) -> None:
    """sync 首次登录但已有歌单：不输出引导，直接返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("musicvault.cli.main._ensure_cookie", lambda _args, _cfg: ("ck", True))
    use_case = _FakePlaylistUseCase([Playlist(1, "已有歌单", ())])
    monkeypatch.setattr("musicvault.application.bootstrap.build_playlist_use_case", lambda _cfg: use_case)

    code = main(["sync", "--config", str(tmp_path / "config.json")])

    assert code == 0
    assert "下一步操作" not in _all_output(capfd)


# -- 歌单命令转发 ----------------------------------------------------------------


def test_add_command_forwards_to_playlist_mgmt_then_pipeline(tmp_path: Path, monkeypatch, capfd) -> None:
    """add 成功：转发 handle_playlist_mgmt 登记歌单，随后继续执行 pipeline。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    use_case = _FakePlaylistUseCase([])
    monkeypatch.setattr("musicvault.application.bootstrap.build_playlist_use_case", lambda _cfg: use_case)

    class _Api:
        def login_with_cookie(self, cookie: str) -> None:
            del cookie

        def get_playlist_info(self, playlist_id: int) -> dict:
            del playlist_id
            return {"name": "测试歌单"}

    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda _cfg: _Api())
    recorder = _PipelineRecorder()
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", recorder.build)

    code = main(["add", "123", "--config", str(tmp_path / "config.json"), "--cookie", "ck"])

    assert code == 0
    assert use_case.playlists[123].name == "测试歌单"
    assert recorder.calls[0][0] == "ck"
    out = _all_output(capfd)
    assert "已添加歌单" in out
    assert "完成" in out


def test_add_invalid_input_returns_1_before_pipeline(tmp_path: Path, monkeypatch, capfd) -> None:
    """add 非法输入：handle 返回 1，main 直接返回 1 且不执行 pipeline。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    use_case = _FakePlaylistUseCase([])
    monkeypatch.setattr("musicvault.application.bootstrap.build_playlist_use_case", lambda _cfg: use_case)
    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda _cfg: object())
    recorder = _PipelineRecorder()
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", recorder.build)

    code = main(["add", "not-a-number", "--config", str(tmp_path / "config.json"), "--cookie", "ck"])

    assert code == 1
    assert recorder.calls == []
    assert "无法识别的歌单标识" in _all_output(capfd)


def test_list_command_forwards_without_pipeline(tmp_path: Path, monkeypatch, capfd) -> None:
    """list 命令：转发 handle 渲染表格，不进入 pipeline。

    list 子命令无 --cookie 参数，登录态经配置文件提供。
    """
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"cookie": "existing", "workspace": "./workspace"}', encoding="utf-8")
    use_case = _FakePlaylistUseCase([Playlist(7, "收藏", (1, 2))])
    monkeypatch.setattr("musicvault.application.bootstrap.build_playlist_use_case", lambda _cfg: use_case)
    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda _cfg: object())
    recorder = _PipelineRecorder()
    monkeypatch.setattr("musicvault.application.bootstrap.build_pipeline", recorder.build)

    code = main(["list", "--config", str(cfg_file)])

    assert code == 0
    assert recorder.calls == []
    assert "收藏" in _all_output(capfd)


# -- 内部函数 --------------------------------------------------------------------


def test_ensure_cookie_returns_existing_from_args() -> None:
    """_ensure_cookie 非交互路径：args.cookie 优先返回，just_logged_in=False。"""
    args = argparse.Namespace(cookie="args-ck")

    assert main_module._ensure_cookie(args, Config()) == ("args-ck", False)


def test_ensure_cookie_returns_existing_from_config() -> None:
    """_ensure_cookie 非交互路径：无 args.cookie 时回退 cfg.cookie。"""
    cfg = Config()
    cfg.cookie = "cfg-ck"

    assert main_module._ensure_cookie(argparse.Namespace(), cfg) == ("cfg-ck", False)


def test_configure_logs_sets_module_logger() -> None:
    """_configure_logs 两种模式均初始化模块 logger。"""
    main_module._configure_logs(verbose=True)
    assert main_module.logger.name == "musicvault.cli.main"
    main_module._configure_logs(verbose=False)
    assert main_module.logger.name == "musicvault.cli.main"


# -- init 无 cookie 路径（登录编排打桩，不进入真实交互） -----------------------


def test_init_login_cancelled_returns_2(tmp_path: Path, monkeypatch) -> None:
    """init 且登录取消：_ensure_cookie 返回空 cookie，main 返回 2。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("musicvault.cli.main._ensure_cookie", lambda _args, _cfg: (None, False))

    assert main(["init", "--config", str(tmp_path / "config.json")]) == 2


def test_init_login_success_returns_0(tmp_path: Path, monkeypatch) -> None:
    """init 且登录成功：_ensure_cookie 返回 cookie，main 返回 0。"""
    monkeypatch.setattr("musicvault.cli.main.signal", _fake_signal_module())
    monkeypatch.setattr("musicvault.cli.main._ensure_cookie", lambda _args, _cfg: ("ck", True))

    assert main(["init", "--config", str(tmp_path / "config.json")]) == 0


def test_ensure_cookie_success_after_interactive_login(tmp_path: Path, monkeypatch) -> None:
    """_ensure_cookie：交互登录返回 cookie 后保存到配置文件并标记 just_logged_in。"""
    monkeypatch.setattr("musicvault.cli.main._interactive_login", lambda _api: "ck-new")
    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda _cfg: object())
    cfg_file = tmp_path / "config.json"
    cfg = Config.load(cfg_file)

    cookie, just_logged_in = main_module._ensure_cookie(argparse.Namespace(), cfg)

    assert cookie == "ck-new"
    assert just_logged_in is True
    assert '"cookie": "ck-new"' in cfg_file.read_text(encoding="utf-8")


# -- _render_qrcode / _interactive_login 确定性分支 ------------------------------
#
# 二维码扫码等待（轮询、过期、超时）为真实交互流程，按任务约束不测；
# 菜单/密码/短信等分支以打桩 input/getpass + fake API 确定性覆盖
# （与 test_cli_cancellation.py 既有的打桩手法一致）。


class _FakeLoginApi:
    """_interactive_login 测试用 fake InteractiveLoginApi。"""

    def __init__(
        self,
        *,
        cookie: str | None = "ck",
        error: str | None = None,
        send_ok: bool = True,
        nickname: str = "测试用户",
    ) -> None:
        self.cookie = cookie
        self.error = error
        self.send_ok = send_ok
        self.nickname = nickname
        self.login_calls: list[tuple[str | None, str | None]] = []

    def login_via_phone(
        self, *, phone: str, password: str | None = None, captcha: str | None = None
    ) -> SimpleNamespace:
        del phone
        if self.error:
            raise RuntimeError(self.error)
        self.login_calls.append((password, captcha))
        return SimpleNamespace(nickname=self.nickname)

    def send_sms_code(self, phone: str) -> bool:
        del phone
        return self.send_ok

    def extract_cookie(self) -> str | None:
        return self.cookie


def _patch_input(monkeypatch, sequence: list[str]) -> None:
    """按序列打桩 builtins.input，模拟用户依次输入。"""
    iterator = iter(sequence)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(iterator))


def test_render_qrcode_produces_ascii_art() -> None:
    """_render_qrcode 把链接渲染为非空 ASCII 二维码。"""
    art = main_module._render_qrcode("https://example.com/qr")

    assert isinstance(art, str)
    assert art.strip()


def test_interactive_login_quit_returns_none(monkeypatch) -> None:
    """登录菜单输入 q：取消并返回 None。"""
    _patch_input(monkeypatch, ["q"])

    assert main_module._interactive_login(_FakeLoginApi()) is None


def test_interactive_login_password_flow_success(monkeypatch) -> None:
    """密码登录：手机号+密码登录成功并提取 cookie 返回。"""
    api = _FakeLoginApi()
    _patch_input(monkeypatch, ["2", "13800000000"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")

    assert main_module._interactive_login(api) == "ck"
    assert api.login_calls == [("pw", None)]


def test_interactive_login_sms_flow_success(monkeypatch) -> None:
    """验证码登录：发送验证码、输入验证码后登录成功。"""
    api = _FakeLoginApi()
    _patch_input(monkeypatch, ["3", "13900000000", "123456"])

    assert main_module._interactive_login(api) == "ck"
    assert api.login_calls == [(None, "123456")]


def test_interactive_login_empty_phone_warns_and_retries(monkeypatch, capfd) -> None:
    """手机号为空：警告后重试，最终取消。"""
    _patch_input(monkeypatch, ["2", "", "q"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")

    assert main_module._interactive_login(_FakeLoginApi()) is None
    assert "手机号不能为空" in _all_output(capfd)


def test_interactive_login_empty_password_warns(monkeypatch, capfd) -> None:
    """密码为空：警告后重试，最终取消。"""
    _patch_input(monkeypatch, ["2", "13800000000", "q"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "")

    assert main_module._interactive_login(_FakeLoginApi()) is None
    assert "密码不能为空" in _all_output(capfd)


def test_interactive_login_sms_empty_phone_warns(monkeypatch, capfd) -> None:
    """短信登录手机号为空：警告后重试，最终取消。"""
    _patch_input(monkeypatch, ["3", "", "q"])

    assert main_module._interactive_login(_FakeLoginApi()) is None
    assert "手机号不能为空" in _all_output(capfd)


def test_interactive_login_sms_send_failure_warns(monkeypatch, capfd) -> None:
    """验证码发送失败：警告后重试，最终取消。"""
    _patch_input(monkeypatch, ["3", "13900000000", "q"])

    assert main_module._interactive_login(_FakeLoginApi(send_ok=False)) is None
    assert "验证码发送失败" in _all_output(capfd)


def test_interactive_login_sms_empty_captcha_warns(monkeypatch, capfd) -> None:
    """验证码为空：警告后重试，最终取消。"""
    _patch_input(monkeypatch, ["3", "13900000000", "", "q"])

    assert main_module._interactive_login(_FakeLoginApi()) is None
    assert "验证码不能为空" in _all_output(capfd)


def test_interactive_login_invalid_option_warns(monkeypatch, capfd) -> None:
    """菜单输入无效选项：警告后重试，最终取消。"""
    _patch_input(monkeypatch, ["9", "q"])

    assert main_module._interactive_login(_FakeLoginApi()) is None
    assert "无效选项" in _all_output(capfd)


def test_interactive_login_cookie_extract_failure_warns(monkeypatch, capfd) -> None:
    """登录成功但无法提取 cookie：警告后重试，最终取消。"""
    _patch_input(monkeypatch, ["2", "13800000000", "q"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")

    assert main_module._interactive_login(_FakeLoginApi(cookie=None)) is None
    assert "无法提取 Cookie" in _all_output(capfd)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("账号或密码错误 502", "账号或密码错误"),
        ("需要行为验证码 8821", "需要行为验证码"),
        ("需要本人确认 8860", "需要本人确认"),
        ("网络超时", "登录失败：网络超时"),
    ],
)
def test_interactive_login_error_mapping(monkeypatch, capfd, error: str, expected: str) -> None:
    """登录异常按错误码映射提示，并显示剩余尝试次数。"""
    _patch_input(monkeypatch, ["2", "13800000000", "q"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")

    assert main_module._interactive_login(_FakeLoginApi(error=error)) is None
    assert expected in _all_output(capfd)


def test_interactive_login_exhausts_attempts_returns_none(monkeypatch, capfd) -> None:
    """连续失败耗尽 3 次尝试后返回 None。"""
    _patch_input(monkeypatch, ["2", "p1", "2", "p2", "2", "p3"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")

    assert main_module._interactive_login(_FakeLoginApi(error="总是失败")) is None
    assert "剩余尝试次数" in _all_output(capfd)


# -- cli/main.py 的 __main__ 入口守卫 --------------------------------------------


def test_cli_main_entry_guard_exits_zero(monkeypatch) -> None:
    """以 __main__ 名义执行 cli/main.py：命中入口守卫并以 0 退出。"""
    monkeypatch.setattr("signal.signal", lambda *_: None)
    monkeypatch.setattr("sys.argv", ["cli-main", "help"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(Path(main_module.__file__).resolve()), run_name="__main__")

    assert exc.value.code == 0


def test_handle_double_sigint_raises_then_force_exits(monkeypatch, capfd) -> None:
    """双击 Ctrl+C：首次抛 KeyboardInterrupt，再次写入提示并 os._exit(130)。

    os._exit 永不返回，故桩函数记录退出码后以 SystemExit 终止，模拟真实语义。
    """
    monkeypatch.setattr("musicvault.cli.main._force_exit", False)

    with pytest.raises(KeyboardInterrupt):
        main_module._handle_double_sigint(2, None)

    exit_codes: list[int] = []

    def _fake_os_exit(code: int) -> None:
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("musicvault.cli.main.os._exit", _fake_os_exit)

    with pytest.raises(SystemExit) as exc:
        main_module._handle_double_sigint(2, None)

    assert exit_codes == [130]
    assert exc.value.code == 130
    assert "再次 Ctrl+C 强制退出" in capfd.readouterr().err


def test_interactive_login_qrcode_flow_success(monkeypatch) -> None:
    """二维码登录：轮询打桩后成功获取登录状态并返回 cookie。"""
    api = _FakeLoginApi()
    api.get_qrcode_unikey = lambda: "uk"  # type: ignore[attr-defined]
    api.get_qrcode_url = lambda unikey: f"https://example.com/qr/{unikey}"  # type: ignore[attr-defined]
    api.get_login_status = lambda: SimpleNamespace(  # type: ignore[attr-defined]
        nickname="测试用户", cookie="ck"
    )
    _patch_input(monkeypatch, ["1"])
    monkeypatch.setattr(main_module, "_poll_qrcode", lambda _api, _unikey: None)

    assert main_module._interactive_login(api) == "ck"

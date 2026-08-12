from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import time
from pathlib import Path

from musicvault.cli.playlist import handle_playlist_mgmt
from musicvault.core.config import Config
from musicvault.ports.source import InteractiveLoginApi
from musicvault.shared.output import error as output_error
from musicvault.shared.output import info as output_info
from musicvault.shared.output import success as output_success
from musicvault.shared.output import warn as output_warn
from musicvault.shared.tui_progress import console, transient_section

_DEFAULT_CONFIG = os.environ.get("MUSIC_VAULT_CONFIG", "./config.json")
_force_exit = False
logger: logging.Logger


def _handle_double_sigint(_signum: int, _frame: object) -> None:
    """双击 Ctrl+C 的 SIGINT 处理器。

    首次 Ctrl+C → 触发 KeyboardInterrupt，走优雅关闭流程（保存状态等）。
    再次 Ctrl+C → 直接 os._exit(130)，立即强制终止。
    """
    del _signum, _frame
    global _force_exit
    if _force_exit:
        sys.stderr.write("\n再次 Ctrl+C 强制退出\n")
        sys.stderr.flush()
        os._exit(130)
    _force_exit = True
    raise KeyboardInterrupt


def _configure_logs(verbose: bool = False) -> None:
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(message)s",
            stream=sys.stderr,
        )
    from musicvault.shared.silence import silence_loggers

    silence_loggers("urllib3.connectionpool", "App")

    global logger
    logger = logging.getLogger(__name__)


def _add_common_args(parser: argparse.ArgumentParser, include_dry_run: bool = True) -> None:
    parser.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    parser.add_argument("--cookie", default=None, help="网易云 Cookie 字符串")
    parser.add_argument("--workspace", default=None, help="工作目录")
    parser.add_argument("--force", action="store_true", help="强制重处理已处理文件（覆盖 processed 索引）")
    if include_dry_run:
        parser.add_argument("--dry-run", action="store_true", help="预览模式：执行全部查询，但不下载、不写入任何文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MusicVault — 网易云音乐本地同步工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # 帮助子命令：musicvault help [subcommand]
    help_parser = sub.add_parser("help", help="显示帮助信息")
    help_parser.add_argument("subcommand", nargs="?", default=None, help="要查看的子命令名称")

    init = sub.add_parser("init", help="初始化配置", description="登录网易云音乐账号并创建配置文件")
    init.add_argument("--cookie", default=None, help="网易云 Cookie（跳过交互登录）")
    init.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）")
    init.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    sync = sub.add_parser("sync", help="同步音乐", description="拉取、后处理并分发到 library")
    _add_common_args(sync)
    distribute_group = sync.add_mutually_exclusive_group()
    distribute_group.add_argument("--no-distribute", action="store_true", help="同步完成后跳过分发（library 重建）")
    distribute_group.add_argument(
        "--only-distribute", action="store_true", help="仅执行分发（library 重建），跳过拉取/下载/后处理"
    )

    add_pl = sub.add_parser("add", help="添加歌单", description="添加要同步的目标歌单")
    add_pl.add_argument(
        "input",
        type=str,
        nargs="*",
        default=None,
        help="歌单 ID 或链接，不提供则从账号歌单中选择",
    )
    add_pl.add_argument("--song", type=int, nargs="+", default=None, help="直接添加单曲 ID（可多个）")
    add_pl.add_argument("--cookie", default=None, help="网易云 Cookie")
    add_pl.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    add_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    rm_pl = sub.add_parser("remove", aliases=["rm"], help="移除歌单（支持 ID 或无参数交互选择）")
    rm_pl.add_argument(
        "playlist_id",
        type=int,
        nargs="?",
        default=None,
        help="歌单 ID，不提供则从已添加歌单中选择",
    )
    rm_pl.add_argument("--song", type=int, nargs="+", default=None, help="移除单曲 ID（可多个）")
    rm_pl.add_argument("--cookie", default=None, help="网易云 Cookie（用于同步）")
    rm_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）")
    rm_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    ls_pl = sub.add_parser("list", aliases=["ls"], help="查看已添加的歌单")
    ls_pl.add_argument("--song", action="store_true", help="查看单独管理的单曲列表")
    ls_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）")
    ls_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    preset = sub.add_parser("preset", help="管理 preset", description="发现并列出内置和外部 Python preset")
    preset_sub = preset.add_subparsers(dest="preset_action", required=True)
    preset_list = preset_sub.add_parser("list", help="列出可用 preset")
    preset_list.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    preset_list.add_argument("--workspace", default=None, help="工作目录")
    preset_list.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    target = sub.add_parser("target", help="管理 sync_target", description="发现并列出内置和外部 sync_target")
    target_sub = target.add_subparsers(dest="target_action", required=True)
    target_list = target_sub.add_parser("list", help="列出可用 sync_target")
    target_list.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    target_list.add_argument("--workspace", default=None, help="工作目录")
    target_list.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    distribute = sub.add_parser(
        "distribute", help="运行本地分发", description="从 SQLite 源快照执行已发现 preset 的目标分发"
    )
    distribute.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    distribute.add_argument("--workspace", default=None, help="工作目录")
    distribute.add_argument("--preset", action="append", default=None, help="只执行指定 preset，可重复指定")
    distribute.add_argument("--dry-run", action="store_true", help="只展示操作计划，不产生目标端副作用")
    distribute.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    return parser


def main(argv: list[str] | None = None) -> int:
    # 安装双击 Ctrl+C 信号处理器：首次→优雅关闭，再次→强制终止
    signal.signal(signal.SIGINT, _handle_double_sigint)

    parser = build_parser()
    raw_args = argv if argv is not None else sys.argv[1:]

    # 如果无参数或 help 子命令，打印帮助
    if not raw_args:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_args)

    if args.command == "help":
        if args.subcommand:
            parser.parse_args([args.subcommand, "--help"])
        else:
            parser.print_help()
        return 0

    cfg_path = Path(args.config).resolve()
    cfg = Config.load(cfg_path)

    _configure_logs(verbose=args.verbose)

    existed = cfg_path.exists()
    if existed:
        logger.info("已加载配置文件：%s", cfg_path)
    else:  # pragma: no cover - 不可达：Config.load 总会先创建配置文件
        logger.info("配置文件不存在，已按默认值自动生成：%s", cfg_path)

    # init 命令：仅登录并创建配置
    if args.command == "init":
        if getattr(args, "cookie", None):
            cfg.cookie = args.cookie
            cfg.save()
            output_success("已通过 --cookie 初始化配置文件")
            return 0
        if cfg.cookie:
            output_info("已登录，配置文件已就绪")
            output_info(f"配置路径：{cfg_path}")
            return 0
        cookie, _ = _ensure_cookie(args, cfg)
        return 0 if cookie else 2

    if args.command in ("preset", "target"):
        if getattr(args, "workspace", None) is not None:
            cfg.workspace = args.workspace
        try:
            from musicvault.application.bootstrap import build_runtime
            from musicvault.cli.render import render_presets, render_targets

            runtime = build_runtime(cfg)
            if args.command == "preset":
                render_presets(runtime.presets.preset_registrations())
            else:
                render_targets(runtime.presets.target_registrations())
        except Exception as error:  # noqa: BLE001 - CLI 将加载失败转换为非零退出码
            output_error(f"preset/target 加载失败：{error}")
            return 2
        return 0

    if args.command == "distribute":
        if getattr(args, "workspace", None) is not None:
            cfg.workspace = args.workspace
        try:
            from musicvault.application.bootstrap import build_distribute_pipeline
            from musicvault.domain.operations import OperationStatus

            result = build_distribute_pipeline(cfg, dry_run=args.dry_run).run(
                selected=set(args.preset) if args.preset else None
            )
        except Exception as error:  # noqa: BLE001 - CLI 将应用失败转换为非零退出码
            output_error(f"分发失败：{error}")
            return 2
        from musicvault.cli.render import render_distribute_result

        render_distribute_result(result)
        if result.status == OperationStatus.FAILED:
            return 1
        output_success(f"分发完成，snapshot={result.snapshot_hash[:16]}")
        return 0

    # 任意需要 API 的操作前先确保登录
    cookie, just_logged_in = _ensure_cookie(args, cfg)
    if cookie is None:
        return 2

    # sync 首次登录后退出，让用户有机会配置歌单
    if args.command == "sync" and just_logged_in:
        from musicvault.application.bootstrap import build_playlist_use_case

        if not build_playlist_use_case(cfg).list_playlists():
            console.print(
                """
  [bold]下一步操作：[/bold]
    选择已有歌单：[bold]msv add[/bold]
    手动添加歌单：[bold]msv add <歌单ID或链接>[/bold]
    开始同步：[bold]msv sync[/bold]
    查看帮助：[bold]msv help[/bold]

  [dim]提示：歌单链接可从网易云音乐客户端分享获取[/dim]""",
                highlight=False,
            )
        return 0

    if args.command in ("add", "remove", "rm", "list", "ls"):
        result = handle_playlist_mgmt(args, cfg)
        if args.command in ("list", "ls") or result != 0:
            return result
        # add/remove 成功后继续执行 pipeline

    workspace = getattr(args, "workspace", None)
    if workspace is not None:
        cfg.workspace = workspace
    if getattr(args, "force", False):
        cfg.force = True

    # add / remove 成功后自动执行 sync
    from musicvault.application.bootstrap import build_pipeline
    from musicvault.cli.render import BatchProgressAdapter, render_pipeline_result

    service = build_pipeline(cfg, dry_run=getattr(args, "dry_run", False))
    progress = BatchProgressAdapter()
    try:
        result = service.run_pipeline(
            cookie,
            distribute=not getattr(args, "no_distribute", False),
            only_distribute=getattr(args, "only_distribute", False),
            progress=progress,
        )
        render_pipeline_result(
            result,
            dry_run=getattr(args, "dry_run", False),
            only_distribute=getattr(args, "only_distribute", False),
        )
    except KeyboardInterrupt:
        output_info("已取消")
        return 130
    return 0


def _ensure_cookie(args: argparse.Namespace, cfg: Config) -> tuple[str | None, bool]:
    """获取或引导登录，返回 (cookie, just_logged_in)。

    - 已有 cookie 则直接返回
    - 否则进入交互式登录；成功后保存到配置文件
    - 登录失败返回 (None, False)
    """
    cookie = getattr(args, "cookie", None) or cfg.cookie
    if cookie:
        return cookie, False

    console.print()
    console.print("[bold]首次使用需要登录网易云音乐账号[/bold]")
    from musicvault.application.bootstrap import build_source_client

    cookie = _interactive_login(build_source_client(cfg))
    if not cookie:
        output_error("登录失败或已取消")
        return None, False
    cfg.cookie = cookie
    cfg.save()
    output_success("登录信息已保存")
    return cookie, True


def _poll_qrcode(  # pragma: no cover - 二维码扫码轮询交互，人工手动测试
    api: InteractiveLoginApi,
    unikey: str,
) -> None:
    """轮询二维码登录状态直至确认 / 过期 / 超时。"""

    with console.status("[dim]等待扫码...[/dim]", spinner="dots") as status:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            code = api.check_qrcode(unikey)
            if code == 802:
                status.update("[dim]已扫码，请在手机上确认登录...[/dim]")
            elif code == 803:
                break
            elif code == 800:
                raise RuntimeError("二维码已过期，请重新获取")
            time.sleep(2)
        else:
            raise TimeoutError("二维码登录超时，请重试")


def _render_qrcode(url: str) -> str:
    """将链接渲染为终端二维码 ASCII 字符串"""
    import io

    import qrcode

    qr = qrcode.QRCode()
    qr.add_data(url)
    qr.make()
    buf = io.StringIO()
    qr.print_ascii(out=buf)
    return buf.getvalue()


def _interactive_login(api: InteractiveLoginApi) -> str | None:
    """交互式登录，返回 cookie 字符串；用户取消则返回 None。

    api 由 composition root（bootstrap.build_source_client）创建后注入。
    """
    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            with transient_section():
                console.print()
                console.print("  选择登录方式：")
                console.print("    [1] 二维码登录（推荐）")
                console.print("    [2] 密码登录")
                console.print("    [3] 验证码登录")
                console.print("    [q] 退出")
                console.print()
                choice = input("  请输入选项 [1/2/3/q]：").strip()
        except KeyboardInterrupt:
            console.print()
            return None

        if choice.lower() == "q":
            return None

        try:
            # -- 二维码登录 -------------------------------------------------
            if choice == "1":
                unikey = api.get_qrcode_unikey()
                url = api.get_qrcode_url(unikey)
                qr_art = _render_qrcode(url)

                with transient_section():
                    console.print()
                    console.print(qr_art, end="", highlight=False)
                    console.print(f"  [dim]{url}[/dim]")
                    console.print()
                    console.print("  [bold]请打开网易云音乐 App，扫描上方二维码[/bold]")

                    _poll_qrcode(api, unikey)

                result = api.get_login_status()

            # -- 手机号 + 密码 ----------------------------------------------
            elif choice == "2":
                with transient_section():
                    phone = input("  手机号：").strip()
                    password = getpass.getpass("  密码：")
                if not phone:
                    output_warn("手机号不能为空")
                    continue
                if not password:
                    output_warn("密码不能为空")
                    continue
                result = api.login_via_phone(phone=phone, password=password)

            # -- 手机号 + 验证码 --------------------------------------------
            elif choice == "3":
                with transient_section():
                    phone = input("  手机号：").strip()
                if not phone:
                    output_warn("手机号不能为空")
                    continue
                if not api.send_sms_code(phone=phone):
                    output_warn("验证码发送失败，请检查手机号或稍后重试")
                    continue
                output_info("验证码已发送，请注意查收短信")

                with transient_section():
                    captcha = input("  验证码：").strip()
                if not captcha:
                    output_warn("验证码不能为空")
                    continue
                result = api.login_via_phone(phone=phone, captcha=captcha)

            else:
                output_warn("无效选项，请输入 1、2、3 或 q")
                continue

            cookie = api.extract_cookie()
            if not cookie:
                output_warn("登录成功但无法提取 Cookie，请尝试其他方式")
                continue

            console.print(f"\n[green]●[/green] 登录成功：[bold]{result.nickname}[/bold]")
            output_info("Cookie 已保存到配置文件")
            return cookie

        except KeyboardInterrupt:
            console.print()
            return None
        except Exception as exc:
            remaining = max_attempts - attempt - 1
            msg = str(exc)
            if "502" in msg:
                output_warn("账号或密码错误")
            elif "8821" in msg:
                output_warn("需要行为验证码，密码/验证码登录可能已被安全策略限制")
                output_info("建议使用二维码登录（更稳定）")
            elif "8860" in msg:
                output_warn("需要本人确认，该账号可能触发了风控检查")
                output_info("建议使用二维码登录（更稳定）")
            else:
                output_error(f"登录失败：{exc}")
            if remaining > 0:
                output_info(f"剩余尝试次数：{remaining}")

    return None


if __name__ == "__main__":
    raise SystemExit(main())

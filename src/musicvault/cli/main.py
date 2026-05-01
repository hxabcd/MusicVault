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
from musicvault.shared.output import error as output_error
from musicvault.shared.output import info as output_info
from musicvault.shared.output import success as output_success
from musicvault.shared.output import warn as output_warn
from musicvault.shared.tui_progress import console

_DEFAULT_CONFIG = os.environ.get("MUSIC_VAULT_CONFIG", "./config.json")
_force_exit = False
logger: logging.Logger


def _handle_double_sigint(signum: int, frame: object) -> None:
    """双击 Ctrl+C 的 SIGINT 处理器。

    首次 Ctrl+C → 触发 KeyboardInterrupt，走优雅关闭流程（保存状态等）。
    再次 Ctrl+C → 直接 os._exit(130)，立即强制终止。
    """
    global _force_exit
    if _force_exit:
        sys.stderr.write("\n再次 Ctrl+C 强制退出\n")
        sys.stderr.flush()
        os._exit(130)
    _force_exit = True
    raise KeyboardInterrupt


def _silence_libs() -> None:
    for name in ("pyncm", "urllib3.connectionpool", "App"):
        muted = logging.getLogger(name)
        muted.setLevel(logging.WARNING)
        muted.propagate = False


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
    _silence_libs()

    global logger
    logger = logging.getLogger(__name__)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    parser.add_argument("--cookie", default=None, help="网易云 Cookie 字符串")
    parser.add_argument("--workspace", default=None, help="工作目录")
    parser.add_argument("--force", action="store_true", help="强制重处理已处理文件（覆盖 processed 索引）")
    parser.add_argument("--no-translation", action="store_true", help="关闭网易云歌词翻译合并（默认开启）")
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

    sync = sub.add_parser("sync", help="同步音乐", description="拉取并处理音乐")
    _add_common_args(sync)

    pull = sub.add_parser("pull", help="拉取音乐", description="从网易云歌单同步并下载音乐")
    _add_common_args(pull)

    process = sub.add_parser(
        "process",
        help="处理音乐",
        description="对本地音乐文件进行后处理。对 lossless 填充完整的元数据，对 lossy 压缩并仅填充基本元数据",
    )
    _add_common_args(process)

    add_pl = sub.add_parser("add", help="添加歌单", description="添加要同步的目标歌单")
    add_pl.add_argument(
        "input",
        type=str,
        nargs="*",
        default=None,
        help="歌单 ID 或链接，不提供则从账号歌单中选择",
    )
    add_pl.add_argument(
        "--song", type=int, nargs="+", default=None, help="直接添加单曲 ID（可多个）"
    )
    add_pl.add_argument("--cookie", default=None, help="网易云 Cookie")
    add_pl.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    add_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    rm_pl = sub.add_parser("remove", help="移除歌单（支持 ID 或无参数交互选择）")
    rm_pl.add_argument(
        "playlist_id",
        type=int,
        nargs="?",
        default=None,
        help="歌单 ID，不提供则从已添加歌单中选择",
    )
    rm_pl.add_argument(
        "--song", type=int, nargs="+", default=None, help="移除单曲 ID（可多个）"
    )
    rm_pl.add_argument("--cookie", default=None, help="网易云 Cookie（用于同步）")
    rm_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）")
    rm_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    ls_pl = sub.add_parser("list", aliases=["ls"], help="查看已添加的歌单")
    ls_pl.add_argument("--song", action="store_true", help="查看单独管理的单曲列表")
    ls_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）")
    ls_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

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
    else:
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

    # 任意需要 API 的操作前先确保登录
    cookie, just_logged_in = _ensure_cookie(args, cfg)
    if cookie is None:
        return 2

    # sync / pull 首次登录后退出，让用户有机会配置歌单
    if args.command in ("sync", "pull") and just_logged_in:
        if not cfg.get_playlist_ids():
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

    if args.command in ("add", "remove", "list", "ls"):
        result = handle_playlist_mgmt(args, cfg)
        if args.command in ("list", "ls") or result != 0:
            return result
        # add/remove 成功后继续执行 pipeline

    workspace = getattr(args, "workspace", None)
    if workspace is not None:
        cfg.workspace = workspace
    if getattr(args, "force", False):
        cfg.force = True
    if getattr(args, "no_translation", False):
        cfg.include_translation = False

    # add / remove 成功后自动执行 sync；其余子命令照原样传递
    pipeline_cmd = args.command if args.command in ("sync", "pull", "process") else "sync"

    from musicvault.adapters.providers.pyncm_client import PyncmClient
    from musicvault.services.run_service import RunService

    service = RunService(
        cfg=cfg,
        api=PyncmClient(
            text_cleaning_enabled=cfg.text_cleaning_enabled,
            download_quality=cfg.download_quality,
            api_download_url_chunk_size=cfg.api_download_url_chunk_size,
            api_track_detail_chunk_size=cfg.api_track_detail_chunk_size,
            alias_split_separators=cfg.alias_split_separators,
        ),
    )
    try:
        service.run_pipeline(cookie=cookie, command=pipeline_cmd)
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
    cookie = _interactive_login()
    if not cookie:
        output_error("登录失败或已取消")
        return None, False
    cfg.cookie = cookie
    cfg.save()
    output_success("登录信息已保存")
    return cookie, True


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


def _interactive_login() -> str | None:
    """交互式登录，返回 cookie 字符串；用户取消则返回 None"""
    from musicvault.adapters.providers.pyncm_client import PyncmClient

    api = PyncmClient()
    max_attempts = 3

    for attempt in range(max_attempts):
        console.print()
        console.print("  选择登录方式：")
        console.print("    [1] 二维码登录（推荐）")
        console.print("    [2] 密码登录")
        console.print("    [3] 验证码登录")
        console.print("    [q] 退出")
        console.print()

        choice = input("  请输入选项 [1/2/3/q]：").strip()

        if choice.lower() == "q":
            return None

        try:
            # -- 二维码登录 -------------------------------------------------
            if choice == "1":
                unikey = api.get_qrcode_unikey()
                url = api.get_qrcode_url(unikey)
                console.print()
                qr_art = _render_qrcode(url)
                console.print(qr_art, end="", highlight=False)
                console.print(f"  [dim]{url}[/dim]")
                console.print()
                console.print("  [bold]请打开网易云音乐 App，扫描上方二维码[/bold]")

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

                result = api.get_login_status()

            # -- 手机号 + 密码 ----------------------------------------------
            elif choice == "2":
                phone = input("  手机号：").strip()
                if not phone:
                    output_warn("手机号不能为空")
                    continue
                password = getpass.getpass("  密码：")
                if not password:
                    output_warn("密码不能为空")
                    continue
                result = api.login_via_phone(phone=phone, password=password)

            # -- 手机号 + 验证码 --------------------------------------------
            elif choice == "3":
                phone = input("  手机号：").strip()
                if not phone:
                    output_warn("手机号不能为空")
                    continue
                if not api.send_sms_code(phone=phone):
                    output_warn("验证码发送失败，请检查手机号或稍后重试")
                    continue
                output_info("验证码已发送，请注意查收短信")
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
            # 常见安全拦截错误码提示
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

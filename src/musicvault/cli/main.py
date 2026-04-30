from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from musicvault.core.config import Config
from musicvault.shared.tui_progress import console

logger = logging.getLogger(__name__)


def _silence_logs() -> None:
    for name in ("pyncm", "urllib3.connectionpool", "App"):
        muted = logging.getLogger(name)
        muted.setLevel(logging.WARNING)
        muted.propagate = False


def _configure_logs() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    _silence_logs()


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="./config.json", help="配置文件路径（JSON）")
    parser.add_argument("--cookie", default=None, help="网易云 Cookie 字符串")
    parser.add_argument("--workspace", default=None, help="工作目录")
    parser.add_argument("--force", action="store_true", help="强制重处理已处理文件（覆盖 processed 索引）")
    parser.add_argument("--no-translation", action="store_true", help="关闭网易云歌词翻译合并（默认开启）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MusicVault 命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行同步和后处理")
    _add_common_args(run)

    sync = sub.add_parser("sync", help="仅执行同步下载")
    _add_common_args(sync)

    process = sub.add_parser("process", help="仅执行本地后处理")
    _add_common_args(process)

    add_pl = sub.add_parser("add", help="添加歌单 ID")
    add_pl.add_argument("playlist_id", type=int, help="歌单 ID")
    add_pl.add_argument("--config", default="./config.json", help="配置文件路径（JSON）")

    rm_pl = sub.add_parser("remove", help="移除歌单 ID")
    rm_pl.add_argument("playlist_id", type=int, help="歌单 ID")
    rm_pl.add_argument("--config", default="./config.json", help="配置文件路径（JSON）")

    ls_pl = sub.add_parser("list", help="查看已添加的歌单")
    ls_pl.add_argument("--config", default="./config.json", help="配置文件路径（JSON）")

    sub.add_parser("ls", help="查看已添加的歌单（list 别名）").add_argument(
        "--config", default="./config.json", help="配置文件路径（JSON）"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config).resolve()
    cfg = Config.load(cfg_path)

    if args.command in ("add", "remove", "list", "ls"):
        return _handle_playlist_mgmt(args, cfg)

    _configure_logs()

    existed = cfg_path.exists()
    if existed:
        logger.info("已加载配置文件：%s", cfg_path)
    else:
        logger.info("配置文件不存在，已按默认值自动生成：%s", cfg_path)

    cookie = args.cookie or cfg.cookie
    if not cookie:
        logger.error("缺少 cookie：请通过 --cookie 或配置文件提供")
        return 2

    if args.workspace is not None:
        cfg.workspace = args.workspace
    if args.force:
        cfg.force = True
    if args.no_translation:
        cfg.include_translation = False

    from musicvault.adapters.providers.pyncm_client import PyncmClient
    from musicvault.services.run_service import RunService

    service = RunService(
        cfg=cfg,
        api=PyncmClient(text_cleaning_enabled=cfg.text_cleaning_enabled),
    )
    service.run_pipeline(
        cookie=cookie,
        command=args.command,
    )
    return 0


def _handle_playlist_mgmt(args: argparse.Namespace, cfg: Config) -> int:
    if args.command == "add":
        if args.playlist_id in cfg.playlist_ids:
            console.print(f"[yellow]歌单 {args.playlist_id} 已存在，跳过添加[/yellow]")
        else:
            cfg.playlist_ids.append(args.playlist_id)
            cfg.save()
            console.print(f"[green]已添加歌单：{args.playlist_id}[/green]")

    elif args.command == "remove":
        if args.playlist_id not in cfg.playlist_ids:
            console.print(f"[yellow]歌单 {args.playlist_id} 不存在，无法移除[/yellow]")
        else:
            cfg.playlist_ids.remove(args.playlist_id)
            cfg.save()
            console.print(f"[green]已移除歌单：{args.playlist_id}[/green]")

    elif args.command in ("list", "ls"):
        if cfg.playlist_ids:
            console.print("[bold]当前管理的歌单 ID：[/bold]")
            for pid in cfg.playlist_ids:
                console.print(f"  {pid}")
        else:
            console.print("[dim]尚未添加任何歌单（将使用喜欢音乐歌单）[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

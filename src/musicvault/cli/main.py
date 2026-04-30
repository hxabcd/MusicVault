from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from musicvault.core.config import Config
from musicvault.shared.tui_progress import console

_DEFAULT_CONFIG = os.environ.get("MUSIC_VAULT_CONFIG", "./config.json")

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
    parser.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）")
    parser.add_argument("--cookie", default=None, help="网易云 Cookie 字符串")
    parser.add_argument("--workspace", default=None, help="工作目录")
    parser.add_argument("--force", action="store_true", help="强制重处理已处理文件（覆盖 processed 索引）")
    parser.add_argument("--no-translation", action="store_true", help="关闭网易云歌词翻译合并（默认开启）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MusicVault — 网易云音乐本地同步工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="文档 & 问题反馈: https://github.com/user/musicvault",
    )
    sub = parser.add_subparsers(dest="command")

    # 帮助子命令：musicvault help [subcommand]
    help_parser = sub.add_parser("help", help="显示帮助信息")
    help_parser.add_argument("subcommand", nargs="?", default=None, help="要查看的子命令名称")

    sync = sub.add_parser("sync", help="完整同步：拉取 + 处理")
    _add_common_args(sync)

    pull = sub.add_parser("pull", help="仅拉取下载")
    _add_common_args(pull)

    process = sub.add_parser("process", help="仅本地后处理")
    _add_common_args(process)

    add_pl = sub.add_parser("add", help="添加歌单（支持 ID 或网易云链接）")
    add_pl.add_argument("input", type=str, help="歌单 ID 或链接（如 https://music.163.com/playlist?id=xxx）")
    add_pl.add_argument("--cookie", default=None, help="网易云 Cookie（用于验证歌单有效性）")
    add_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）")

    rm_pl = sub.add_parser("remove", help="移除歌单 ID")
    rm_pl.add_argument("playlist_id", type=int, help="歌单 ID")
    rm_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）")

    ls_pl = sub.add_parser("list", help="查看已添加的歌单")
    ls_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）")

    sub.add_parser("ls", help="list 别名").add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
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


def _parse_playlist_id(raw: str) -> int:
    """从 ID 数字字符串或网易云链接中提取歌单 ID。"""
    stripped = raw.strip()
    if stripped.isdigit():
        return int(stripped)

    parsed = urlparse(stripped)
    if parsed.hostname and "music.163.com" in parsed.hostname:
        qs = parse_qs(parsed.query)
        ids = qs.get("id", [])
        if ids and ids[0].isdigit():
            return int(ids[0])
        fragment = parsed.fragment
        if fragment:
            m = re.search(r"[?&]id=(\d+)", fragment)
            if m:
                return int(m.group(1))

    raise RuntimeError(f"无法识别的歌单标识：{raw}（需为数字 ID 或 https://music.163.com 歌单链接）")


def _fetch_playlist_info(pid: int, cookie: str | None) -> dict[str, object] | None:
    """通过 API 获取歌单元数据，失败返回 None。"""
    if not cookie:
        return None
    try:
        from musicvault.adapters.providers.pyncm_client import PyncmClient

        api = PyncmClient()
        api.login_with_cookie(cookie)
        return dict(api.get_playlist_info(pid))
    except Exception:
        return None


def _load_playlist_index(cfg: Config) -> dict[str, dict[str, object]]:
    """加载缓存的歌单索引。"""
    index_path = cfg.state_dir / "playlists.json"
    if index_path.exists():
        from musicvault.shared.utils import load_json

        return load_json(index_path, {})
    return {}


def _handle_playlist_mgmt(args: argparse.Namespace, cfg: Config) -> int:
    if args.command == "add":
        try:
            pid = _parse_playlist_id(args.input)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1

        if pid in cfg.playlist_ids:
            cached = _load_playlist_index(cfg)
            entry = cached.get(str(pid), {})
            name = entry.get("name")
            label = f"{name} ({pid})" if name else str(pid)
            console.print(f"[yellow]歌单 {label} 已存在，跳过添加[/yellow]")
            return 0

        cookie = getattr(args, "cookie", None) or cfg.cookie
        info = _fetch_playlist_info(pid, cookie)

        if info is None:
            if not cookie:
                console.print("[yellow]未提供 cookie，跳过 API 验证[/yellow]")
            else:
                console.print("[yellow]无法获取歌单信息，将仅保存 ID[/yellow]")

        # 缓存歌单信息
        if info is not None:
            cfg.ensure_dirs()
            from musicvault.shared.utils import load_json, save_json

            index_path = cfg.state_dir / "playlists.json"
            cached = load_json(index_path, {})
            cached[str(pid)] = {"name": info["name"], "track_count": info["track_count"]}
            save_json(index_path, cached)

        cfg.playlist_ids.append(pid)
        cfg.save()

        name = info.get("name") if info else None
        if name:
            console.print(f"[green]已添加歌单：[bold]{name}[/bold] (ID: {pid})[/green]")
        else:
            console.print(f"[green]已添加歌单：{pid}[/green]")

    elif args.command == "remove":
        if args.playlist_id not in cfg.playlist_ids:
            console.print(f"[yellow]歌单 {args.playlist_id} 不存在，无法移除[/yellow]")
        else:
            cfg.playlist_ids.remove(args.playlist_id)
            cfg.save()
            console.print(f"[green]已移除歌单：{args.playlist_id}[/green]")

    elif args.command in ("list", "ls"):
        if cfg.playlist_ids:
            cached = _load_playlist_index(cfg)
            console.print("[bold]当前管理的歌单：[/bold]")
            for pid in cfg.playlist_ids:
                entry = cached.get(str(pid), {})
                name = entry.get("name")
                if name:
                    console.print(f"  [cyan]{pid}[/cyan]\t[dim]{name}[/dim]")
                else:
                    console.print(f"  [cyan]{pid}[/cyan]")
        else:
            console.print("[dim]尚未添加任何歌单（将使用喜欢音乐歌单）[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

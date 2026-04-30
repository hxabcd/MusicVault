from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from musicvault.core.config import Config
from musicvault.shared.output import error as output_error, info as output_info, success as output_success
from musicvault.shared.tui_progress import console

_DEFAULT_CONFIG = os.environ.get("MUSIC_VAULT_CONFIG", "./config.json")


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


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）")
    parser.add_argument("--cookie", default=None, help="网易云 Cookie 字符串")
    parser.add_argument("--workspace", default=None, help="工作目录")
    parser.add_argument("--force", action="store_true", help="强制重处理已处理文件（覆盖 processed 索引）")
    parser.add_argument("--no-translation", action="store_true", help="关闭网易云歌词翻译合并（默认开启）")
    parser.add_argument("-v", "--verbose", action="store_true", help="启用详细日志（DEBUG 级别）")


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
    add_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志（DEBUG 级别）")

    rm_pl = sub.add_parser("remove", help="移除歌单 ID")
    rm_pl.add_argument("playlist_id", type=int, help="歌单 ID")
    rm_pl.add_argument("--cookie", default=None, help="网易云 Cookie（用于同步）")
    rm_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）")
    rm_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志（DEBUG 级别）")

    ls_pl = sub.add_parser("list", help="查看已添加的歌单")
    ls_pl.add_argument("--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）")
    ls_pl.add_argument("-v", "--verbose", action="store_true", help="启用详细日志（DEBUG 级别）")

    ls_alias = sub.add_parser("ls", help="list 别名")
    ls_alias.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（也支持 MUSIC_VAULT_CONFIG 环境变量）"
    )
    ls_alias.add_argument("-v", "--verbose", action="store_true", help="启用详细日志（DEBUG 级别）")

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

    _configure_logs(verbose=args.verbose)

    if args.command in ("add", "remove", "list", "ls"):
        result = _handle_playlist_mgmt(args, cfg)
        if args.command in ("list", "ls") or result != 0:
            return result
        # add/remove 成功后继续执行 pipeline

    existed = cfg_path.exists()
    if existed:
        output_info(f"已加载配置文件：{cfg_path}")
    else:
        output_info(f"配置文件不存在，已按默认值自动生成：{cfg_path}")

    cookie = args.cookie or cfg.cookie
    if not cookie:
        output_error("缺少 cookie：请通过 --cookie 或配置文件提供")
        return 2

    workspace = getattr(args, "workspace", None)
    if workspace is not None:
        cfg.workspace = workspace
    if getattr(args, "force", False):
        cfg.force = True
    if getattr(args, "no_translation", False):
        cfg.include_translation = False

    from musicvault.adapters.providers.pyncm_client import PyncmClient
    from musicvault.services.run_service import RunService

    service = RunService(
        cfg=cfg,
        api=PyncmClient(text_cleaning_enabled=cfg.text_cleaning_enabled),
    )
    try:
        service.run_pipeline(
            cookie=cookie,
            command="sync",
        )
    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]已取消[/yellow]")
        return 130
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


def _cleanup_playlist_files(pid: int, cfg: Config) -> None:
    """删除歌单对应的音乐文件并清理相关状态。"""
    from musicvault.shared.utils import load_json, save_json, safe_filename

    playlist_index = load_json(cfg.state_dir / "playlists.json", {})
    entry = playlist_index.get(str(pid), {})
    name = entry.get("name")
    dir_name = safe_filename(str(name)) if name else safe_filename(str(pid))

    # 删除 library 子目录
    deleted_dirs = 0
    for parent in (cfg.lossless_dir, cfg.lossy_dir):
        target = parent / dir_name
        if target.is_dir():
            shutil.rmtree(target)
            deleted_dirs += 1

    # 从 playlists.json 移除
    if str(pid) in playlist_index:
        del playlist_index[str(pid)]
        save_json(cfg.state_dir / "playlists.json", playlist_index)

    # 清理 processed_files.json
    prefix_ll = f"library/lossless/{dir_name}/"
    prefix_ly = f"library/lossy/{dir_name}/"

    processed = load_json(cfg.processed_state_file, {})
    if isinstance(processed, dict) and processed:
        removed_source_keys: list[str] = []
        for key, value in list(processed.items()):
            if not isinstance(value, dict):
                continue
            ll = str(value.get("lossless", ""))
            ly = str(value.get("lossy", ""))
            if ll.startswith(prefix_ll) or ly.startswith(prefix_ly):
                removed_source_keys.append(key)
            else:
                links = value.get("links")
                if isinstance(links, list):
                    filtered = [
                        lnk
                        for lnk in links
                        if isinstance(lnk, dict)
                        and not str(lnk.get("lossless", "")).startswith(prefix_ll)
                        and not str(lnk.get("lossy", "")).startswith(prefix_ly)
                    ]
                    if len(filtered) != len(links):
                        value["links"] = filtered
                        processed[key] = value

        # 提取被移除条目的 track_id，同步清理 synced_tracks.json
        removed_track_ids: set[int] = set()
        for key in removed_source_keys:
            entry = processed[key]
            if isinstance(entry, dict):
                try:
                    removed_track_ids.add(int(entry.get("track_id", 0)))
                except (TypeError, ValueError):
                    pass
            del processed[key]
        save_json(cfg.processed_state_file, processed)

        if removed_track_ids:
            synced = load_json(cfg.synced_state_file, {"ids": []})
            if isinstance(synced, dict):
                existing = {int(x) for x in synced.get("ids", []) if isinstance(x, (int, str))}
                cleaned = existing - removed_track_ids
                if cleaned != existing:
                    save_json(cfg.synced_state_file, {"ids": sorted(cleaned)})

    if deleted_dirs:
        output_success(f"已删除 [bold]{dir_name}[/bold] 的音乐文件（{deleted_dirs} 个目录）")
    elif name:
        output_info(f"未找到 {dir_name} 的音乐目录，已跳过文件删除")


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
            return 1

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
            return 1
        cfg.playlist_ids.remove(args.playlist_id)
        cfg.save()
        _cleanup_playlist_files(args.playlist_id, cfg)
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

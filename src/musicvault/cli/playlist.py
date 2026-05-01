from __future__ import annotations

import argparse
import logging
import re
import shutil
from urllib.parse import parse_qs, urlparse

from rich.table import Table

from musicvault.core.config import Config
from musicvault.shared.output import error as output_error
from musicvault.shared.output import info as output_info
from musicvault.shared.output import success as output_success
from musicvault.shared.output import warn as output_warn
from musicvault.shared.tui_progress import console

logger = logging.getLogger(__name__)

def handle_playlist_mgmt(args: argparse.Namespace, cfg: Config) -> int:
    if args.command == "add":
        cookie = getattr(args, "cookie", None) or cfg.cookie

        if args.input is None:
            return _add_playlist_interactive(cfg, cookie)

        try:
            pid = _parse_playlist_id(args.input)
        except RuntimeError as exc:
            output_error(str(exc))
            return 1

        return _add_playlist_by_id(pid, cfg, cookie)

    elif args.command == "remove":
        if args.playlist_id is None:
            return _remove_playlist_interactive(cfg)
        if not cfg.has_playlist(args.playlist_id):
            output_warn(f"歌单 {args.playlist_id} 不存在，无法移除")
            return 1
        _cleanup_playlist_files(args.playlist_id, cfg)
        cfg.remove_playlist(args.playlist_id)
        output_success(f"已移除歌单：{args.playlist_id}")

    elif args.command in ("list", "ls"):
        playlist_ids = cfg.get_playlist_ids()
        if playlist_ids:
            cached = _load_playlist_index(cfg)
            table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)
            table.add_column(style="cyan")
            table.add_column(style="dim")
            for pid in playlist_ids:
                entry = cached.get(str(pid), {})
                name = entry.get("name")
                table.add_row(str(pid), name or "")
            console.print("[bold]当前管理的歌单：[/bold]")
            console.print(table, highlight=False)
        else:
            output_info("尚未添加任何歌单，请执行 msv add 添加")
    return 0


# ---------------------------------------------------------------------------
# 内部函数
# ---------------------------------------------------------------------------


def _parse_playlist_id(raw: str) -> int:
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
    index_path = cfg.state_dir / "playlists.json"
    if index_path.exists():
        from musicvault.shared.utils import load_json

        return load_json(index_path, {})
    return {}


def _cleanup_playlist_files(pid: int, cfg: Config) -> None:
    from musicvault.shared.utils import load_json, safe_filename, save_json

    playlist_index = load_json(cfg.state_dir / "playlists.json", {})
    entry = playlist_index.get(str(pid), {})
    name = entry.get("name")
    dir_name = safe_filename(str(name)) if name else safe_filename(str(pid))

    # 删除 library 目录（仅含硬链接，直接 rmtree）
    deleted_dirs = 0
    for parent in (cfg.lossless_dir, cfg.lossy_dir):
        target = parent / dir_name
        if target.is_dir():
            shutil.rmtree(target)
            deleted_dirs += 1

    # 更新 synced_tracks.json：移除该歌单的关联
    synced = load_json(cfg.synced_state_file, {"ids": []})
    ids_to_remove: set[int] = set()
    if isinstance(synced, dict):
        ids = synced.get("ids", [])
        if isinstance(ids, list):
            # 旧格式：无法区分歌单，跳过
            pass
        elif isinstance(ids, dict):
            for tid_str, pids in list(ids.items()):
                new_pids = [p for p in pids if p != pid]
                if new_pids:
                    ids[tid_str] = new_pids
                else:
                    ids_to_remove.add(int(tid_str))
                    del ids[tid_str]
            save_json(cfg.synced_state_file, {"ids": ids})

    # 删除无歌单归属的 canonical 文件
    for track_id in ids_to_remove:
        for ext in (".flac", ".mp3", ".lrc"):
            (cfg.downloads_dir / f"{track_id}{ext}").unlink(missing_ok=True)

    if deleted_dirs:
        logger.info("已删除 [bold]%s[/bold] 的音乐文件（%s 个目录）", dir_name, deleted_dirs)
    elif name:
        logger.info("未找到 %s 的音乐目录，已跳过文件删除", dir_name)


def _add_playlist_by_id(pid: int, cfg: Config, cookie: str | None) -> int:
    if cfg.has_playlist(pid):
        cached = _load_playlist_index(cfg)
        entry = cached.get(str(pid), {})
        name = entry.get("name")
        label = f"{name} ({pid})" if name else str(pid)
        logger.warning(f"歌单 {label} 已存在，跳过添加")
        return 1

    info = _fetch_playlist_info(pid, cookie)

    if info is None:
        if not cookie:
            logger.warning("未提供 cookie，跳过 API 验证")
        else:
            logger.warning("无法获取歌单信息，将仅保存 ID")

    name = str(info["name"]) if info and info.get("name") else ""
    track_count = int(info["track_count"]) if info and info.get("track_count") else 0
    cfg.add_playlist(pid, name=name, track_count=track_count)

    if name:
        output_success(f"已添加歌单：[bold]{name}[/bold] [dim]({pid})[/dim]")
    else:
        output_success(f"已添加歌单：{pid}")
    return 0


def _add_playlist_interactive(cfg: Config, cookie: str | None) -> int:
    if not cookie:
        output_error("未提供 cookie，无法获取账号歌单列表")
        output_info('请先执行 msv sync 登录，或通过 msv add <ID> --cookie "..." 添加')
        return 1

    from musicvault.adapters.providers.pyncm_client import PyncmClient

    api = PyncmClient()
    try:
        user = api.login_with_cookie(cookie)
        playlists = api.list_user_playlists(user.user_id)
    except Exception as exc:
        output_error(f"获取歌单列表失败：{exc}")
        return 1

    if not playlists:
        output_warn("当前账号没有歌单")
        return 1

    playlist_ids = cfg.get_playlist_ids()
    existing_ids = set(playlist_ids)
    available = [pl for pl in playlists if int(pl["id"]) not in existing_ids]
    already_added = [pl for pl in playlists if int(pl["id"]) in existing_ids]

    if not available:
        output_info("账号中所有歌单都已添加")
        if already_added:
            table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)
            table.add_column(style="cyan")
            table.add_column(style="dim")
            for pl in already_added:
                table.add_row(str(pl["id"]), pl["name"])
            console.print()
            console.print("  [dim]已添加的歌单：[/dim]")
            console.print(table, highlight=False)
        return 1

    console.print()
    console.print(f"[bold]{user.nickname}[/bold] 的歌单列表：")
    console.print()
    max_show = min(len(available), 50)

    table = Table(show_header=False, box=None, padding=(0, 1), collapse_padding=True)

    table.add_column(justify="right", style="cyan")
    table.add_column(justify="left", max_width=40, no_wrap=True)
    table.add_column(justify="right", style="dim")

    for i, pl in enumerate(available[:max_show], 1):
        track_count = pl.get("trackCount", pl.get("track_count", "?"))
        table.add_row(f"{i}.", pl["name"], f" {track_count} 首")

    console.print(table, highlight=False)

    if len(available) > max_show:
        console.print(f"  [dim]... 还有 {len(available) - max_show} 个歌单未显示[/dim]")

    if already_added:
        console.print()
        console.print(f"  隐藏了 {len(already_added)} 个已添加歌单")

    console.print()
    console.print("  输入编号选择歌单（如: 1,3,5 或 1-5 或 all），输入 q 取消")

    choice = input("  > ").strip()
    if choice.lower() == "q":
        output_info("已取消")
        return 1

    selected_indices = _parse_selection(choice, len(available))
    if not selected_indices:
        output_warn("未选择任何歌单")
        return 1

    added = 0
    for idx in selected_indices:
        pl = available[idx - 1]
        pid = int(pl["id"])
        if cfg.has_playlist(pid):
            continue
        cfg.add_playlist(
            pid,
            name=pl["name"],
            track_count=int(pl.get("trackCount", pl.get("track_count", 0))),
        )
        output_success(f"已添加歌单：[bold]{pl['name']}[/bold] (ID: {pid})")
        added += 1

    return 0 if added > 0 else 1


def _remove_playlist_interactive(cfg: Config) -> int:
    playlist_ids = cfg.get_playlist_ids()
    if not playlist_ids:
        output_info("尚未添加任何歌单，无需移除")
        return 1

    cached = _load_playlist_index(cfg)
    max_show = min(len(playlist_ids), 50)

    console.print()
    console.print("[bold]当前管理的歌单：[/bold]")
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 1), collapse_padding=True)
    table.add_column(justify="right", style="cyan")
    table.add_column(justify="left", max_width=40, no_wrap=True)
    table.add_column(justify="right", style="dim")

    for i, pid in enumerate(playlist_ids[:max_show], 1):
        entry = cached.get(str(pid), {})
        name = entry.get("name", "")
        track_count = entry.get("track_count", "?")
        table.add_row(f"{i}.", name or str(pid), f" {track_count} 首")

    console.print(table, highlight=False)

    if len(playlist_ids) > max_show:
        console.print(f"  [dim]... 还有 {len(playlist_ids) - max_show} 个歌单未显示[/dim]")

    console.print()
    console.print("  输入编号选择要移除的歌单（如: 1,3,5 或 1-5 或 all），输入 q 取消")

    choice = input("  > ").strip()
    if choice.lower() == "q":
        output_info("已取消")
        return 1

    selected_indices = _parse_selection(choice, len(playlist_ids))
    if not selected_indices:
        output_warn("未选择任何歌单")
        return 1

    removed = 0
    for idx in reversed(selected_indices):
        pid = playlist_ids[idx - 1]
        _cleanup_playlist_files(pid, cfg)
        cfg.remove_playlist(pid)
        entry = cached.get(str(pid), {})
        name = entry.get("name")
        label = f"[bold]{name}[/bold] (ID: {pid})" if name else str(pid)
        output_success(f"已移除歌单：{label}")
        removed += 1

    return 0 if removed > 0 else 1


def _parse_selection(raw: str, max_num: int) -> list[int]:
    raw = raw.strip()
    if raw.lower() == "all":
        return list(range(1, max_num + 1))

    selected: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a.strip()), int(b.strip())
                if start > end:
                    start, end = end, start
                for n in range(start, end + 1):
                    if 1 <= n <= max_num:
                        selected.add(n)
            except ValueError:
                output_warn(f"无效范围：{part}，已跳过")
        else:
            try:
                n = int(part)
                if 1 <= n <= max_num:
                    selected.add(n)
            except ValueError:
                output_warn(f"无效编号：{part}，已跳过")
    return sorted(selected)

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
from musicvault.shared.tui_progress import console, transient_section

logger = logging.getLogger(__name__)


def handle_playlist_mgmt(args: argparse.Namespace, cfg: Config) -> int:
    if args.command == "add":
        cookie = getattr(args, "cookie", None) or cfg.cookie
        has_songs = bool(getattr(args, "song", None))
        inputs = getattr(args, "input", None) or []

        if has_songs:
            _add_songs(args.song, cfg)

        if not inputs and not has_songs:
            return _add_playlist_interactive(cfg, cookie)

        result = 0
        for raw in inputs:
            try:
                pid = _parse_playlist_id(raw)
            except RuntimeError as exc:
                output_error(str(exc))
                result = 1
                continue
            if _add_playlist_by_id(pid, cfg, cookie) != 0:
                result = 1

        return 0 if result == 0 and not (has_songs and not inputs) else result

    elif args.command == "remove" or args.command == "rm":
        has_songs = bool(getattr(args, "song", None))

        if has_songs:
            _remove_songs(args.song, cfg)

        if args.playlist_id is not None:
            if not cfg.has_playlist(args.playlist_id):
                output_warn(f"歌单 {args.playlist_id} 不存在，无法移除")
                return 1
            _cleanup_playlist_files(args.playlist_id, cfg)
            cfg.remove_playlist(args.playlist_id)
            output_success(f"已移除歌单：{args.playlist_id}")
            return 0

        if not has_songs:
            return _remove_playlist_interactive(cfg)

    elif args.command in ("list", "ls"):
        if getattr(args, "song", False):
            return _list_songs(cfg)

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
        from musicvault.adapters.providers.netease_client import NeteaseClient

        api = NeteaseClient()
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
    from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
    from musicvault.shared.utils import load_json, safe_filename

    playlist_index = load_json(cfg.state_dir / "playlists.json", {})
    entry = playlist_index.get(str(pid), {})
    name = entry.get("name")
    dir_name = safe_filename(str(name)) if name else safe_filename(str(pid))

    # 删除 library 目录（仅含硬链接，直接 rmtree）
    deleted_dirs = 0
    for preset in cfg.presets:
        target = cfg.preset_dir(preset.name) / dir_name
        if target.is_dir():
            shutil.rmtree(target)
            deleted_dirs += 1

    # 从 SQLite 移除该歌单（级联删除 playlist_tracks），并找出不再属于任何歌单的曲目
    repo = SQLiteStateRepository(SQLiteState(cfg.state_db_file))
    snapshot = repo.create_snapshot()
    playlist = snapshot.playlist(pid)
    ids_to_remove: set[int] = set()
    if playlist is not None:
        repo.remove_playlist(pid)
        for track_id in playlist.track_ids:
            # 该曲目是否还属于其他歌单（删除本歌单后）
            still_member = any(
                other.track_ids and track_id in other.track_ids for other in snapshot.playlists if other.id != pid
            )
            if not still_member:
                ids_to_remove.add(track_id)

    # 记录 canonical 文件的 inode（删除前），用于后续匹配 未分类 中的硬链接
    canonical_inodes: set[tuple[int, int]] = set()
    for track_id in ids_to_remove:
        for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus"):
            p = cfg.downloads_dir / f"{track_id}{ext}"
            if p.exists():
                try:
                    st = p.stat()
                    canonical_inodes.add((st.st_dev, st.st_ino))
                except OSError:
                    pass

    # 删除无歌单归属的 canonical 文件（覆盖所有格式及 bitrate 后缀变体）
    for track_id in ids_to_remove:
        for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".lrc"):
            (cfg.downloads_dir / f"{track_id}{ext}").unlink(missing_ok=True)
        if cfg.downloads_dir.is_dir():
            for f in list(cfg.downloads_dir.iterdir()):
                if f.is_file() and f.stem.startswith(f"{track_id}_"):
                    f.unlink(missing_ok=True)
        repo.remove_track(track_id)

    # 删除 未分类 中对应的硬链接（它们指向同一 inode，unlink 不会自动消失）
    if canonical_inodes:
        for preset in cfg.presets:
            uncat_dir = cfg.preset_dir(preset.name) / cfg.default_playlist_name
            if not uncat_dir.is_dir():
                continue
            for f in list(uncat_dir.iterdir()):
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                    if (st.st_dev, st.st_ino) in canonical_inodes:
                        f.unlink()
                except OSError:
                    continue
            # 删除空的 未分类 目录
            try:
                if not any(uncat_dir.iterdir()):
                    uncat_dir.rmdir()
            except OSError:
                pass

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

    from musicvault.adapters.providers.netease_client import NeteaseClient

    api = NeteaseClient()
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

    with transient_section():
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

    with transient_section():
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


# ---------------------------------------------------------------------------
# 单曲管理 (--song)
# ---------------------------------------------------------------------------


def _add_songs(song_ids: list[int], cfg: Config) -> None:
    added = 0
    for sid in song_ids:
        if cfg.has_song(sid):
            output_warn(f"单曲 {sid} 已存在，跳过")
            continue
        cfg.add_song(sid)
        output_success(f"已添加单曲：{sid}")
        added += 1
    if added == 0:
        output_info("未添加任何新单曲")


def _remove_songs(song_ids: list[int], cfg: Config) -> None:
    removed = 0
    for sid in song_ids:
        if not cfg.has_song(sid):
            output_warn(f"单曲 {sid} 不存在，跳过")
            continue
        cfg.remove_song(sid)
        # 删除 canonical 文件
        for ext in (".flac", ".mp3", ".lrc"):
            (cfg.downloads_dir / f"{sid}{ext}").unlink(missing_ok=True)
        output_success(f"已移除单曲：{sid}")
        removed += 1
    if removed == 0:
        output_info("未移除任何单曲")


def _list_songs(cfg: Config) -> int:
    song_ids = cfg.get_song_ids()
    if not song_ids:
        output_info("尚未添加任何单曲，请执行 msv add --song <ID> 添加")
        return 0

    from musicvault.shared.tui_progress import console as c

    table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)
    table.add_column(style="cyan")
    table.add_column(style="dim")
    for sid in song_ids:
        table.add_row(str(sid), "")
    c.print("[bold]当前管理的单曲：[/bold]")
    c.print(table, highlight=False)
    return 0

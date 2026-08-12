"""歌单/单曲管理命令（add/remove/list）的 CLI 呈现层。

管理逻辑由 application 用例（PlaylistUseCase）承载，API 客户端由
composition root（bootstrap）注入；本模块只做参数解析与输出。
"""

from __future__ import annotations

import argparse
import logging
import re
from urllib.parse import parse_qs, urlparse

from rich.table import Table

from musicvault.application.playlist_use_case import PlaylistUseCase
from musicvault.core.config import Config
from musicvault.ports.source import InteractiveLoginApi
from musicvault.shared.output import error as output_error
from musicvault.shared.output import info as output_info
from musicvault.shared.output import success as output_success
from musicvault.shared.output import warn as output_warn
from musicvault.shared.tui_progress import console, transient_section

logger = logging.getLogger(__name__)


def handle_playlist_mgmt(args: argparse.Namespace, cfg: Config) -> int:
    from musicvault.application.bootstrap import build_playlist_use_case, build_source_client

    use_case = build_playlist_use_case(cfg)
    api = build_source_client(cfg)

    if args.command == "add":
        cookie = getattr(args, "cookie", None) or cfg.cookie
        has_songs = bool(getattr(args, "song", None))
        inputs = getattr(args, "input", None) or []

        if has_songs:
            _add_songs(args.song, use_case)

        if not inputs and not has_songs:
            return _add_playlist_interactive(use_case, api, cookie)  # pragma: no cover - 交互流程，人工手动测试

        result = 0
        for raw in inputs:
            try:
                pid = _parse_playlist_id(raw)
            except RuntimeError as exc:
                output_error(str(exc))
                result = 1
                continue
            if _add_playlist_by_id(pid, cfg, use_case, cookie) != 0:
                result = 1

        return 0 if result == 0 and not (has_songs and not inputs) else result

    elif args.command == "remove" or args.command == "rm":
        has_songs = bool(getattr(args, "song", None))

        if has_songs:
            _remove_songs(args.song, use_case)

        if args.playlist_id is not None:
            if not use_case.has_playlist(args.playlist_id):
                output_warn(f"歌单 {args.playlist_id} 不存在，无法移除")
                return 1
            use_case.remove_playlist(args.playlist_id)
            output_success(f"已移除歌单：{args.playlist_id}")
            return 0

        if not has_songs:
            return _remove_playlist_interactive(use_case)  # pragma: no cover - 交互流程，人工手动测试

    elif args.command in ("list", "ls"):
        if getattr(args, "song", False):
            return _list_songs(use_case)

        playlists = use_case.list_playlists()
        if playlists:
            table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)
            table.add_column(style="cyan")
            table.add_column(style="dim")
            table.add_column(style="dim")
            for pl in playlists:
                table.add_row(str(pl.id), pl.name or "", f"{len(pl.track_ids)} 首")
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


def _fetch_playlist_info(pid: int, cfg: Config, cookie: str | None) -> dict[str, object] | None:
    if not cookie:
        return None
    try:
        from musicvault.application.bootstrap import build_source_client

        api = build_source_client(cfg)
        api.login_with_cookie(cookie)
        return dict(api.get_playlist_info(pid))
    except Exception:
        return None


def _add_playlist_by_id(
    pid: int,
    cfg: Config,
    use_case: PlaylistUseCase,
    cookie: str | None,
) -> int:
    if use_case.has_playlist(pid):
        playlist = use_case.get_playlist(pid)
        name = playlist.name if playlist else ""
        label = f"{name} ({pid})" if name else str(pid)
        logger.warning(f"歌单 {label} 已存在，跳过添加")
        return 1

    info = _fetch_playlist_info(pid, cfg, cookie)

    if info is None:
        if not cookie:
            logger.warning("未提供 cookie，跳过 API 验证")
        else:
            logger.warning("无法获取歌单信息，将仅保存 ID")

    name = str(info["name"]) if info and info.get("name") else ""
    use_case.add_playlist(pid, name=name)

    if name:
        output_success(f"已添加歌单：[bold]{name}[/bold] [dim]({pid})[/dim]")
    else:
        output_success(f"已添加歌单：{pid}")
    return 0


def _add_playlist_interactive(  # pragma: no cover - 交互流程（input 选择），人工手动测试
    use_case: PlaylistUseCase,
    api: InteractiveLoginApi,
    cookie: str | None,
) -> int:
    if not cookie:
        output_error("未提供 cookie，无法获取账号歌单列表")
        output_info('请先执行 msv sync 登录，或通过 msv add <ID> --cookie "..." 添加')
        return 1

    try:
        user = api.login_with_cookie(cookie)
        playlists = api.list_user_playlists(user.user_id)
    except Exception as exc:
        output_error(f"获取歌单列表失败：{exc}")
        return 1

    if not playlists:
        output_warn("当前账号没有歌单")
        return 1

    existing = {pl.id for pl in use_case.list_playlists()}
    available = [pl for pl in playlists if int(pl["id"]) not in existing]
    already_added = [pl for pl in playlists if int(pl["id"]) in existing]

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
        if use_case.has_playlist(pid):
            continue
        use_case.add_playlist(pid, name=pl["name"])
        output_success(f"已添加歌单：[bold]{pl['name']}[/bold] (ID: {pid})")
        added += 1

    return 0 if added > 0 else 1


def _remove_playlist_interactive(
    use_case: PlaylistUseCase,
) -> int:  # pragma: no cover - 交互流程（input 选择），人工手动测试
    playlists = use_case.list_playlists()
    if not playlists:
        output_info("尚未添加任何歌单，无需移除")
        return 1

    max_show = min(len(playlists), 50)

    with transient_section():
        console.print()
        console.print("[bold]当前管理的歌单：[/bold]")
        console.print()

        table = Table(show_header=False, box=None, padding=(0, 1), collapse_padding=True)
        table.add_column(justify="right", style="cyan")
        table.add_column(justify="left", max_width=40, no_wrap=True)
        table.add_column(justify="right", style="dim")

        for i, pl in enumerate(playlists[:max_show], 1):
            table.add_row(f"{i}.", pl.name or str(pl.id), f" {len(pl.track_ids)} 首")

        console.print(table, highlight=False)

        if len(playlists) > max_show:
            console.print(f"  [dim]... 还有 {len(playlists) - max_show} 个歌单未显示[/dim]")

        console.print()
        console.print("  输入编号选择要移除的歌单（如: 1,3,5 或 1-5 或 all），输入 q 取消")

        choice = input("  > ").strip()

    if choice.lower() == "q":
        output_info("已取消")
        return 1

    selected_indices = _parse_selection(choice, len(playlists))
    if not selected_indices:
        output_warn("未选择任何歌单")
        return 1

    removed = 0
    for idx in reversed(selected_indices):
        pid = playlists[idx - 1].id
        use_case.remove_playlist(pid)
        label = f"[bold]{playlists[idx - 1].name}[/bold] (ID: {pid})" if playlists[idx - 1].name else str(pid)
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


def _add_songs(song_ids: list[int], use_case: PlaylistUseCase) -> None:
    added = 0
    for sid in song_ids:
        if use_case.has_song(sid):
            output_warn(f"单曲 {sid} 已存在，跳过")
            continue
        use_case.add_song(sid)
        output_success(f"已添加单曲：{sid}")
        added += 1
    if added == 0:
        output_info("未添加任何新单曲")


def _remove_songs(song_ids: list[int], use_case: PlaylistUseCase) -> None:
    removed = 0
    for sid in song_ids:
        if not use_case.has_song(sid):
            output_warn(f"单曲 {sid} 不存在，跳过")
            continue
        use_case.remove_song(sid)
        output_success(f"已移除单曲：{sid}")
        removed += 1
    if removed == 0:
        output_info("未移除任何单曲")


def _list_songs(use_case: PlaylistUseCase) -> int:
    song_ids = use_case.list_songs()
    if not song_ids:
        output_info("尚未添加任何单曲，请执行 msv add --song <ID> 添加")
        return 0

    table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)
    table.add_column(style="cyan")
    table.add_column(style="dim")
    for sid in song_ids:
        table.add_row(str(sid), "")
    console.print("[bold]当前管理的单曲：[/bold]")
    console.print(table, highlight=False)
    return 0

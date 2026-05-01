from __future__ import annotations

import argparse
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
        if args.playlist_id not in cfg.playlist_ids:
            output_warn(f"歌单 {args.playlist_id} 不存在，无法移除")
            return 1
        cfg.playlist_ids.remove(args.playlist_id)
        cfg.save()
        _cleanup_playlist_files(args.playlist_id, cfg)
        output_success(f"已移除歌单：{args.playlist_id}")

    elif args.command in ("list", "ls"):
        if cfg.playlist_ids:
            cached = _load_playlist_index(cfg)
            table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)
            table.add_column(style="cyan")
            table.add_column(style="dim")
            for pid in cfg.playlist_ids:
                entry = cached.get(str(pid), {})
                name = entry.get("name")
                table.add_row(str(pid), name or "")
            console.print("[bold]当前管理的歌单：[/bold]")
            console.print(table, highlight=False)
        else:
            output_info("尚未添加任何歌单（将使用我喜欢的音乐）")
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

    deleted_dirs = 0
    for parent in (cfg.lossless_dir, cfg.lossy_dir):
        target = parent / dir_name
        if target.is_dir():
            shutil.rmtree(target)
            deleted_dirs += 1

    if str(pid) in playlist_index:
        del playlist_index[str(pid)]
        save_json(cfg.state_dir / "playlists.json", playlist_index)

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


def _add_playlist_by_id(pid: int, cfg: Config, cookie: str | None) -> int:
    if pid in cfg.playlist_ids:
        cached = _load_playlist_index(cfg)
        entry = cached.get(str(pid), {})
        name = entry.get("name")
        label = f"{name} ({pid})" if name else str(pid)
        output_warn(f"歌单 {label} 已存在，跳过添加")
        return 1

    info = _fetch_playlist_info(pid, cookie)

    if info is None:
        if not cookie:
            output_warn("未提供 cookie，跳过 API 验证")
        else:
            output_warn("无法获取歌单信息，将仅保存 ID")

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
        output_success(f"已添加歌单：[bold]{name}[/bold] (ID: {pid})")
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

    existing_ids = set(cfg.playlist_ids)
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
        if pid in cfg.playlist_ids:
            continue
        cfg.ensure_dirs()
        from musicvault.shared.utils import load_json, save_json

        index_path = cfg.state_dir / "playlists.json"
        cached = load_json(index_path, {})
        cached[str(pid)] = {"name": pl["name"], "track_count": pl.get("trackCount", pl.get("track_count", 0))}
        save_json(index_path, cached)

        cfg.playlist_ids.append(pid)
        output_success(f"已添加歌单：[bold]{pl['name']}[/bold] (ID: {pid})")
        added += 1

    cfg.save()
    return 0 if added > 0 else 1


def _remove_playlist_interactive(cfg: Config) -> int:
    if not cfg.playlist_ids:
        output_info("尚未添加任何歌单，无需移除")
        return 1

    cached = _load_playlist_index(cfg)
    max_show = min(len(cfg.playlist_ids), 50)

    console.print()
    console.print("[bold]当前管理的歌单：[/bold]")
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 1), collapse_padding=True)
    table.add_column(justify="right", style="cyan")
    table.add_column(justify="left", max_width=40, no_wrap=True)
    table.add_column(justify="right", style="dim")

    for i, pid in enumerate(cfg.playlist_ids[:max_show], 1):
        entry = cached.get(str(pid), {})
        name = entry.get("name", "")
        track_count = entry.get("track_count", "?")
        table.add_row(f"{i}.", name or str(pid), f" {track_count} 首")

    console.print(table, highlight=False)

    if len(cfg.playlist_ids) > max_show:
        console.print(f"  [dim]... 还有 {len(cfg.playlist_ids) - max_show} 个歌单未显示[/dim]")

    console.print()
    console.print("  输入编号选择要移除的歌单（如: 1,3,5 或 1-5 或 all），输入 q 取消")

    choice = input("  > ").strip()
    if choice.lower() == "q":
        output_info("已取消")
        return 1

    selected_indices = _parse_selection(choice, len(cfg.playlist_ids))
    if not selected_indices:
        output_warn("未选择任何歌单")
        return 1

    removed = 0
    for idx in reversed(selected_indices):
        pid = cfg.playlist_ids[idx - 1]
        cfg.playlist_ids.remove(pid)
        _cleanup_playlist_files(pid, cfg)
        entry = cached.get(str(pid), {})
        name = entry.get("name")
        label = f"[bold]{name}[/bold] (ID: {pid})" if name else str(pid)
        output_success(f"已移除歌单：{label}")
        removed += 1

    cfg.save()
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

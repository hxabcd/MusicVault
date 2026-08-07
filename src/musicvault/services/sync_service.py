from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.application.source_state import SourceStateRecorder
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.core.preset import Preset, audio_spec_key
from musicvault.domain.models import Playlist
from musicvault.ports.state import StateRepository
from musicvault.shared.output import warn as output_warn
from musicvault.shared.tui_progress import BatchProgress, console
from musicvault.shared.utils import (
    create_link,
    format_track_name,
    load_json,
    remove_link,
    safe_filename,
    save_json,
    workspace_rel_path,
)

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(
        self,
        cfg: Config,
        api: NeteaseClient,
        downloader: Downloader,
        workers: int,
        state: StateRepository,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.downloader = downloader
        self.workers = max(1, workers)
        self.dry_run = dry_run
        # 把本次 sync 的源侧状态写入 SQLite，供 target-sync 消费
        self.recorder = SourceStateRecorder(state)
        # dry-run 计划（仅 dry_run 模式下填充）：with_url / no_url / pruned / moves / renames / stale_index
        self.plan: dict = {}

    def _load_synced_state(self) -> dict[int, list[int]]:
        """从 SQLite 快照派生 {track_id: [playlist_ids]} 映射。

        替代旧 synced_tracks.json：曲目与歌单关系由 SourceStateRecorder
        在 sync 完成后写入，这里只读。无歌单归属的单独管理单曲保留空列表。
        """
        snapshot = self.recorder.state.create_snapshot()
        state_map: dict[int, list[int]] = {}
        for playlist in snapshot.playlists:
            for track_id in playlist.track_ids:
                state_map.setdefault(track_id, []).append(playlist.id)
        for track in snapshot.tracks:
            state_map.setdefault(track.id, [])
        return state_map

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_sync(self, cookie: str, playlist_ids: list[int]) -> list[DownloadedTrack]:
        stale_index = self._cleanup_stale_state()
        song_ids = self.cfg.get_song_ids()
        if not playlist_ids and not song_ids:
            output_warn("未配置任何歌单或单曲，请先执行 msv add 添加歌单或 msv add --song <ID> 添加单曲")
            return []

        self.api.login_with_cookie(cookie)
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        track_playlists: dict[int, list[int]] = {}
        all_tracks: dict[int, Track] = {}
        pending_renames: list[tuple[int, str, str]] = []
        playlist_track_order: dict[int, list[int]] = {}

        if playlist_ids:
            logger.info("将同步 %s 个歌单", len(playlist_ids))
            for pid in playlist_ids:
                info = self.api.get_playlist_info(pid)
                old_entry = playlist_index.get(str(pid))
                old_name = old_entry.get("name") if old_entry else None
                new_name = info["name"]
                if old_name and old_name != new_name:
                    pending_renames.append((pid, old_name, new_name))
                playlist_index[str(pid)] = {"name": info["name"], "track_count": info["track_count"]}
                tracks = self.api.get_playlist_tracks(pid)
                playlist_track_order[pid] = [track.id for track in tracks]
                for track in tracks:
                    all_tracks[track.id] = track
                    track_playlists.setdefault(track.id, []).append(pid)

        if pending_renames and not self.dry_run:
            for pid, old_name, new_name in pending_renames:
                self._handle_playlist_rename(pid, old_name, new_name, all_tracks)

        # 获取单独管理的单曲
        if song_ids:
            logger.info("将同步 %s 首单独管理的单曲", len(song_ids))
            song_details = self.api.get_tracks_detail(song_ids)
            for sid, track in song_details.items():
                if track.id not in all_tracks:
                    all_tracks[track.id] = track
                    track_playlists.setdefault(track.id, [])
                # 过滤本地已删除但仍在 songs.json 中的旧 ID
                missing = sorted(set(song_ids) - set(song_details.keys()))
                if missing:
                    for mid in missing:
                        self.cfg.remove_song(mid)
                    logger.info("清理无效单曲 ID：%s", missing)

        if not self.dry_run:
            save_json(self.cfg.state_dir / "playlists.json", playlist_index)
        self.playlist_index = playlist_index

        unique = list(all_tracks.values())
        logger.info("歌单曲目合计：%s 首（去重后）", len(unique))

        # 协调已有曲目的歌单分配变化（移动/链接文件），dry-run 下仅计算不执行
        moves = self._reconcile_playlist_assignments(track_playlists, playlist_index, all_tracks)

        pruned_count, pruned_ids = self._prune_stale_tracks(all_tracks)
        new_tracks, synced_ids = self._diff_tracks(unique)

        if self.dry_run:
            with_url, no_url = self._resolve_dry_urls(new_tracks)
            self.plan = {
                "with_url": with_url,
                "no_url": no_url,
                "pruned": pruned_ids,
                "moves": moves,
                "renames": pending_renames,
                "stale_index": stale_index,
            }
            self._print_dry_run_plan(unique_count=len(unique), n_playlists=len(playlist_ids) + (1 if song_ids else 0))
            return []

        downloaded = self._sync_tracks(new_tracks, track_playlists)
        self._record_source_state(all_tracks, playlist_track_order, playlist_index, song_ids)

        # 单行摘要
        added = len(downloaded)
        n_playlists = len(playlist_ids) + (1 if song_ids else 0)
        console.print(f"  从 [cyan]{n_playlists}[/cyan] 个歌单同步 [cyan]{len(unique)}[/cyan] 首")
        stats: list[str] = []
        if added:
            stats.append(f"[green]+{added} 首[/green]")
        if pruned_count:
            stats.append(f"[red]-{pruned_count} 首[/red]")
        console.print("    " + " | ".join(stats) if stats else "    [dim]无变化[/dim]")

        return downloaded

    def _record_source_state(
        self,
        all_tracks: dict[int, Track],
        playlist_track_order: dict[int, list[int]],
        playlist_index: dict[str, dict[str, object]],
        song_ids: list[int],
    ) -> None:
        """把本次 sync 的曲目、歌单关系与单独管理的单曲写入 SQLite。"""
        playlists: list[Playlist] = []
        for pid_str, entry in playlist_index.items():
            if not pid_str.lstrip("-").isdigit() or not isinstance(entry, dict):
                continue
            pid = int(pid_str)
            name = str(entry.get("name") or pid)
            playlists.append(Playlist(pid, name, tuple(playlist_track_order.get(pid, ()))))
        # 只登记本次已获取详情的单曲，避免陈旧 song_id 违反外键约束
        managed_songs = [song_id for song_id in song_ids if song_id in all_tracks]
        self.recorder.record_source_state(all_tracks.values(), playlists, managed_songs)

    def _cleanup_stale_state(self) -> int:
        """清理 canonical 文件已不存在的过期状态，避免阻止重新下载。

        检查 SQLite media_assets 中每个 audio 资产的 canonical 文件是否仍存在，
        不存在则删除该曲目（级联清理 processed_tracks / pending_files / 关系）。
        返回过期曲目数量；dry-run 模式下只计算并上报，不写入任何数据。
        """
        snapshot = self.recorder.state.create_snapshot()
        if not snapshot.media_assets:
            return 0

        stale_ids: set[int] = set()
        for asset in snapshot.media_assets:
            if asset.asset_type != "audio":
                continue
            if not asset.path.exists():
                stale_ids.add(asset.track_id)

        if not stale_ids:
            return 0

        if self.dry_run:
            logger.info("dry-run：将清理 %s 条过期索引条目", len(stale_ids))
            return len(stale_ids)

        for sid in stale_ids:
            self.recorder.state.remove_track(sid)
        logger.info("清理过期状态：%s 个文件已不存在，已从索引中移除", len(stale_ids))
        return len(stale_ids)

    def _handle_playlist_rename(self, pid: int, old_name: str, new_name: str, all_tracks: dict[int, Track]) -> None:
        old_safe = safe_filename(old_name)
        new_safe = safe_filename(new_name)
        if old_safe == new_safe:
            return

        # 删除旧 library 目录（仅含硬链接，直接 rmtree）
        for preset in self.cfg.presets:
            old_dir = self.cfg.preset_dir(preset.name) / old_safe
            if old_dir.is_dir():
                shutil.rmtree(old_dir)

        # 重建新目录中的硬链接
        state_map = self._load_synced_state()
        for track_id, pids in state_map.items():
            if pid not in pids:
                continue
            track = all_tracks.get(track_id)
            if track is None:
                continue

            for preset in self.cfg.presets:
                spec_key = audio_spec_key(preset.format, preset.bitrate)
                audio_src = self._find_canonical_for_spec(track_id, spec_key)
                if not audio_src:
                    continue
                dst = self.cfg.preset_dir(preset.name) / new_safe / self._link_name(track, preset, audio_src.suffix)
                create_link(audio_src, dst)

                if preset.write_lrc_file:
                    lrc_src = audio_src.with_name(f"{track_id}.{preset.name}.lrc")
                    if lrc_src.exists():
                        create_link(lrc_src, dst.with_suffix(".lrc"))

        logger.info("歌单 '%s' 已重命名为 '%s'，已迁移本地目录", old_name, new_name)

    # ------------------------------------------------------------------
    # 歌单分配协调
    # ------------------------------------------------------------------

    def _reconcile_playlist_assignments(
        self,
        track_playlists: dict[int, list[int]],
        playlist_index: dict[str, dict[str, object]],
        all_tracks: dict[int, Track],
    ) -> list[tuple[Track, set[str], set[str]]]:
        """对比 API 返回的歌单分配与本地存储，删旧链接 + 建新链接。

        返回需要调整的曲目列表 [(track, 需删除的目录, 需新增的目录)]；
        dry-run 模式下只计算并返回，不执行任何文件操作、不写状态。
        """
        old_map = self._load_synced_state()
        if not old_map:
            return []

        moves: list[tuple[Track, set[str], set[str]]] = []
        audio_maps: dict[int, dict[str, Path]] = {}

        for track_id, old_pids in old_map.items():
            new_pids = track_playlists.get(track_id, [])
            if not new_pids or old_pids == new_pids:
                continue

            old_names = {self._pid_to_dirname(pid, playlist_index) for pid in old_pids}
            new_names = {self._pid_to_dirname(pid, playlist_index) for pid in new_pids}
            if old_names == new_names:
                continue

            track = all_tracks.get(track_id)
            if track is None:
                continue

            rm_names, add_names = old_names - new_names, new_names - old_names

            # 需要新建链接时必须存在 canonical 源文件，否则整条跳过（与旧行为一致）
            if add_names:
                audio_map: dict[str, Path] = {}
                for preset in self.cfg.presets:
                    spec_key = audio_spec_key(preset.format, preset.bitrate)
                    if spec_key not in audio_map:
                        src = self._find_canonical_for_spec(track_id, spec_key)
                        if src:
                            audio_map[spec_key] = src
                if not audio_map:
                    continue
                audio_maps[track_id] = audio_map

            moves.append((track, rm_names, add_names))

        if not self.dry_run:
            for track, rm_names, add_names in moves:
                # 删除已移除歌单的链接
                for name in rm_names:
                    self._remove_track_links(track, name)
                # 创建新增歌单的链接
                if add_names:
                    for name in add_names:
                        self._create_track_links(audio_maps[track.id], track, name)

            # 歌单分配以 run_sync 末尾的 _record_source_state（playlist_track_order）为准，
            # 这里无需再写回旧 JSON 状态。

        return moves

    def _create_track_links(self, audio_map: dict[str, Path], track: Track, dirname: str) -> None:
        """在 library 中各 preset 目录下创建硬链接（人类可读文件名）。"""
        for preset in self.cfg.presets:
            spec_key = audio_spec_key(preset.format, preset.bitrate)
            audio_src = audio_map.get(spec_key)
            if not audio_src:
                continue
            dst_dir = self.cfg.preset_dir(preset.name) / dirname
            dst_dir.mkdir(parents=True, exist_ok=True)
            create_link(audio_src, dst_dir / self._link_name(track, preset, audio_src.suffix))
            if preset.write_lrc_file:
                lrc_src = audio_src.with_name(f"{track.id}.{preset.name}.lrc")
                if lrc_src.exists():
                    create_link(lrc_src, dst_dir / self._link_name(track, preset, ".lrc"))

    def _remove_track_links(self, track: Track, dirname: str) -> None:
        """删除 library 中各 preset 目录下的硬链接。"""
        for preset in self.cfg.presets:
            p_dir = self.cfg.preset_dir(preset.name)
            if preset.format:
                ext_map = {"flac": ".flac", "mp3": ".mp3", "aac": ".m4a", "ogg": ".ogg", "opus": ".opus"}
                ext = ext_map.get(preset.format, f".{preset.format}")
                remove_link(p_dir / dirname / self._link_name(track, preset, ext))
            else:
                # ORIGINAL spec：不确定扩展名，尝试常见格式
                for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus"):
                    remove_link(p_dir / dirname / self._link_name(track, preset, ext))
            if preset.write_lrc_file:
                remove_link(p_dir / dirname / self._link_name(track, preset, ".lrc"))

    def _find_canonical_for_spec(self, track_id: int, spec_key: str) -> Path | None:
        """查找符合指定 spec_key 的 canonical 文件（downloads 目录中）。"""
        if spec_key == "ORIGINAL":
            for ext in (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"):
                p = self.cfg.downloads_dir / f"{track_id}{ext}"
                if p.exists():
                    return p
            return None

        parts = spec_key.split("-", 1)
        fmt = parts[0].lower()
        bitrate = parts[1] if len(parts) > 1 else None
        ext_map = {"flac": ".flac", "mp3": ".mp3", "aac": ".m4a", "ogg": ".ogg", "opus": ".opus"}
        ext = ext_map.get(fmt, f".{fmt}")

        # 先尝试带 bitrate 后缀，再尝试无 bitrate
        candidates: list[str] = []
        if bitrate:
            candidates.append(f"{track_id}_{bitrate}{ext}")
        candidates.append(f"{track_id}{ext}")

        for name in candidates:
            p = self.cfg.downloads_dir / name
            if p.exists():
                return p
        return None

    # ------------------------------------------------------------------
    # 链接文件名（与 ProcessService 保持一致）
    # ------------------------------------------------------------------

    def _link_name(self, track: Track, preset: Preset, suffix: str = "") -> str:
        stem = format_track_name(preset.filename_template, track)
        return stem + suffix

    def _pid_to_dirname(self, pid: int, playlist_index: dict[str, dict[str, object]]) -> str:
        """将 playlist_id 映射为安全的目录名。"""
        entry = playlist_index.get(str(pid))
        name = str(entry["name"]) if entry and entry.get("name") else str(pid)
        return safe_filename(name)

    def _diff_tracks(self, tracks: list[Track]) -> tuple[list[Track], set[int]]:
        """返回 (新增曲目, 已同步的 track_id 集合)，已同步集合来自 SQLite 快照。"""
        state_map = self._load_synced_state()
        synced_ids = set(state_map.keys())
        new_tracks = [track for track in tracks if track.id not in synced_ids]
        return new_tracks, synced_ids

    def _resolve_dry_urls(self, tracks: list[Track]) -> tuple[list[Track], list[Track]]:
        """dry-run：批量查询直链，返回 (可下载, 无直链) 两组曲目。"""
        if not tracks:
            return [], []
        url_map = self.api.get_tracks_download_urls([track.id for track in tracks])
        with_url = [t for t in tracks if url_map.get(t.id)]
        no_url = [t for t in tracks if not url_map.get(t.id)]
        return with_url, no_url

    def _print_dry_run_plan(self, unique_count: int, n_playlists: int) -> None:
        """输出 dry-run 计划预览（仅查询，未执行任何写操作）。"""
        plan = self.plan
        console.print(
            f"  从 [cyan]{n_playlists}[/cyan] 个歌单同步 [cyan]{unique_count}[/cyan] 首（[bold yellow]dry-run 预览[/bold yellow]）"
        )

        with_url: list[Track] = plan.get("with_url") or []
        no_url: list[Track] = plan.get("no_url") or []
        pruned: list[int] = plan.get("pruned") or []
        moves: list = plan.get("moves") or []
        renames: list = plan.get("renames") or []
        stale_index: int = plan.get("stale_index") or 0

        if with_url:
            console.print(f"  [green]将下载[/green] [cyan]{len(with_url)}[/cyan] 首：")
            for i, t in enumerate(with_url, 1):
                console.print(f"    [dim]{i:>3}.[/dim] {t.artist_text} - {t.name}")
        else:
            console.print("  [dim]将下载 0 首（无新增曲目）[/dim]")

        if no_url:
            console.print(f"  [yellow]无可用直链将跳过[/yellow] [cyan]{len(no_url)}[/cyan] 首：")
            for i, t in enumerate(no_url, 1):
                console.print(f"    [dim]{i:>3}.[/dim] {t.artist_text} - {t.name}")

        if pruned:
            console.print(
                f"  [red]将清理远端已删除曲目[/red] [cyan]{len(pruned)}[/cyan] 首：{', '.join(map(str, pruned))}"
            )

        if renames:
            console.print("  [cyan]歌单目录将重命名：[/cyan]")
            for _pid, old, new in renames:
                console.print(f"    [dim]-[/dim] {old} → {new}")

        if moves:
            console.print(f"  [cyan]歌单归属调整：[/cyan][cyan]{len(moves)}[/cyan] 首曲目的 library 链接将移动")

        if stale_index:
            console.print(f"  [yellow]将清理 {stale_index} 条本地文件缺失的过期索引[/yellow]")

    def _prune_stale_tracks(self, remote_tracks: dict[int, Track]) -> tuple[int, list[int]]:
        """删除远端已不存在的本地曲目（canonical 文件 + library 链接）。

        返回 (清理数量, stale_ids)；dry-run 模式下只计算并上报，不删除文件、不写状态。
        """
        state_map = self._load_synced_state()
        synced_ids = set(state_map.keys())
        stale_ids = sorted(synced_ids - set(remote_tracks.keys()))
        if not stale_ids:
            return 0, []

        if self.dry_run:
            logger.info("dry-run：将清理远端已删除曲目 %s 首", len(stale_ids))
            return len(stale_ids), stale_ids

        removed_count = 0
        for track_id in stale_ids:
            # 收集 canonical 文件 inode（删除前）
            canonical_inodes: set[tuple[int, int]] = set()
            for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus"):
                p = self.cfg.downloads_dir / f"{track_id}{ext}"
                if p.exists():
                    try:
                        st = p.stat()
                        canonical_inodes.add((st.st_dev, st.st_ino))
                    except OSError:
                        pass

            # 删除 canonical 文件
            for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".lrc"):
                (self.cfg.downloads_dir / f"{track_id}{ext}").unlink(missing_ok=True)
            # 删除带 bitrate 后缀的 canonical 文件（如 12345_192k.mp3）
            if self.cfg.downloads_dir.is_dir():
                for f in list(self.cfg.downloads_dir.iterdir()):
                    if f.is_file() and f.stem.startswith(f"{track_id}_"):
                        f.unlink(missing_ok=True)

            # 通过 inode 匹配删除所有 preset 目录下的 library 链接
            if canonical_inodes:
                for preset in self.cfg.presets:
                    parent = self.cfg.preset_dir(preset.name)
                    if not parent.is_dir():
                        continue
                    for pl_dir in parent.iterdir():
                        if not pl_dir.is_dir():
                            continue
                        for f in list(pl_dir.iterdir()):
                            if not f.is_file():
                                continue
                            try:
                                st = f.stat()
                                if (st.st_dev, st.st_ino) in canonical_inodes:
                                    f.unlink()
                            except OSError:
                                continue

            state_map.pop(track_id, None)
            removed_count += 1

        if removed_count:
            for track_id in stale_ids:
                self.recorder.state.remove_track(track_id)
            logger.info("清理远端已删除曲目：%s 首", removed_count)
        return removed_count, stale_ids

    def _sync_tracks(self, tracks: list[Track], track_playlists: dict[int, list[int]]) -> list[DownloadedTrack]:
        if not tracks:
            logger.info("同步阶段无新增曲目，跳过下载")
            return []

        url_map = self.api.get_tracks_download_urls([track.id for track in tracks])
        pending: list[tuple[Track, str]] = []
        skipped = 0
        for track in tracks:
            url = url_map.get(track.id)
            if not url:
                skipped += 1
                logger.info("跳过下载：无可用直链 track_id=%s name=%s", track.id, track.name)
                continue
            pending.append((track, url))
        logger.info("下载准备完成：可下载=%s 跳过=%s", len(pending), skipped)

        downloaded = self._run_download_batch(pending, track_playlists)
        # 写入 raw→track 映射（供 process 阶段从文件名反查 track_id）
        for item in downloaded:
            self.recorder.state.upsert_track(item.track)
            rel = workspace_rel_path(Path(item.source_file), self.cfg.workspace_path)
            self.recorder.state.add_pending_file(rel, item.track.id)
        return downloaded

    def _run_download_batch(
        self,
        tasks: list[tuple[Track, str]],
        track_playlists: dict[int, list[int]],
    ) -> list[DownloadedTrack]:
        if not tasks:
            logger.info("下载队列为空，无需执行")
            return []

        total = len(tasks)
        workers = min(self.workers, total)
        results: list[DownloadedTrack] = []

        with ThreadPoolExecutor(max_workers=workers) as pool, BatchProgress(total=total, phase="下载中") as bp:
            future_map = {
                pool.submit(self.downloader.download_track, track, url, self.cfg.downloads_cache_dir): (idx, track)
                for idx, (track, url) in enumerate(tasks, start=1)
            }
            try:
                for future in as_completed(future_map):
                    idx, track = future_map[future]
                    try:
                        item = future.result()
                        item.playlist_ids = track_playlists.get(track.id, [])
                        results.append(item)
                        bp.advance(success=True, idx=idx, item_name=track.name)
                    except Exception as exc:
                        bp.advance(success=False, idx=idx, item_name=track.name)
                        logger.error("下载失败：#%s %s，原因：%s", idx, track.name, exc, exc_info=True)
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                if results:
                    self._save_partial_downloads(results)
                raise

        return results

    def _save_partial_downloads(self, results: list[DownloadedTrack]) -> None:
        """中断时把已下载曲目登记到 SQLite，供下次 sync 跳过。"""
        # 曲目本体先入 SQLite；歌单关系由下次 sync 的 _record_source_state 重建
        for item in results:
            self.recorder.state.upsert_track(item.track)
            rel = workspace_rel_path(Path(item.source_file), self.cfg.workspace_path)
            self.recorder.state.add_pending_file(rel, item.track.id)

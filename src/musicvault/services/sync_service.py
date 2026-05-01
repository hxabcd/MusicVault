from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
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
    def __init__(self, cfg: Config, api: PyncmClient, downloader: Downloader, workers: int) -> None:
        self.cfg = cfg
        self.api = api
        self.downloader = downloader
        self.workers = max(1, workers)

    # ------------------------------------------------------------------
    # 同步状态持久化（synced_tracks.json）
    # ------------------------------------------------------------------

    @staticmethod
    def _load_synced_state(cfg: Config) -> dict[int, list[int]]:
        """加载 synced_tracks.json，自动迁移旧格式。

        旧格式: {"ids": [1, 2, 3]}
        新格式: {"ids": {"1": [10, 20], "2": [10]}}
        返回: {track_id: [playlist_ids]}
        """
        raw = load_json(cfg.synced_state_file, {})
        ids = raw.get("ids", [])
        if isinstance(ids, list):
            return {int(x): [] for x in ids if isinstance(x, (int, str))}
        # 新格式
        return {int(k): [int(p) for p in v] for k, v in ids.items()}

    @staticmethod
    def _save_synced_state(cfg: Config, state_map: dict[int, list[int]]) -> None:
        """将 {track_id: [playlist_ids]} 写入 synced_tracks.json（新格式）。"""
        ids: dict[str, list[int]] = {}
        for tid, pids in sorted(state_map.items()):
            ids[str(tid)] = sorted(pids)
        save_json(cfg.synced_state_file, {"ids": ids})

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_sync(self, cookie: str, playlist_ids: list[int]) -> list[DownloadedTrack]:
        self._cleanup_stale_state()
        song_ids = self.cfg.get_song_ids()
        if not playlist_ids and not song_ids:
            output_warn("未配置任何歌单或单曲，请先执行 msv add 添加歌单或 msv add --song <ID> 添加单曲")
            return []

        self.api.login_with_cookie(cookie)
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        track_playlists: dict[int, list[int]] = {}
        all_tracks: dict[int, Track] = {}
        pending_renames: list[tuple[int, str, str]] = []

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
                for track in tracks:
                    all_tracks[track.id] = track
                    track_playlists.setdefault(track.id, []).append(pid)

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

        save_json(self.cfg.state_dir / "playlists.json", playlist_index)
        self.playlist_index = playlist_index

        unique = list(all_tracks.values())
        logger.info("歌单曲目合计：%s 首（去重后）", len(unique))

        # 协调已有曲目的歌单分配变化（移动/链接文件）
        self._reconcile_playlist_assignments(track_playlists, playlist_index, all_tracks)

        pruned = self._prune_stale_tracks(all_tracks)
        new_tracks, synced_ids = self._diff_tracks(unique)
        downloaded = self._sync_tracks(new_tracks, track_playlists)
        self._mark_synced(downloaded, synced_ids, track_playlists)

        # 单行摘要
        added = len(downloaded)
        n_playlists = len(playlist_ids) + (1 if song_ids else 0)
        console.print(f"  从 [cyan]{n_playlists}[/cyan] 个歌单同步 [cyan]{len(unique)}[/cyan] 首")
        stats: list[str] = []
        if added:
            stats.append(f"[green]+{added} 首[/green]")
        if pruned:
            stats.append(f"[red]-{pruned} 首[/red]")
        console.print("    " + " | ".join(stats) if stats else "    [dim]无变化[/dim]")

        return downloaded

    def _cleanup_stale_state(self) -> None:
        """清理 canonical 文件已不存在的过期索引条目，避免阻止重新下载。

        processed_files.json 格式：key 为 track_id 字符串，value 含 flac/mp3/lrc 路径。
        检查对应文件是否存在，不存在则从索引中移除。
        """
        processed = load_json(self.cfg.processed_state_file, {})
        if not isinstance(processed, dict) or not processed:
            return

        stale_ids: set[int] = set()
        for key, value in list(processed.items()):
            if not isinstance(value, dict):
                continue

            flac_rel = value.get("flac") or value.get("lossless")
            mp3_rel = value.get("mp3")
            source_rel = value.get("source")

            flac_exists = isinstance(flac_rel, str) and (self.cfg.workspace_path / flac_rel).exists()
            mp3_exists = isinstance(mp3_rel, str) and (self.cfg.workspace_path / mp3_rel).exists()
            source_exists = isinstance(source_rel, str) and (self.cfg.workspace_path / source_rel).exists()

            if flac_exists or mp3_exists or source_exists:
                continue

            try:
                stale_ids.add(int(key))
            except (TypeError, ValueError):
                pass
            del processed[key]

        if stale_ids:
            save_json(self.cfg.processed_state_file, processed)
            state_map = self._load_synced_state(self.cfg)
            existing = set(state_map.keys())
            cleaned = existing - stale_ids
            if cleaned != existing:
                for sid in stale_ids:
                    state_map.pop(sid, None)
                self._save_synced_state(self.cfg, state_map)
                logger.info("清理过期状态：%s 个文件已不存在，已从索引中移除", len(stale_ids))

    def _handle_playlist_rename(self, pid: int, old_name: str, new_name: str, all_tracks: dict[int, Track]) -> None:
        old_safe = safe_filename(old_name)
        new_safe = safe_filename(new_name)
        if old_safe == new_safe:
            return

        # 删除旧 library 目录（仅含硬链接，直接 rmtree）
        for parent in (self.cfg.lossless_dir, self.cfg.lossy_dir):
            old_dir = parent / old_safe
            if old_dir.is_dir():
                shutil.rmtree(old_dir)

        # 重建新目录中的硬链接
        state_map = self._load_synced_state(self.cfg)
        for track_id, pids in state_map.items():
            if pid not in pids:
                continue
            track = all_tracks.get(track_id)
            if track is None:
                continue
            flac_src = self._find_lossless_canonical(track_id)
            mp3_src = self.cfg.downloads_dir / f"{track_id}.mp3"
            if not flac_src or not mp3_src.exists():
                continue
            ll_dst = self.cfg.lossless_dir / new_safe / self._lossless_link_name(track, flac_src.suffix)
            ly_dst = self.cfg.lossy_dir / new_safe / self._lossy_link_name(track)
            create_link(flac_src, ll_dst)
            create_link(mp3_src, ly_dst)
            lrc_src = mp3_src.with_suffix(".lrc")
            if lrc_src.exists():
                create_link(lrc_src, ly_dst.with_suffix(".lrc"))

        logger.info("歌单 '%s' 已重命名为 '%s'，已迁移本地目录", old_name, new_name)

    # ------------------------------------------------------------------
    # 歌单分配协调
    # ------------------------------------------------------------------

    def _reconcile_playlist_assignments(
        self,
        track_playlists: dict[int, list[int]],
        playlist_index: dict[str, dict[str, object]],
        all_tracks: dict[int, Track],
    ) -> None:
        """对比 API 返回的歌单分配与本地存储，删旧链接 + 建新链接。"""
        old_map = self._load_synced_state(self.cfg)
        if not old_map:
            return

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

            flac_src = self._find_lossless_canonical(track_id)
            mp3_src = self.cfg.downloads_dir / f"{track_id}.mp3"
            if not flac_src or not mp3_src.exists():
                continue

            # 删除已移除歌单的链接
            for name in old_names - new_names:
                self._remove_track_links(track, name)

            # 创建新增歌单的链接
            for name in new_names - old_names:
                self._create_track_links(flac_src, mp3_src, track, name)

        # 写回更新后的歌单分配
        new_map = dict(old_map)
        for track_id, new_pids in track_playlists.items():
            if track_id in old_map:
                new_map[track_id] = sorted(new_pids)
        self._save_synced_state(self.cfg, new_map)

    def _create_track_links(self, flac_src: Path, mp3_src: Path, track: Track, dirname: str) -> None:
        """在 library 中为一个歌单目录创建硬链接（人类可读文件名）。"""
        ll_dir = self.cfg.lossless_dir / dirname
        ly_dir = self.cfg.lossy_dir / dirname
        create_link(flac_src, ll_dir / self._lossless_link_name(track, flac_src.suffix))
        create_link(mp3_src, ly_dir / self._lossy_link_name(track))
        lrc_src = mp3_src.with_suffix(".lrc")
        if lrc_src.exists():
            create_link(lrc_src, ly_dir / self._lossy_link_name(track, suffix=".lrc"))

    def _remove_track_links(self, track: Track, dirname: str) -> None:
        """删除 library 中一个歌单目录下的硬链接（尝试 .flac / .mp3 两种扩展名）。"""
        for suffix in (".flac", ".mp3"):
            remove_link(self.cfg.lossless_dir / dirname / self._lossless_link_name(track, suffix))
        ly_name = self._lossy_link_name(track)
        remove_link(self.cfg.lossy_dir / dirname / ly_name)
        remove_link(self.cfg.lossy_dir / dirname / ly_name.replace(".mp3", ".lrc"))

    def _find_lossless_canonical(self, track_id: int) -> Path | None:
        """查找 lossless canonical 文件（.flac 或 .mp3）。"""
        for ext in (".flac", ".mp3"):
            p = self.cfg.downloads_dir / f"{track_id}{ext}"
            if p.exists():
                return p
        return None

    # ------------------------------------------------------------------
    # 链接文件名（与 ProcessService 保持一致）
    # ------------------------------------------------------------------

    def _lossless_link_name(self, track: Track, suffix: str = ".flac") -> str:
        stem = format_track_name(self.cfg.filename_lossless, track)
        return stem + suffix

    def _lossy_link_name(self, track: Track, suffix: str = ".mp3") -> str:
        stem = format_track_name(self.cfg.filename_lossy, track)
        return stem + suffix

    def _pid_to_dirname(self, pid: int, playlist_index: dict[str, dict[str, object]]) -> str:
        """将 playlist_id 映射为安全的目录名。"""
        entry = playlist_index.get(str(pid))
        name = str(entry["name"]) if entry and entry.get("name") else str(pid)
        return safe_filename(name)

    def _diff_tracks(self, tracks: list[Track]) -> tuple[list[Track], set[int]]:
        """返回 (新增曲目, 已同步的 track_id 集合)，调用方可将集合传给 _mark_synced 避免重复加载。"""
        state_map = self._load_synced_state(self.cfg)
        synced_ids = set(state_map.keys())
        new_tracks = [track for track in tracks if track.id not in synced_ids]
        return new_tracks, synced_ids

    def _prune_stale_tracks(self, remote_tracks: dict[int, Track]) -> int:
        """删除远端已不存在的本地曲目（canonical 文件 + library 链接），返回清理数量。"""
        state_map = self._load_synced_state(self.cfg)
        synced_ids = set(state_map.keys())
        stale_ids = synced_ids - set(remote_tracks.keys())
        if not stale_ids:
            return 0

        removed_count = 0
        for track_id in stale_ids:
            # 删除 canonical 文件
            for ext in (".flac", ".mp3", ".lrc"):
                (self.cfg.downloads_dir / f"{track_id}{ext}").unlink(missing_ok=True)
            # 删除所有 library 链接（遍历歌单目录）
            for parent in (self.cfg.lossless_dir, self.cfg.lossy_dir):
                if not parent.is_dir():
                    continue
                for pl_dir in parent.iterdir():
                    if not pl_dir.is_dir():
                        continue
                    for ext in (".flac", ".mp3", ".lrc"):
                        link = pl_dir / f"{track_id}{ext}"
                        if link.exists():
                            link.unlink(missing_ok=True)
            state_map.pop(track_id, None)
            removed_count += 1

        if removed_count:
            self._save_synced_state(self.cfg, state_map)
            logger.info("清理远端已删除曲目：%s 首", removed_count)
        return removed_count

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
        # 写入下载索引（供 process 阶段匹配 raw file → track_id）
        processed_path = self.cfg.processed_state_file
        processed = load_json(processed_path, {})
        if not isinstance(processed, dict):
            processed = {}
        for item in downloaded:
            rel = workspace_rel_path(Path(item.source_file), self.cfg.workspace_path)
            processed[str(item.track.id)] = {"source": rel}
        save_json(processed_path, processed)
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
                    _save_partial_downloads(self.cfg, results)
                raise

        return results

    def _mark_synced(
        self, downloaded: list[DownloadedTrack], existing_ids: set[int], track_playlists: dict[int, list[int]]
    ) -> None:
        """将新下载的 track ID 合并到 existing_ids 并写回状态文件"""
        if not downloaded:
            return
        state_map = self._load_synced_state(self.cfg)
        for item in downloaded:
            tid = item.track.id
            existing_ids.add(tid)
            state_map[tid] = sorted(track_playlists.get(tid, []))
        self._save_synced_state(self.cfg, state_map)


def _save_partial_downloads(cfg: Config, results: list[DownloadedTrack]) -> None:
    """Save partially completed downloads to state files so the next run skips them."""
    processed_path = cfg.processed_state_file
    processed = load_json(processed_path, {})
    if not isinstance(processed, dict):
        processed = {}
    for item in results:
        rel = workspace_rel_path(Path(item.source_file), cfg.workspace_path)
        processed[str(item.track.id)] = {"source": rel}
    save_json(processed_path, processed)

    # synced_tracks.json — 使用新格式
    state_map = SyncService._load_synced_state(cfg)
    for item in results:
        tid = item.track.id
        if tid not in state_map:
            state_map[tid] = sorted(item.playlist_ids)
    SyncService._save_synced_state(cfg, state_map)

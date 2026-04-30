from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.providers.pyncm_client import LoginResult, PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.shared.output import info as output_info, warn as output_warn
from musicvault.shared.tui_progress import BatchProgress
from musicvault.shared.utils import load_json, save_json, workspace_rel_path

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, cfg: Config, api: PyncmClient, downloader: Downloader, workers: int) -> None:
        self.cfg = cfg
        self.api = api
        self.downloader = downloader
        self.workers = max(1, workers)

    def run_sync(self, cookie: str, playlist_ids: list[int]) -> list[DownloadedTrack]:
        self._cleanup_stale_state()
        user = self.api.login_with_cookie(cookie)
        target_ids = playlist_ids or self._resolve_liked_playlist(user)
        output_info(f"将同步 {len(target_ids)} 个歌单")

        # 收集歌单元数据 + 建立 track → playlist_ids 映射
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        track_playlists: dict[int, list[int]] = {}
        all_tracks: dict[int, Track] = {}

        for pid in target_ids:
            info = self.api.get_playlist_info(pid)
            playlist_index[str(pid)] = {"name": info["name"], "track_count": info["track_count"]}
            tracks = self.api.get_playlist_tracks(pid)
            for track in tracks:
                all_tracks[track.id] = track
                track_playlists.setdefault(track.id, []).append(pid)

        save_json(self.cfg.state_dir / "playlists.json", playlist_index)
        self.playlist_index = playlist_index

        unique = list(all_tracks.values())
        output_info(f"歌单曲目合计：{len(unique)} 首（去重后）")

        new_tracks, synced_ids = self._diff_tracks(unique)
        downloaded = self._sync_tracks(new_tracks, track_playlists)
        self._mark_synced(downloaded, synced_ids)
        return downloaded

    def _cleanup_stale_state(self) -> None:
        """清理源文件已不存在的过期索引条目，避免阻止重新下载"""
        index_path = self.cfg.state_dir / "file_track_index.json"
        file_index = load_json(index_path, {})
        if not isinstance(file_index, dict) or not file_index:
            return

        stale_ids: set[int] = set()
        for rel_path, track_id_raw in list(file_index.items()):
            source_file = self.cfg.workspace_path / str(rel_path)
            if not source_file.exists():
                try:
                    stale_ids.add(int(track_id_raw))
                except (TypeError, ValueError):
                    pass
                del file_index[rel_path]

        if stale_ids:
            save_json(index_path, file_index)
            state = load_json(self.cfg.synced_state_file, {"ids": []})
            if isinstance(state, dict):
                existing = {int(x) for x in state.get("ids", []) if isinstance(x, (int, str))}
                cleaned = existing - stale_ids
                if cleaned != existing:
                    save_json(self.cfg.synced_state_file, {"ids": sorted(cleaned)})
                    logger.info("清理过期状态：%s 个文件已不存在，已从索引中移除", len(stale_ids))

    def _resolve_liked_playlist(self, user: LoginResult) -> list[int]:
        playlists = self.api.list_user_playlists(user.user_id)
        for item in playlists:
            if item.get("specialType") == 5:
                return [int(item["id"])]
        if playlists:
            return [int(playlists[0]["id"])]
        raise RuntimeError("当前账号无可用歌单")

    def _diff_tracks(self, tracks: list[Track]) -> tuple[list[Track], set[int]]:
        """返回 (新增曲目, 已同步的 track_id 集合)，调用方可将集合传给 _mark_synced 避免重复加载。"""
        state = load_json(self.cfg.synced_state_file, {"ids": []})
        synced_ids = {int(x) for x in state.get("ids", [])}
        new_tracks = [track for track in tracks if track.id not in synced_ids]
        return new_tracks, synced_ids

    def _sync_tracks(self, tracks: list[Track], track_playlists: dict[int, list[int]]) -> list[DownloadedTrack]:
        if not tracks:
            output_info("同步阶段无新增曲目，跳过下载")
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
        index_path = self.cfg.state_dir / "file_track_index.json"
        file_index = load_json(index_path, {})
        for item in downloaded:
            rel = workspace_rel_path(Path(item.source_file), self.cfg.workspace_path)
            file_index[rel] = item.track.id
        save_json(index_path, file_index)
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
                pool.submit(self.downloader.download_track, track, url, self.cfg.downloads_dir): (idx, track)
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
                    output_warn(f"Ctrl+C 中断，保存已完成的 {len(results)} 项下载...")
                    _save_partial_downloads(self.cfg, results)
                raise

        return results

    def _mark_synced(self, downloaded: list[DownloadedTrack], existing_ids: set[int]) -> None:
        """将新下载的 track ID 合并到 existing_ids 并写回状态文件"""
        if not downloaded:
            return
        for item in downloaded:
            existing_ids.add(item.track.id)
        save_json(self.cfg.synced_state_file, {"ids": sorted(existing_ids)})


def _save_partial_downloads(cfg: Config, results: list[DownloadedTrack]) -> None:
    """Save partially completed downloads to state files so the next run skips them."""
    # file_track_index.json
    index_path = cfg.state_dir / "file_track_index.json"
    file_index = load_json(index_path, {})
    for item in results:
        rel = workspace_rel_path(Path(item.source_file), cfg.workspace_path)
        file_index[rel] = item.track.id
    save_json(index_path, file_index)

    # synced_tracks.json
    state = load_json(cfg.synced_state_file, {"ids": []})
    existing = {int(x) for x in state.get("ids", []) if isinstance(x, (int, str))}
    for item in results:
        existing.add(item.track.id)
    save_json(cfg.synced_state_file, {"ids": sorted(existing)})

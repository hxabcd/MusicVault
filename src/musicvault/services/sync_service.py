from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.providers.pyncm_client import LoginResult, PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.shared.tui_progress import BatchProgress
from musicvault.shared.utils import load_json, save_json

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, cfg: Config, api: PyncmClient, downloader: Downloader, workers: int) -> None:
        self.cfg = cfg
        self.api = api
        self.downloader = downloader
        self.workers = max(1, workers)

    def run_sync(self, cookie: str, playlist_id: int | None = None) -> list[DownloadedTrack]:
        user = self.api.login_with_cookie(cookie)
        selected_playlist = playlist_id or self._resolve_favorites(user)
        tracks = self.api.get_playlist_tracks(selected_playlist)
        new_tracks = self._diff_tracks(tracks)
        downloaded = self._sync_tracks(new_tracks)
        self._mark_synced(downloaded)
        return downloaded

    def _resolve_favorites(self, user: LoginResult) -> int:
        playlists = self.api.list_user_playlists(user.user_id)
        for item in playlists:
            if item.get("specialType") == 5:
                return int(item["id"])
        if playlists:
            return int(playlists[0]["id"])
        raise RuntimeError("当前账号无可用歌单")

    def _diff_tracks(self, tracks: list[Track]) -> list[Track]:
        state = load_json(self.cfg.synced_state_file, {"ids": []})
        synced_ids = {int(x) for x in state.get("ids", [])}
        return [track for track in tracks if track.id not in synced_ids]

    def _sync_tracks(self, tracks: list[Track]) -> list[DownloadedTrack]:
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
                logger.warning("跳过下载：无可用直链 track_id=%s name=%s", track.id, track.name)
                continue
            pending.append((track, url))
        logger.info("下载准备完成：可下载=%s 跳过=%s", len(pending), skipped)

        downloaded = self._run_download_batch(pending)
        index_path = self.cfg.state_dir / "file_track_index.json"
        file_index = load_json(index_path, {})
        for item in downloaded:
            rel = Path(item.source_file).resolve().relative_to(self.cfg.workspace_path)
            file_index[str(rel)] = item.track.id
        save_json(index_path, file_index)
        return downloaded

    def _run_download_batch(self, tasks: list[tuple[Track, str]]) -> list[DownloadedTrack]:
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
            for future in as_completed(future_map):
                idx, track = future_map[future]
                try:
                    results.append(future.result())
                    bp.advance(success=True, idx=idx, item_name=track.name)
                except Exception as exc:
                    bp.advance(success=False, idx=idx, item_name=track.name)
                    logger.error("下载失败：#%s %s，原因：%s", idx, track.name, exc, exc_info=True)

        return results

    def _mark_synced(self, downloaded: list[DownloadedTrack]) -> None:
        if not downloaded:
            return
        state = load_json(self.cfg.synced_state_file, {"ids": []})
        existing = {int(x) for x in state.get("ids", [])}
        for item in downloaded:
            existing.add(item.track.id)
        save_json(self.cfg.synced_state_file, {"ids": sorted(existing)})

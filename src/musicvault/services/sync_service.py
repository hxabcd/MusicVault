from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.core.preset import Preset, audio_spec_key
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

        processed_files.json 格式：key 为 track_id 字符串，value 含 audios 字典或旧版 flac/mp3/source 等字段。
        检查对应文件是否存在，不存在则从索引中移除。
        """
        processed = load_json(self.cfg.processed_state_file, {})
        if not isinstance(processed, dict) or not processed:
            return

        stale_ids: set[int] = set()
        for key, value in list(processed.items()):
            if not isinstance(value, dict):
                continue

            has_any = False
            # 新版格式：{"audios": {"FLAC": "relative/path", ...}}
            audios = value.get("audios")
            if isinstance(audios, dict):
                for _spec_key, rel in audios.items():
                    if isinstance(rel, str) and (self.cfg.workspace_path / rel).exists():
                        has_any = True
                        break

            # 旧版格式兼容（flac / mp3 / lossless / source / lrc）
            if not has_any:
                for field in ("flac", "mp3", "lossless", "source", "lrc"):
                    rel = value.get(field)
                    if isinstance(rel, str) and (self.cfg.workspace_path / rel).exists():
                        has_any = True
                        break

            if has_any:
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
        for preset in self.cfg.presets:
            old_dir = self.cfg.preset_dir(preset.name) / old_safe
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

            # 从 download 目录中的 canonical 文件构建 audio_map
            audio_map: dict[str, Path] = {}
            for preset in self.cfg.presets:
                spec_key = audio_spec_key(preset.format, preset.bitrate)
                if spec_key not in audio_map:
                    src = self._find_canonical_for_spec(track_id, spec_key)
                    if src:
                        audio_map[spec_key] = src

            if not audio_map:
                continue

            # 删除已移除歌单的链接
            for name in old_names - new_names:
                self._remove_track_links(track, name)

            # 创建新增歌单的链接
            for name in new_names - old_names:
                self._create_track_links(audio_map, track, name)

        # 写回更新后的歌单分配
        new_map = dict(old_map)
        for track_id, new_pids in track_playlists.items():
            if track_id in old_map:
                new_map[track_id] = sorted(new_pids)
        self._save_synced_state(self.cfg, new_map)

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

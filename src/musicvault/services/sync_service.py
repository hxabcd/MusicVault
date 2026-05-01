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
from musicvault.shared.utils import hardlink_or_copy, load_json, safe_filename, save_json, workspace_rel_path

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
        if not playlist_ids:
            output_warn("未配置任何歌单，请先执行 msv add 添加歌单")
            return []

        self.api.login_with_cookie(cookie)
        logger.info("将同步 %s 个歌单", len(playlist_ids))

        # 收集歌单元数据 + 建立 track → playlist_ids 映射
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        track_playlists: dict[int, list[int]] = {}
        all_tracks: dict[int, Track] = {}

        for pid in playlist_ids:
            info = self.api.get_playlist_info(pid)
            old_entry = playlist_index.get(str(pid))
            old_name = old_entry.get("name") if old_entry else None
            new_name = info["name"]
            if old_name and old_name != new_name:
                self._handle_playlist_rename(pid, old_name, new_name)
            playlist_index[str(pid)] = {"name": info["name"], "track_count": info["track_count"]}
            tracks = self.api.get_playlist_tracks(pid)
            for track in tracks:
                all_tracks[track.id] = track
                track_playlists.setdefault(track.id, []).append(pid)

        save_json(self.cfg.state_dir / "playlists.json", playlist_index)
        self.playlist_index = playlist_index

        unique = list(all_tracks.values())
        logger.info("歌单曲目合计：%s 首（去重后）", len(unique))

        # 协调已有曲目的歌单分配变化（移动/链接文件）
        self._reconcile_playlist_assignments(track_playlists, playlist_index)

        pruned = self._prune_stale_tracks(all_tracks)
        new_tracks, synced_ids = self._diff_tracks(unique)
        downloaded = self._sync_tracks(new_tracks, track_playlists)
        self._mark_synced(downloaded, synced_ids, track_playlists)

        # 单行摘要
        added = len(downloaded)
        console.print(f"  从 [cyan]{len(playlist_ids)}[/cyan] 个歌单同步 [cyan]{len(unique)}[/cyan] 首")
        stats: list[str] = []
        if added:
            stats.append(f"[green]+{added} 首[/green]")
        if pruned:
            stats.append(f"[red]-{pruned} 首[/red]")
        console.print("    " + " | ".join(stats) if stats else "    [dim]无变化[/dim]")

        return downloaded

    def _cleanup_stale_state(self) -> None:
        """清理源文件已不存在的过期索引条目，避免阻止重新下载"""
        processed = load_json(self.cfg.processed_state_file, {})
        if not isinstance(processed, dict) or not processed:
            return

        stale_ids: set[int] = set()
        for rel_path, value in list(processed.items()):
            source_file = self.cfg.workspace_path / str(rel_path)
            if not source_file.exists():
                if isinstance(value, dict):
                    try:
                        stale_ids.add(int(value.get("track_id", 0)))
                    except (TypeError, ValueError):
                        pass
                del processed[rel_path]

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

    def _handle_playlist_rename(self, pid: int, old_name: str, new_name: str) -> None:
        old_safe = safe_filename(old_name)
        new_safe = safe_filename(new_name)
        if old_safe == new_safe:
            return

        # 迁移 library 目录
        for parent in (self.cfg.lossless_dir, self.cfg.lossy_dir):
            old_dir = parent / old_safe
            new_dir = parent / new_safe
            if not old_dir.is_dir():
                continue
            if new_dir.exists():
                for f in old_dir.iterdir():
                    target = new_dir / f.name
                    if not target.exists():
                        shutil.move(str(f), str(target))
                shutil.rmtree(old_dir)
            else:
                old_dir.rename(new_dir)

        # 更新 processed_files.json 中的路径
        processed = load_json(self.cfg.processed_state_file, {})
        if not isinstance(processed, dict):
            return

        old_prefix_ll = f"library/lossless/{old_safe}/"
        old_prefix_ly = f"library/lossy/{old_safe}/"
        new_prefix_ll = f"library/lossless/{new_safe}/"
        new_prefix_ly = f"library/lossy/{new_safe}/"

        modified = False
        for _key, value in processed.items():
            if not isinstance(value, dict):
                continue
            for file_key, old_prefix, new_prefix in [
                ("lossless", old_prefix_ll, new_prefix_ll),
                ("lossy", old_prefix_ly, new_prefix_ly),
            ]:
                file_rel = str(value.get(file_key, ""))
                if file_rel.startswith(old_prefix):
                    value[file_key] = new_prefix + file_rel[len(old_prefix) :]
                    modified = True
            for link in value.get("links", []) or []:
                if not isinstance(link, dict):
                    continue
                for file_key, old_prefix, new_prefix in [
                    ("lossless", old_prefix_ll, new_prefix_ll),
                    ("lossy", old_prefix_ly, new_prefix_ly),
                ]:
                    file_rel = str(link.get(file_key, ""))
                    if file_rel.startswith(old_prefix):
                        link[file_key] = new_prefix + file_rel[len(old_prefix) :]
                        modified = True

        if modified:
            save_json(self.cfg.processed_state_file, processed)

        logger.info("歌单 '%s' 已重命名为 '%s'，已迁移本地目录", old_name, new_name)

    # ------------------------------------------------------------------
    # 歌单分配协调
    # ------------------------------------------------------------------

    def _reconcile_playlist_assignments(
        self, track_playlists: dict[int, list[int]], playlist_index: dict[str, dict[str, object]]
    ) -> None:
        """对比 API 返回的歌单分配与本地存储，移动/创建/删除文件以保持一致。"""
        old_map = self._load_synced_state(self.cfg)
        if not old_map:
            return

        processed = load_json(self.cfg.processed_state_file, {})
        if not isinstance(processed, dict):
            processed = {}

        # 构建 track_id → (source_key, entry) 反向索引
        track_index: dict[int, tuple[str, dict]] = {}
        for key, value in processed.items():
            if not isinstance(value, dict):
                continue
            tid_raw = value.get("track_id")
            if tid_raw is None:
                continue
            try:
                track_index[int(tid_raw)] = (key, value)
            except (TypeError, ValueError):
                continue

        changes = False
        for track_id, old_pids in old_map.items():
            new_pids = track_playlists.get(track_id, [])
            if not new_pids:
                continue  # 已完全移除，由 _prune_stale_tracks 处理
            if old_pids == new_pids:
                continue

            result = track_index.get(track_id)
            if result is None:
                continue  # processed_files.json 无对应条目，仅更新状态
            source_key, entry = result

            if self._update_track_location(
                track_id, old_pids, new_pids, playlist_index, entry
            ):
                changes = True

        if changes:
            save_json(self.cfg.processed_state_file, processed)

        # 写回更新后的歌单分配
        new_map = dict(old_map)
        for track_id, new_pids in track_playlists.items():
            if track_id in old_map:
                new_map[track_id] = sorted(new_pids)
        self._save_synced_state(self.cfg, new_map)

    def _update_track_location(
        self,
        track_id: int,
        old_pids: list[int],
        new_pids: list[int],
        playlist_index: dict[str, dict[str, object]],
        entry: dict,
    ) -> bool:
        """对单个 track 执行文件系统变更（移动/链接），更新 entry 中的路径。返回是否有变更。"""
        ws = self.cfg.workspace_path
        old_names = [self._playlist_id_to_dirname(pid, playlist_index) for pid in old_pids]
        new_names = [self._playlist_id_to_dirname(pid, playlist_index) for pid in new_pids]

        old_set = set(old_pids)
        new_set = set(new_pids)

        primary_changed = (old_pids[:1] != new_pids[:1])
        changed = False

        # --- 主歌单变化：移动主文件 ---
        if primary_changed and old_names and new_names:
            old_primary = old_names[0]
            new_primary = new_names[0]
            if old_primary != new_primary:
                ll_rel = str(entry.get("lossless", ""))
                ly_rel = str(entry.get("lossy", ""))
                if ll_rel and ly_rel:
                    new_ll_rel = self._replace_dir_in_path(ll_rel, old_primary, new_primary)
                    new_ly_rel = self._replace_dir_in_path(ly_rel, old_primary, new_primary)
                    old_ll = ws / ll_rel
                    old_ly = ws / ly_rel
                    new_ll = ws / new_ll_rel
                    new_ly = ws / new_ly_rel

                    new_ll.parent.mkdir(parents=True, exist_ok=True)
                    new_ly.parent.mkdir(parents=True, exist_ok=True)

                    if old_ll.exists():
                        shutil.move(str(old_ll), str(new_ll))
                    if old_ly.exists():
                        shutil.move(str(old_ly), str(new_ly))

                    # 仅当目标文件确实存在时才更新 entry
                    if not new_ll.exists() and not new_ly.exists():
                        logger.warning(
                            "主歌单变化但源文件不存在，跳过：track_id=%s %s → %s",
                            track_id, old_primary, new_primary,
                        )
                    else:
                        entry["lossless"] = new_ll_rel
                        entry["lossy"] = new_ly_rel
                        changed = True

                    # 如果旧主歌单仍在新的分配中 → 创建硬链接回去
                    if old_pids[0] in new_set and new_ll.exists():
                        old_ll.parent.mkdir(parents=True, exist_ok=True)
                        old_ly.parent.mkdir(parents=True, exist_ok=True)
                        hardlink_or_copy(new_ll, old_ll)
                        hardlink_or_copy(new_ly, old_ly)
                        links = entry.setdefault("links", [])
                        if not isinstance(links, list):
                            links = []
                            entry["links"] = links
                        links.append({
                            "lossless": workspace_rel_path(old_ll, ws),
                            "lossy": workspace_rel_path(old_ly, ws),
                        })

        # --- 移除多余链接 ---
        removed_pids = old_set - new_set
        if removed_pids:
            links = entry.get("links", [])
            if isinstance(links, list):
                new_links = []
                for link in links:
                    if not isinstance(link, dict):
                        new_links.append(link)
                        continue
                    link_ll = str(link.get("lossless", ""))
                    link_ly = str(link.get("lossy", ""))
                    should_keep = True
                    for rpid in removed_pids:
                        rname = self._playlist_id_to_dirname(rpid, playlist_index)
                        prefix_ll = f"library/lossless/{rname}/"
                        prefix_ly = f"library/lossy/{rname}/"
                        if link_ll.startswith(prefix_ll) or link_ly.startswith(prefix_ly):
                            (ws / link_ll).unlink(missing_ok=True)
                            (ws / link_ly).unlink(missing_ok=True)
                            should_keep = False
                            changed = True
                            break
                    if should_keep:
                        new_links.append(link)
                if len(new_links) != len(links):
                    entry["links"] = new_links
            # 如果被移除的恰好是旧主歌单（且主歌单未变化）—— 清理旧主目录
            if not primary_changed:
                for rpid in removed_pids:
                    rname = self._playlist_id_to_dirname(rpid, playlist_index)
                    if not old_names or rname == old_names[0]:
                        ll_rel = str(entry.get("lossless", ""))
                        ly_rel = str(entry.get("lossy", ""))
                        prefix_ll = f"library/lossless/{rname}/"
                        prefix_ly = f"library/lossy/{rname}/"
                        if ll_rel.startswith(prefix_ll):
                            (ws / ll_rel).unlink(missing_ok=True)
                        if ly_rel.startswith(prefix_ly):
                            (ws / ly_rel).unlink(missing_ok=True)

        # --- 添加缺失链接 ---
        added_pids = new_set - old_set
        if added_pids:
            ll_rel = str(entry.get("lossless", ""))
            ly_rel = str(entry.get("lossy", ""))
            if ll_rel and ly_rel:
                ll_src = ws / ll_rel
                ly_src = ws / ly_rel
                if not ll_src.exists() or not ly_src.exists():
                    logger.warning(
                        "添加链接但源文件不存在，跳过：track_id=%s src=%s",
                        track_id, ll_rel,
                    )
                    return changed
                links = entry.get("links", [])
                if not isinstance(links, list):
                    links = []
                    entry["links"] = links

                for apid in added_pids:
                    aname = self._playlist_id_to_dirname(apid, playlist_index)
                    # 跳过主歌单（主文件已由 organizer 生成或上方已移动）
                    if new_names and aname == new_names[0]:
                        continue

                    ll_dst_dir = self.cfg.lossless_dir / aname
                    ly_dst_dir = self.cfg.lossy_dir / aname
                    ll_dst_dir.mkdir(parents=True, exist_ok=True)
                    ly_dst_dir.mkdir(parents=True, exist_ok=True)
                    ll_dst = ll_dst_dir / ll_src.name
                    ly_dst = ly_dst_dir / ly_src.name

                    # 幂等性检查
                    already_linked = any(
                        isinstance(lnk, dict)
                        and str(lnk.get("lossless", "")).startswith(f"library/lossless/{aname}/")
                        for lnk in links
                    )
                    if not already_linked:
                        hardlink_or_copy(ll_src, ll_dst)
                        hardlink_or_copy(ly_src, ly_dst)
                        links.append({
                            "lossless": workspace_rel_path(ll_dst, ws),
                            "lossy": workspace_rel_path(ly_dst, ws),
                        })
                        changed = True

        return changed

    def _playlist_id_to_dirname(
        self, pid: int, playlist_index: dict[str, dict[str, object]]
    ) -> str:
        """将 playlist_id 映射为安全的目录名。"""
        entry = playlist_index.get(str(pid))
        name = str(entry["name"]) if entry and entry.get("name") else str(pid)
        return safe_filename(name)

    @staticmethod
    def _replace_dir_in_path(rel_path: str, old_dir: str, new_dir: str) -> str:
        """替换相对路径中的第一级子目录名（如 library/lossless/OldName/file → library/lossless/NewName/file）。"""
        prefix = "library/"
        rest = rel_path[len(prefix):] if rel_path.startswith(prefix) else rel_path
        parts = rest.split("/", 2)
        if len(parts) >= 2 and parts[1] == old_dir:
            parts[1] = new_dir
            return prefix + "/".join(parts)
        return rel_path

    def _diff_tracks(self, tracks: list[Track]) -> tuple[list[Track], set[int]]:
        """返回 (新增曲目, 已同步的 track_id 集合)，调用方可将集合传给 _mark_synced 避免重复加载。"""
        state_map = self._load_synced_state(self.cfg)
        synced_ids = set(state_map.keys())
        new_tracks = [track for track in tracks if track.id not in synced_ids]
        return new_tracks, synced_ids

    def _prune_stale_tracks(self, remote_tracks: dict[int, Track]) -> int:
        """删除远端已不存在的本地曲目（以远端为准），返回清理数量。"""
        state_map = self._load_synced_state(self.cfg)
        synced_ids = set(state_map.keys())
        stale_ids = synced_ids - set(remote_tracks.keys())
        if not stale_ids:
            return 0

        processed = load_json(self.cfg.processed_state_file, {})
        if not isinstance(processed, dict):
            processed = {}
        removed_count = 0

        for stale_id in stale_ids:
            for rel_path, value in list(processed.items()):
                if not isinstance(value, dict):
                    continue
                if int(value.get("track_id", 0)) != stale_id:
                    continue

                for file_key in ("lossless", "lossy"):
                    file_rel = str(value.get(file_key, ""))
                    if file_rel:
                        file_abs = self.cfg.workspace_path / file_rel
                        try:
                            file_abs.unlink(missing_ok=True)
                        except OSError:
                            pass
                # 清理源下载文件
                source_abs = self.cfg.workspace_path / rel_path
                try:
                    source_abs.unlink(missing_ok=True)
                except OSError:
                    pass
                # 清理 links 中的关联文件（重复曲目）
                for link in value.get("links", []) or []:
                    if isinstance(link, dict):
                        for file_key in ("lossless", "lossy"):
                            file_rel = str(link.get(file_key, ""))
                            if file_rel:
                                file_abs = self.cfg.workspace_path / file_rel
                                try:
                                    file_abs.unlink(missing_ok=True)
                                except OSError:
                                    pass

                del processed[rel_path]
                removed_count += 1

        if removed_count:
            # 写入清理后的 processed_files
            save_json(self.cfg.processed_state_file, processed)
            # 写入清理后的 synced_tracks
            for sid in stale_ids:
                state_map.pop(sid, None)
            self._save_synced_state(self.cfg, state_map)
            logger.info("清理远端已删除曲目：%s 首（%s 个 track_id）", removed_count, len(stale_ids))
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
        processed_path = self.cfg.processed_state_file
        processed = load_json(processed_path, {})
        if not isinstance(processed, dict):
            processed = {}
        for item in downloaded:
            rel = workspace_rel_path(Path(item.source_file), self.cfg.workspace_path)
            processed[rel] = {"track_id": item.track.id}
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

    def _mark_synced(self, downloaded: list[DownloadedTrack], existing_ids: set[int], track_playlists: dict[int, list[int]]) -> None:
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
        processed[rel] = {"track_id": item.track.id}
    save_json(processed_path, processed)

    # synced_tracks.json — 使用新格式
    state_map = SyncService._load_synced_state(cfg)
    for item in results:
        tid = item.track.id
        if tid not in state_map:
            state_map[tid] = sorted(item.playlist_ids)
    SyncService._save_synced_state(cfg, state_map)

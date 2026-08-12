from __future__ import annotations

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.processors.lyrics import convert_lyrics_payload
from musicvault.application.progress import ProgressReporter
from musicvault.application.source_state import SourceStateRecorder
from musicvault.core.config import Config
from musicvault.domain.lyrics import lyrics_to_json
from musicvault.domain.models import DownloadedTrack, Playlist, Track
from musicvault.ports.source import SourceClient
from musicvault.ports.state import StateRepository
from musicvault.shared.output import warn as output_warn
from musicvault.shared.utils import workspace_rel_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """sync 运行的结构化结果：CLI 据此渲染，process 阶段消费 downloaded。"""

    downloaded: tuple[DownloadedTrack, ...] = ()
    added: int = 0
    no_url: int = 0
    pruned: int = 0
    track_count: int = 0
    playlist_count: int = 0
    dry_run_plan: dict | None = None


@dataclass(frozen=True, slots=True)
class _RemoteState:
    """fetch/pull 共享的远端拉取结果（纯 API 读取，不落库）。"""

    all_tracks: dict[int, Track]
    playlist_track_order: dict[int, list[int]]
    playlist_index: dict[str, dict[str, object]]
    pending_renames: list[tuple[int, str, str]]
    song_ids: list[int]


class SyncUseCase:
    """同步应用用例：fetch 拉取元数据、pull 下载曲目与歌词入库、源侧状态登记"""

    def __init__(
        self,
        cfg: Config,
        api: SourceClient,
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
        # workspace 各生命周期区域路径的唯一来源（cache/media_store/library/logs）
        self.paths = WorkspacePaths(cfg.workspace_path)
        # 把本次 sync 的源侧状态写入 SQLite，供 target-sync 消费
        self.recorder = SourceStateRecorder(state)
        # 歌单索引：run_fetch 正常路径末尾填充；无歌单早退时保持空（run_pipeline 直接消费）
        self.playlist_index: dict[str, dict[str, object]] = {}

    def load_synced_state(self) -> dict[int, list[int]]:
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

    def run_fetch(self, cookie: str, playlist_ids: list[int]) -> None:
        """fetch 阶段：拉取歌单元数据并登记 SQLite（不下载、不碰 library）。

        改名/删除/分配变化只更新 SQLite 关系（rename 走 upsert_playlist），
        library 目录迁移由 distribute 幂等重建覆盖。纯元数据写 SQLite，
        无 dry-run 分支（dry-run 下本阶段由 PipelineUseCase 跳过）。
        """
        song_ids = self.recorder.state.list_managed_songs()
        if not playlist_ids and not song_ids:
            output_warn("未配置任何歌单或单曲，请先执行 msv add 添加歌单或 msv add --song <ID> 添加单曲")
            return

        self.api.login_with_cookie(cookie)
        remote = self._fetch_remote(playlist_ids)
        for pid, _old_name, new_name in remote.pending_renames:
            self.recorder.state.upsert_playlist(Playlist(pid, new_name, ()))
            logger.info("歌单 %s 已改名为 '%s'（仅登记 SQLite，library 由 distribute 幂等重建）", pid, new_name)

        self.playlist_index = remote.playlist_index
        self._record_source_state(
            remote.all_tracks, remote.playlist_track_order, remote.playlist_index, remote.song_ids
        )

    def run_pull(
        self,
        cookie: str,
        playlist_ids: list[int],
        *,
        progress: ProgressReporter | None = None,
    ) -> SyncResult:
        """pull 阶段：对比并下载新增曲目、歌词统一格式入库、清理远端已删曲目。"""
        stale_index = self._cleanup_stale_state()
        song_ids = self.recorder.state.list_managed_songs()
        if not playlist_ids and not song_ids:
            return SyncResult()

        self.api.login_with_cookie(cookie)
        remote = self._fetch_remote(playlist_ids)
        self.playlist_index = remote.playlist_index
        unique = list(remote.all_tracks.values())
        logger.info("歌单曲目合计：%s 首（去重后）", len(unique))

        new_tracks, _ = self._diff_tracks(unique)

        if self.dry_run:
            with_url, no_url = self._resolve_dry_urls(new_tracks)
            pruned_ids = self._prune_stale_tracks(remote.all_tracks)[1]
            plan = {
                "with_url": with_url,
                "no_url": no_url,
                "pruned": pruned_ids,
                "stale_index": stale_index,
                "track_count": len(unique),
                "playlist_count": len(playlist_ids) + (1 if song_ids else 0),
            }
            return SyncResult(
                downloaded=(),
                dry_run_plan=plan,
                track_count=len(unique),
                playlist_count=len(playlist_ids) + (1 if song_ids else 0),
            )

        downloaded, no_url = self._sync_tracks(new_tracks, progress)
        for item in downloaded:
            self._save_lyrics(item.track.id)
        pruned_count, _ = self._prune_stale_tracks(remote.all_tracks)
        self._record_source_state(
            remote.all_tracks, remote.playlist_track_order, remote.playlist_index, remote.song_ids
        )

        n_playlists = len(playlist_ids) + (1 if song_ids else 0)
        return SyncResult(
            downloaded=tuple(downloaded),
            added=len(downloaded),
            no_url=no_url,
            pruned=pruned_count,
            track_count=len(unique),
            playlist_count=n_playlists,
        )

    def _fetch_remote(self, playlist_ids: list[int]) -> _RemoteState:
        """拉取远端歌单曲目与单独管理单曲详情（fetch/pull 共用，纯 API 读取）。"""
        song_ids = self.recorder.state.list_managed_songs()
        playlist_index = {
            str(pl.id): {"name": pl.name, "track_count": len(pl.track_ids)}
            for pl in self.recorder.state.list_playlists()
        }
        all_tracks: dict[int, Track] = {}
        playlist_track_order: dict[int, list[int]] = {}
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
                playlist_track_order[pid] = [track.id for track in tracks]
                for track in tracks:
                    all_tracks[track.id] = track

        if song_ids:
            logger.info("将同步 %s 首单独管理的单曲", len(song_ids))
            song_details = self.api.get_tracks_detail(song_ids)
            for _sid, track in song_details.items():
                if track.id not in all_tracks:
                    all_tracks[track.id] = track
            # 过滤本地已删除但仍在 managed_songs 中的旧 ID
            missing = sorted(set(song_ids) - set(song_details.keys()))
            if missing:
                for mid in missing:
                    self.recorder.state.remove_managed_song(mid)
                logger.info("清理无效单曲 ID：%s", missing)

        return _RemoteState(
            all_tracks=all_tracks,
            playlist_track_order=playlist_track_order,
            playlist_index=playlist_index,
            pending_renames=pending_renames,
            song_ids=song_ids,
        )

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

    def find_canonical_for_spec(self, track_id: int, spec_key: str) -> Path | None:
        """查找符合指定 spec_key 的 canonical 文件（media_store/<track_id>/ 扁平布局）。"""
        audio_dir = self.paths.media_store / str(track_id)
        if not audio_dir.is_dir():
            return None
        if spec_key == "ORIGINAL":
            for ext in (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"):
                p = audio_dir / f"{track_id}{ext}"
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
            p = audio_dir / name
            if p.exists():
                return p
        return None

    def _diff_tracks(self, tracks: list[Track]) -> tuple[list[Track], set[int]]:
        """返回 (新增曲目, 已下载的 track_id 集合)。

        fetch 阶段只登记元数据、不下载，因此「已同步」以实际下载产物为准：
        存在媒体资产或待处理 raw 文件（pending_files）的曲目视为已下载。
        """
        snapshot = self.recorder.state.create_snapshot()
        downloaded_ids = {asset.track_id for asset in snapshot.media_assets if asset.asset_type == "audio"}
        downloaded_ids.update(self.recorder.state.list_pending_track_ids())
        new_tracks = [track for track in tracks if track.id not in downloaded_ids]
        return new_tracks, downloaded_ids

    def _resolve_dry_urls(self, tracks: list[Track]) -> tuple[list[Track], list[Track]]:
        """dry-run：批量查询直链，返回 (可下载, 无直链) 两组曲目。"""
        if not tracks:
            return [], []
        url_map = self.api.get_tracks_download_urls([track.id for track in tracks])
        with_url = [t for t in tracks if url_map.get(t.id)]
        no_url = [t for t in tracks if not url_map.get(t.id)]
        return with_url, no_url

    def _prune_stale_tracks(self, remote_tracks: dict[int, Track]) -> tuple[int, list[int]]:
        """删除远端已不存在的本地曲目（canonical 文件 + SQLite 状态）。

        返回 (清理数量, stale_ids)；dry-run 模式下只计算并上报，不删除文件、不写状态。
        library 链接由 distribute 幂等重建覆盖，这里不再遍历 library。
        """
        state_map = self.load_synced_state()
        synced_ids = set(state_map.keys())
        stale_ids = sorted(synced_ids - set(remote_tracks.keys()))
        if not stale_ids:
            return 0, []

        if self.dry_run:
            logger.info("dry-run：将清理远端已删除曲目 %s 首", len(stale_ids))
            return len(stale_ids), stale_ids

        removed_count = 0
        for track_id in stale_ids:
            # 收集 canonical 文件 inode（保留供扩展；library 链接清理已移交 distribute）
            canonical_inodes: set[tuple[int, int]] = set()
            track_dir = self.paths.media_store / str(track_id)
            if track_dir.is_dir():
                for f in list(track_dir.iterdir()):
                    if not f.is_file():
                        continue
                    try:
                        st = f.stat()
                        canonical_inodes.add((st.st_dev, st.st_ino))
                    except OSError:
                        continue
                # 扁平布局：删除 media_store/<tid>/ 整个目录（各格式、bitrate 变体、.lrc）
                shutil.rmtree(track_dir)

            state_map.pop(track_id, None)
            removed_count += 1

        if removed_count:
            for track_id in stale_ids:
                self.recorder.state.remove_track(track_id)
            logger.info("清理远端已删除曲目：%s 首", removed_count)
        return removed_count, stale_ids

    def _sync_tracks(
        self,
        tracks: list[Track],
        progress: ProgressReporter | None = None,
    ) -> tuple[list[DownloadedTrack], int]:
        """下载新增曲目，返回 (下载结果, 无直链跳过的数量)。"""
        if not tracks:
            logger.info("同步阶段无新增曲目，跳过下载")
            return [], 0

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

        downloaded = self._run_download_batch(pending, progress)
        # 写入 raw→track 映射（供 process 阶段从文件名反查 track_id）
        for item in downloaded:
            self.recorder.state.upsert_track(item.track)
            rel = workspace_rel_path(Path(item.source_file), self.cfg.workspace_path)
            self.recorder.state.add_pending_file(rel, item.track.id)
        return downloaded, skipped

    def _run_download_batch(
        self,
        tasks: list[tuple[Track, str]],
        progress: ProgressReporter | None = None,
    ) -> list[DownloadedTrack]:
        if not tasks:
            logger.info("下载队列为空，无需执行")
            return []

        total = len(tasks)
        workers = min(self.workers, total)
        results: list[DownloadedTrack] = []

        if progress is not None:
            progress.begin(total=total, phase="下载中")
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self.downloader.download_track, track, url, self.paths.cache): (idx, track)
                    for idx, (track, url) in enumerate(tasks, start=1)
                }
                try:
                    for future in as_completed(future_map):
                        idx, track = future_map[future]
                        try:
                            item = future.result()
                            results.append(item)
                            if progress is not None:
                                progress.advance(success=True, idx=idx, item_name=track.name)
                        except Exception as exc:
                            if progress is not None:
                                progress.advance(success=False, idx=idx, item_name=track.name)
                            logger.error("下载失败：#%s %s，原因：%s", idx, track.name, exc, exc_info=True)
                except KeyboardInterrupt:
                    pool.shutdown(wait=False, cancel_futures=True)
                    if results:
                        self._save_partial_downloads(results)
                    raise
        finally:
            if progress is not None:
                progress.end()

        return results

    def _save_partial_downloads(self, results: list[DownloadedTrack]) -> None:
        """中断时把已下载曲目登记到 SQLite，供下次 sync 跳过。"""
        # 曲目本体先入 SQLite；歌单关系由下次 sync 的 _record_source_state 重建
        for item in results:
            self.recorder.state.upsert_track(item.track)
            rel = workspace_rel_path(Path(item.source_file), self.cfg.workspace_path)
            self.recorder.state.add_pending_file(rel, item.track.id)

    def _save_lyrics(self, track_id: int) -> None:
        """拉取曲目歌词并转统一格式入库；失败降级为空行，不阻塞下载。"""
        try:
            payload = self.api.get_track_lyrics(track_id)
            lines = convert_lyrics_payload(payload)
        except Exception as error:  # noqa: BLE001 - 歌词失败降级，不阻塞下载
            logger.warning("获取歌词失败 track_id=%s：%s", track_id, error)
            lines = ()
        self.recorder.state.save_lyrics(track_id, lyrics_to_json(lines), time.time())

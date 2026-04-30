from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Mapping

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.lyrics import (
    build_lossless_lyrics,
    build_lossy_lyrics,
    write_gb18030_lrc,
)
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.shared.output import info as output_info, warn as output_warn
from musicvault.shared.tui_progress import BatchProgress
from musicvault.shared.utils import load_json, safe_filename, save_json, workspace_rel_path

logger = logging.getLogger(__name__)

_DEFAULT_PLAYLIST = "未分类"


class ProcessService:
    def __init__(
        self,
        cfg: Config,
        api: PyncmClient,
        decryptor: Decryptor,
        organizer: Organizer,
        metadata: MetadataWriter,
        workers: int,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.decryptor = decryptor
        self.organizer = organizer
        self.metadata = metadata
        self.workers = max(1, workers)

    def run_process(
        self,
        downloaded: list[DownloadedTrack],
        include_translation: bool,
        force: bool,
        playlist_index: dict[str, dict[str, object]] | None = None,
    ) -> None:
        if downloaded:
            playlist_index = playlist_index or {}
            tasks: list[tuple[Path, Track, list[str]]] = []
            for item in downloaded:
                names = self._resolve_playlist_names(item.playlist_ids, playlist_index)
                tasks.append((Path(item.source_file), item.track, names))
            self._run_process_batch(tasks, "处理中", include_translation, force)
            return
        self._process_local(include_translation, force)

    def _resolve_playlist_names(
        self,
        playlist_ids: list[int],
        playlist_index: Mapping[str, Mapping[str, object]],
    ) -> list[str]:
        names: list[str] = []
        for pid in playlist_ids:
            entry = playlist_index.get(str(pid))
            name = str(entry["name"]) if entry and entry.get("name") else str(pid)
            names.append(safe_filename(name))
        return names or [_DEFAULT_PLAYLIST]

    def _build_track_playlists(self) -> dict[int, list[int]]:
        """通过 API 获取所有配置歌单的 track_id -> [playlist_id] 映射。"""
        mapping: dict[int, list[int]] = {}
        for pid in self.cfg.playlist_ids:
            try:
                tracks = self.api.get_playlist_tracks(pid)
            except Exception:
                logger.info("获取歌单曲目失败 playlist_id=%s，跳过分类", pid)
                continue
            for track in tracks:
                mapping.setdefault(track.id, []).append(pid)
        return mapping

    def _process_local(self, include_translation: bool, force: bool) -> None:
        raw_files = list(self._iter_downloads())
        processed = load_json(self.cfg.processed_state_file, {})
        if raw_files and not isinstance(processed, dict):
            processed = {}
        if raw_files and not processed:
            output_warn(
                f"下载目录有 {len(raw_files)} 个文件，但 processed_files.json 为空，"
                "无法匹配 track_id。请先执行 sync 建立索引。"
            )
        pending: list[tuple[Path, int]] = []
        for raw_file in raw_files:
            track_id = self._guess_track_id(raw_file, index=processed)
            if track_id is None:
                logger.info("跳过文件：无法推断 track_id，文件=%s", raw_file.name)
                continue
            pending.append((raw_file, track_id))

        track_playlists = self._build_track_playlists()
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})

        detail_map = self.api.get_tracks_detail([track_id for _, track_id in pending])
        tasks: list[tuple[Path, Track, list[str]]] = []
        for raw_file, track_id in pending:
            track_info = detail_map.get(track_id) or self._fallback_track(track_id, raw_file.stem)
            pids = track_playlists.get(track_id, [])
            names = self._resolve_playlist_names(pids, playlist_index)
            tasks.append((raw_file, track_info, names))

        self._run_process_batch(tasks, "处理中", include_translation, force)

    def _run_process_batch(
        self,
        tasks: list[tuple[Path, Track, list[str]]],
        stage_name: str,
        include_translation: bool,
        force: bool,
    ) -> None:
        if not tasks:
            logger.info("处理队列为空：阶段=%s", stage_name)
            return

        processed_index = self._load_processed_index()
        pending, skipped = self._filter_pending(tasks, processed_index, force=force)
        logger.info("已处理索引过滤：阶段=%s force=%s 跳过=%s 待处理=%s", stage_name, force, skipped, len(pending))
        if not pending:
            output_info(f"处理队列为空（全部已处理）：{stage_name}")
            return

        total = len(pending)
        workers = min(self.workers, total)

        with ThreadPoolExecutor(max_workers=workers) as pool, BatchProgress(total=total, phase=stage_name) as bp:
            future_map = {
                pool.submit(self._process_file, raw_file, track_info, include_translation, playlist_names): (
                    idx,
                    raw_file,
                )
                for idx, (raw_file, track_info, playlist_names) in enumerate(pending, start=1)
            }

            try:
                for future in as_completed(future_map):
                    idx, raw_file = future_map[future]
                    try:
                        primary_lossless, primary_lossy, link_targets = future.result()
                        self._mark_processed(raw_file, primary_lossless, primary_lossy, link_targets, processed_index)
                        bp.advance(success=True, idx=idx, item_name=raw_file.name)
                    except Exception as exc:
                        bp.advance(success=False, idx=idx, item_name=raw_file.name)
                        logger.error(
                            "处理失败：阶段=%s #%s %s，原因：%s", stage_name, idx, raw_file.name, exc, exc_info=True
                        )
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                if processed_index:
                    output_warn(f"Ctrl+C 中断，保存已完成的 {len(processed_index)} 项处理...")
                    self._save_processed_index(processed_index)
                raise

        self._save_processed_index(processed_index)

    def _process_file(
        self,
        raw_file: Path,
        prefetched_track: Track | None = None,
        include_translation: bool = True,
        playlist_names: list[str] | None = None,
    ) -> tuple[Path, Path, list[tuple[Path, Path]]]:
        track_info = prefetched_track
        track_id = prefetched_track.id if prefetched_track else None
        if track_info is None:
            track_id = self._guess_track_id(raw_file)
            if track_id is None:
                raise RuntimeError(f"无法推断 track_id：{raw_file.name}")
            track_info = self._safe_track(track_id, raw_file.stem)

        if track_id is None:
            raise RuntimeError(f"无法推断 track_id：{raw_file.name}")
        downloaded = DownloadedTrack(
            track=track_info,
            source_file=str(raw_file),
            is_ncm=raw_file.suffix.lower() == ".ncm",
        )
        decoded = self.decryptor.decrypt_if_needed(downloaded, self.cfg.workspace_path / "decoded")

        names = playlist_names or [_DEFAULT_PLAYLIST]
        primary_name = names[0]

        lossless_dir = self.cfg.lossless_dir / primary_name
        lossy_dir = self.cfg.lossy_dir / primary_name

        primary_lossless, primary_lossy = self.organizer.route_audio(decoded, track_info, lossless_dir, lossy_dir)

        # 为多歌单的共享曲目创建硬链接
        link_targets: list[tuple[Path, Path]] = []
        for name in names[1:]:
            alt_lossless_dir = self.cfg.lossless_dir / name
            alt_lossy_dir = self.cfg.lossy_dir / name
            alt_lossless_dir.mkdir(parents=True, exist_ok=True)
            alt_lossy_dir.mkdir(parents=True, exist_ok=True)

            link_lossless = alt_lossless_dir / primary_lossless.name
            link_lossy = alt_lossy_dir / primary_lossy.name
            self._hardlink_or_copy(primary_lossless, link_lossless)
            self._hardlink_or_copy(primary_lossy, link_lossy)
            link_targets.append((link_lossless, link_lossy))

        lyrics = self.api.get_track_lyrics(track_id)
        lossless_lyrics = build_lossless_lyrics(lyrics, include_translation=include_translation)
        lossy_lyrics = build_lossy_lyrics(lyrics, include_translation=include_translation)

        self.metadata.write(primary_lossless, track_info, lyric_text=lossless_lyrics, is_lossless=True)
        self.metadata.write(primary_lossy, track_info, lyric_text=None, is_lossless=False)
        write_gb18030_lrc(primary_lossy, lossy_lyrics, encodings=self.cfg.lossy_lrc_encodings)

        if downloaded.is_ncm and decoded.exists():
            decoded.unlink()
        return primary_lossless, primary_lossy, link_targets

    @staticmethod
    def _hardlink_or_copy(src: Path, dst: Path) -> None:
        if dst.exists():
            return
        try:
            os.link(src, dst)
        except OSError:
            import shutil

            shutil.copy2(src, dst)

    def _iter_downloads(self) -> Iterable[Path]:
        allowed = {".ncm", ".flac", ".mp3", ".m4a", ".aac", ".wav"}
        if not self.cfg.downloads_dir.exists():
            return
        for file_path in self.cfg.downloads_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in allowed:
                yield file_path

    def _guess_track_id(self, file_path: Path, index: Mapping[str, object] | None = None) -> int | None:
        index_map: Mapping[str, object]
        if index is None:
            index_path = self.cfg.processed_state_file
            loaded = load_json(index_path, {})
            index_map = loaded if isinstance(loaded, dict) else {}
        else:
            index_map = index

        rel = workspace_rel_path(file_path, self.cfg.workspace_path)
        entry = index_map.get(rel)
        if isinstance(entry, dict):
            try:
                return int(entry.get("track_id", 0))
            except (TypeError, ValueError):
                return None
        # 兼容旧格式：value 直接是 track_id (int 或 str)
        if isinstance(entry, (int, str)):
            try:
                return int(entry)
            except (TypeError, ValueError):
                return None
        return None

    def _load_processed_index(self) -> dict[str, dict[str, object]]:
        loaded = load_json(self.cfg.processed_state_file, {})
        if not isinstance(loaded, dict):
            return {}
        normalized: dict[str, dict[str, object]] = {}
        stale_keys: list[str] = []
        for key, value in loaded.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            lossless_rel = value.get("lossless")
            lossy_rel = value.get("lossy")
            ll_exists = isinstance(lossless_rel, str) and (self.cfg.workspace_path / lossless_rel).exists()
            ly_exists = isinstance(lossy_rel, str) and (self.cfg.workspace_path / lossy_rel).exists()
            source_exists = (self.cfg.workspace_path / key).exists()
            if not ll_exists and not ly_exists and not source_exists:
                stale_keys.append(key)
                continue
            normalized[key] = dict(value)
        if stale_keys:
            save_json(self.cfg.processed_state_file, normalized)
            logger.info("清理过期处理记录：%s 条（输出及源文件均已不存在）", len(stale_keys))
        return normalized

    def _save_processed_index(self, index: dict[str, dict[str, object]]) -> None:
        save_json(self.cfg.processed_state_file, index)

    def _filter_pending(
        self,
        tasks: list[tuple[Path, Track, list[str]]],
        processed_index: Mapping[str, Mapping[str, object]],
        force: bool,
    ) -> tuple[list[tuple[Path, Track, list[str]]], int]:
        if force:
            return tasks, 0
        pending: list[tuple[Path, Track, list[str]]] = []
        skipped = 0
        for raw_file, track, playlist_names in tasks:
            if self._is_processed(raw_file, processed_index):
                skipped += 1
                logger.info("跳过已处理文件：%s", raw_file.name)
                continue
            pending.append((raw_file, track, playlist_names))
        return pending, skipped

    def _is_processed(self, raw_file: Path, processed_index: Mapping[str, Mapping[str, object]]) -> bool:
        rel = workspace_rel_path(raw_file, self.cfg.workspace_path)
        record = processed_index.get(rel)
        if not isinstance(record, Mapping) or not raw_file.exists():
            return False
        try:
            stat = raw_file.stat()
        except OSError:
            return False
        mtime = record.get("source_mtime_ns")
        size = record.get("source_size")
        try:
            return int(mtime) == stat.st_mtime_ns and int(size) == stat.st_size
        except (TypeError, ValueError):
            return False

    def _mark_processed(
        self,
        raw_file: Path,
        lossless_path: Path,
        lossy_path: Path,
        link_targets: list[tuple[Path, Path]],
        processed_index: dict[str, dict[str, object]],
    ) -> None:
        try:
            stat = raw_file.stat()
        except OSError:
            return
        rel = workspace_rel_path(raw_file, self.cfg.workspace_path)
        links_data = [
            {
                "lossless": workspace_rel_path(ll, self.cfg.workspace_path),
                "lossy": workspace_rel_path(ly, self.cfg.workspace_path),
            }
            for ll, ly in link_targets
        ]
        # 保留已存在的 track_id（由 sync 阶段写入）
        existing = processed_index.get(rel)
        track_id = existing.get("track_id") if isinstance(existing, dict) else None
        processed_index[rel] = {
            "track_id": track_id,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_size": stat.st_size,
            "lossless": workspace_rel_path(lossless_path, self.cfg.workspace_path),
            "lossy": workspace_rel_path(lossy_path, self.cfg.workspace_path),
            "links": links_data,
            "updated_at": int(time.time()),
        }

    def _safe_track(self, track_id: int, fallback_name: str) -> Track:
        detail = self.api.get_track_detail(track_id)
        if detail is not None:
            return detail
        return self._fallback_track(track_id, fallback_name)

    @staticmethod
    def _fallback_track(track_id: int, fallback_name: str) -> Track:
        return Track(
            id=track_id,
            name=fallback_name,
            artists=[],
            album="Unknown Album",
            cover_url=None,
            raw={},
        )

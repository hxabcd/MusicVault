from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Mapping

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.lyrics import (
    build_lossless_lyrics,
    build_lossy_lyrics,
    write_gb2312_lrc,
)
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import AppConfig
from musicvault.core.models import DownloadedTrack, Track
from musicvault.shared.utils import load_json, save_json

logger = logging.getLogger(__name__)


class ProcessService:
    def __init__(
        self,
        cfg: AppConfig,
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
    ) -> None:
        if downloaded:
            tasks = [(Path(item.source_file), item.track) for item in downloaded]
            self._run_process_batch(tasks, "new", include_translation, force)
            return
        self._process_local(include_translation, force)

    def _process_local(self, include_translation: bool, force: bool) -> None:
        raw_files = list(self._iter_downloads())
        index_path = self.cfg.state_dir / "file_track_index.json"
        file_index = load_json(index_path, {})
        pending: list[tuple[Path, int]] = []
        for raw_file in raw_files:
            track_id = self._guess_track_id(raw_file, index=file_index)
            if track_id is None:
                logger.warning("跳过文件：无法推断 track_id，文件=%s", raw_file.name)
                continue
            pending.append((raw_file, track_id))

        detail_map = self.api.get_tracks_detail([track_id for _, track_id in pending])
        tasks: list[tuple[Path, Track]] = []
        for raw_file, track_id in pending:
            track_info = detail_map.get(track_id) or self._fallback_track(track_id, raw_file.stem)
            tasks.append((raw_file, track_info))

        self._run_process_batch(tasks, "local", include_translation, force)

    def _run_process_batch(
        self,
        tasks: list[tuple[Path, Track]],
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
            logger.info("处理队列为空（全部已处理）：阶段=%s", stage_name)
            return

        total = len(pending)
        workers = min(self.workers, total)
        done = 0
        failed = 0
        started = time.perf_counter()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(self._process_file, raw_file, track_info, include_translation): (idx, raw_file)
                for idx, (raw_file, track_info) in enumerate(pending, start=1)
            }

            for future in as_completed(future_map):
                idx, raw_file = future_map[future]
                try:
                    lossless_path, lossy_path = future.result()
                    done += 1
                    self._mark_processed(raw_file, lossless_path, lossy_path, processed_index)
                    logger.info(
                        "处理进度：阶段=%s %s/%s 完成 #%-3s %s", stage_name, done + failed, total, idx, raw_file.name
                    )
                except Exception as exc:
                    failed += 1
                    logger.error("处理失败：阶段=%s #%s %s，原因：%s", stage_name, idx, raw_file.name, exc)

        elapsed = time.perf_counter() - started
        logger.info(
            "处理队列结束：阶段=%s 总数=%s 成功=%s 失败=%s 耗时=%.1fs", stage_name, total, done, failed, elapsed
        )
        self._save_processed_index(processed_index)

    def _process_file(
        self,
        raw_file: Path,
        prefetched_track: Track | None = None,
        include_translation: bool = True,
    ) -> tuple[Path, Path]:
        track_info = prefetched_track
        track_id = prefetched_track.id if prefetched_track else None
        if track_info is None:
            track_id = self._guess_track_id(raw_file)
            if track_id is None:
                raise RuntimeError(f"无法推断 track_id：{raw_file.name}")
            track_info = self._safe_track(track_id, raw_file.stem)

        assert track_id is not None
        downloaded = DownloadedTrack(
            track=track_info,
            source_file=str(raw_file),
            is_ncm=raw_file.suffix.lower() == ".ncm",
        )
        decoded = self.decryptor.decrypt_if_needed(downloaded, self.cfg.workspace / "decoded")
        lossless_path, lossy_path = self.organizer.route_audio(
            decoded, track_info, self.cfg.lossless_dir, self.cfg.lossy_dir
        )

        lyrics = self.api.get_track_lyrics(track_id)
        lossless_lyrics = build_lossless_lyrics(lyrics, include_translation=include_translation)
        lossy_lyrics = build_lossy_lyrics(lyrics, include_translation=include_translation)

        self.metadata.write(lossless_path, track_info, lyric_text=lossless_lyrics, is_lossless=True)
        self.metadata.write(lossy_path, track_info, lyric_text=None, is_lossless=False)
        write_gb2312_lrc(lossy_path, lossy_lyrics, encodings=self.cfg.lossy_lrc_encodings)

        if downloaded.is_ncm and decoded.exists():
            decoded.unlink()
        return lossless_path, lossy_path

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
            index_path = self.cfg.state_dir / "file_track_index.json"
            loaded = load_json(index_path, {})
            index_map = loaded if isinstance(loaded, dict) else {}
        else:
            index_map = index

        raw = index_map.get(str(file_path.resolve()))
        if raw is None or not isinstance(raw, (int, str)):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _load_processed_index(self) -> dict[str, dict[str, object]]:
        loaded = load_json(self.cfg.processed_state_file, {})
        if not isinstance(loaded, dict):
            return {}
        normalized: dict[str, dict[str, object]] = {}
        for key, value in loaded.items():
            if isinstance(key, str) and isinstance(value, dict):
                normalized[key] = dict(value)
        return normalized

    def _save_processed_index(self, index: dict[str, dict[str, object]]) -> None:
        save_json(self.cfg.processed_state_file, index)

    def _filter_pending(
        self,
        tasks: list[tuple[Path, Track]],
        processed_index: Mapping[str, Mapping[str, object]],
        force: bool,
    ) -> tuple[list[tuple[Path, Track]], int]:
        if force:
            return tasks, 0
        pending: list[tuple[Path, Track]] = []
        skipped = 0
        for raw_file, track in tasks:
            if self._is_processed(raw_file, processed_index):
                skipped += 1
                logger.info("跳过已处理文件：%s", raw_file.name)
                continue
            pending.append((raw_file, track))
        return pending, skipped

    def _is_processed(self, raw_file: Path, processed_index: Mapping[str, Mapping[str, object]]) -> bool:
        record = processed_index.get(str(raw_file.resolve()))
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
        processed_index: dict[str, dict[str, object]],
    ) -> None:
        try:
            stat = raw_file.stat()
        except OSError:
            return
        processed_index[str(raw_file.resolve())] = {
            "source_mtime_ns": stat.st_mtime_ns,
            "source_size": stat.st_size,
            "lossless": str(lossless_path.resolve()),
            "lossy": str(lossy_path.resolve()),
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

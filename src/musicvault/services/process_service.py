from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Mapping

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.lyrics import (
    StandardLyrics,
    KaraokeLyrics,
    write_gb18030_lrc,
)
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.shared.tui_progress import BatchProgress
from musicvault.shared.utils import (
    create_link,
    format_track_name,
    hardlink_or_copy,
    load_json,
    remove_link,
    safe_filename,
    save_json,
    workspace_rel_path,
)

logger = logging.getLogger(__name__)

_DEFAULT_PLAYLIST = "未分类"  # 仅在 cfg.default_playlist_name 不可用时作为后备


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

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def run_process(
        self,
        downloaded: list[DownloadedTrack],
        include_translation: bool,
        translation_format: str,
        force: bool,
        playlist_index: dict[str, dict[str, object]] | None = None,
    ) -> None:
        if downloaded:
            playlist_index = playlist_index or {}
            tasks: list[tuple[Path, Track, list[str]]] = []
            for item in downloaded:
                names = self._resolve_playlist_names(item.playlist_ids, playlist_index)
                tasks.append((Path(item.source_file), item.track, names))
            self._run_process_batch(tasks, "处理中", include_translation, translation_format, force)
            return
        self._process_local(include_translation, translation_format, force)

    # ------------------------------------------------------------------
    # 处理管线
    # ------------------------------------------------------------------

    def _run_process_batch(
        self,
        tasks: list[tuple[Path, Track, list[str]]],
        stage_name: str,
        include_translation: bool,
        translation_format: str,
        force: bool,
    ) -> None:
        if not tasks:
            logger.info("处理队列为空：阶段=%s", stage_name)
            return

        processed_index = self._load_processed_index()
        pending, skipped = self._filter_pending(tasks, processed_index, force=force)
        logger.info("已处理索引过滤：阶段=%s force=%s 跳过=%s 待处理=%s", stage_name, force, skipped, len(pending))
        if not pending:
            logger.info("处理队列为空（全部已处理）：%s", stage_name)
            return

        total = len(pending)
        workers = min(self.workers, total)

        # 收集处理结果，随后统一创建 library 链接
        results: list[tuple[Path, Path, Track, list[str]]] = []

        with ThreadPoolExecutor(max_workers=workers) as pool, BatchProgress(total=total, phase=stage_name) as bp:
            future_map = {
                pool.submit(self._process_file, raw_file, track_info, include_translation, translation_format): (
                    idx,
                    raw_file,
                )
                for idx, (raw_file, track_info, _names) in enumerate(pending, start=1)
            }

            try:
                for future in as_completed(future_map):
                    idx, raw_file = future_map[future]
                    try:
                        lossless_path, lossy_path = future.result()
                        track_info = None
                        playlist_names = None
                        for rf, ti, pn in pending:
                            if rf == raw_file:
                                track_info, playlist_names = ti, pn
                                break
                        self._mark_processed(raw_file, lossless_path, lossy_path, track_info, processed_index)
                        if track_info and playlist_names:
                            results.append((lossless_path, lossy_path, track_info, playlist_names))
                        bp.advance(success=True, idx=idx, item_name=raw_file.name)
                    except Exception as exc:
                        bp.advance(success=False, idx=idx, item_name=raw_file.name)
                        logger.error(
                            "处理失败：阶段=%s #%s %s，原因：%s", stage_name, idx, raw_file.name, exc, exc_info=True
                        )
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                if processed_index:
                    self._save_processed_index(processed_index)
                raise

        self._save_processed_index(processed_index)

        # 为所有新处理的 track 创建 library 硬链接
        for lossless_path, lossy_path, track_info, playlist_names in results:
            self._link_track(lossless_path, lossy_path, track_info, playlist_names)

    def _process_file(
        self,
        raw_file: Path,
        prefetched_track: Track | None = None,
        include_translation: bool = True,
        translation_format: str = "separate",
    ) -> tuple[Path, Path]:
        """处理单个下载文件，输出 canonical 文件到 downloads/。返回 (lossless, lossy)。"""
        track_info = prefetched_track
        track_id = prefetched_track.id if prefetched_track else None
        if track_info is None:
            track_id = self._guess_track_id(raw_file)
            if track_id is None:
                raise RuntimeError(f"无法推断 track_id：{raw_file.name}")
            track_info = self._safe_track(track_id, raw_file.stem)

        if track_id is None:
            raise RuntimeError(f"无法推断 track_id：{raw_file.name}")

        # 判断是否已是 downloads/ 下的已有 FLAC（跳过解密和音频路由，仅重写元数据/歌词）
        is_existing_flac = (
            raw_file.suffix.lower() == ".flac"
            and raw_file.parent.resolve() == self.cfg.downloads_dir.resolve()
        )

        if is_existing_flac:
            lossless_path = raw_file
            lossy_path = raw_file.with_suffix(self.organizer.lossy_suffix)
            if not lossy_path.exists():
                self.organizer.transcode_lossy(raw_file, lossy_path)
        else:
            downloaded = DownloadedTrack(
                track=track_info,
                source_file=str(raw_file),
                is_ncm=raw_file.suffix.lower() == ".ncm",
            )
            decoded = self.decryptor.decrypt_if_needed(downloaded, self.cfg.workspace_path / "decoded")
            lossless_path, lossy_path = self.organizer.route_audio(decoded, track_info, self.cfg.downloads_dir)

        lyrics = self.api.get_track_lyrics(track_id)

        # Lossless：有 YRC 且启用时优先逐字，否则回退标准 LRC
        if self.cfg.karaoke_lossless and lyrics["yrc"]:
            lossless_lyrics = self._build_lyrics(KaraokeLyrics(lyrics), include_translation, translation_format)
        else:
            lossless_lyrics = self._build_lyrics(StandardLyrics(lyrics), include_translation, translation_format)

        # Lossy：同样可按配置启用逐字歌词
        if self.cfg.karaoke_lossy and lyrics["yrc"]:
            lossy_lyrics = self._build_lyrics(KaraokeLyrics(lyrics), include_translation, translation_format)
        else:
            lossy_lyrics = self._build_lyrics(StandardLyrics(lyrics), include_translation, translation_format)

        same_file = lossless_path.resolve() == lossy_path.resolve()
        self.metadata.write(lossless_path, track_info, lyric_text=lossless_lyrics, is_lossless=True)
        if not same_file:
            self.metadata.write(lossy_path, track_info, lyric_text=None, is_lossless=False)
        if self.cfg.lyrics_write_lrc_file:
            write_gb18030_lrc(lossy_path, lossy_lyrics, encodings=self.cfg.lossy_lrc_encodings)

        # 清理临时文件
        if not is_existing_flac:
            if downloaded.is_ncm and decoded.exists() and decoded != Path(downloaded.source_file):
                decoded.unlink(missing_ok=True)
            if not self.cfg.keep_downloads:
                if raw_file.exists() and raw_file.resolve() != lossless_path.resolve():
                    raw_file.unlink(missing_ok=True)

        return lossless_path, lossy_path

    def _build_lyrics(
        self,
        lyrics_obj: StandardLyrics | KaraokeLyrics,
        include_translation: bool,
        translation_format: str,
    ) -> str:
        if include_translation and self.cfg.include_romaji:
            return lyrics_obj.merge_all()
        if include_translation:
            return lyrics_obj.merge_translation(format=translation_format)
        if self.cfg.include_romaji:
            return lyrics_obj.merge_romaji(format=translation_format)
        return lyrics_obj.original

    # ------------------------------------------------------------------
    # Library 硬链接
    # ------------------------------------------------------------------

    def _link_track(
        self,
        lossless_src: Path,
        lossy_src: Path,
        track: Track,
        playlist_names: list[str],
    ) -> None:
        """为一个 track 在所有歌单目录中创建 library 硬链接。"""
        lrc_src = lossy_src.with_suffix(".lrc")
        names = playlist_names or [self.cfg.default_playlist_name]

        for name in names:
            ll_dst = self.cfg.lossless_dir / name / self._lossless_link_name(track, lossless_src.suffix)
            ly_dst = self.cfg.lossy_dir / name / self._lossy_link_name(track)
            lrc_dst = ly_dst.with_suffix(".lrc")
            create_link(lossless_src, ll_dst)
            create_link(lossy_src, ly_dst)
            if lrc_src.exists():
                create_link(lrc_src, lrc_dst)

    def _unlink_track(self, track: Track, playlist_names: list[str]) -> None:
        """删除指定歌单中的 library 硬链接（尝试 .flac / .mp3 两种扩展名）。"""
        for name in playlist_names:
            for suffix in (".flac", ".mp3"):
                ll = self.cfg.lossless_dir / name / self._lossless_link_name(track, suffix)
                remove_link(ll)
            ly = self.cfg.lossy_dir / name / self._lossy_link_name(track)
            remove_link(ly)
            remove_link(ly.with_suffix(".lrc"))

    def _update_track_links(
        self,
        track: Track,
        old_names: list[str],
        new_names: list[str],
    ) -> bool:
        """对比歌单分配变化，删除旧链接 + 创建新链接。返回是否有变更。"""
        old_set = set(old_names)
        new_set = set(new_names)
        if old_set == new_set:
            return False

        removed = old_set - new_set
        added = new_set - old_set
        if removed:
            self._unlink_track(track, list(removed))
        if added:
            lossless_src = self._find_lossless_canonical(track.id)
            lossy_src = self.cfg.downloads_dir / f"{track.id}.mp3"
            if lossless_src and lossy_src.exists():
                self._link_track(lossless_src, lossy_src, track, list(added))
        return True

    def _find_lossless_canonical(self, track_id: int) -> Path | None:
        """查找 lossless canonical 文件（.flac 或 .mp3）。"""
        for ext in (".flac", ".mp3"):
            p = self.cfg.downloads_dir / f"{track_id}{ext}"
            if p.exists():
                return p
        return None

    # ------------------------------------------------------------------
    # 链接文件名生成
    # ------------------------------------------------------------------

    def _lossless_link_name(self, track: Track, suffix: str = ".flac") -> str:
        stem = format_track_name(self.cfg.filename_lossless, track)
        return stem + suffix

    def _lossy_link_name(self, track: Track, suffix: str = ".mp3") -> str:
        stem = format_track_name(self.cfg.filename_lossy, track)
        return stem + suffix

    # ------------------------------------------------------------------
    # processed_files.json（新格式：key = track_id 字符串）
    # ------------------------------------------------------------------

    def _load_processed_index(self) -> dict[str, dict[str, object]]:
        loaded = load_json(self.cfg.processed_state_file, {})
        if not isinstance(loaded, dict):
            return {}
        normalized: dict[str, dict[str, object]] = {}
        for key, value in loaded.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            normalized[key] = dict(value)
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
            if str(track.id) in processed_index:
                skipped += 1
                logger.info("跳过已处理文件：track_id=%s", track.id)
                continue
            pending.append((raw_file, track, playlist_names))
        return pending, skipped

    def _mark_processed(
        self,
        raw_file: Path,
        lossless_path: Path,
        lossy_path: Path,
        track: Track | None,
        processed_index: dict[str, dict[str, object]],
    ) -> None:
        if track is None:
            return
        lrc_path = lossy_path.with_suffix(".lrc")
        processed_index[str(track.id)] = {
            "flac": str(lossless_path.relative_to(self.cfg.workspace_path)),
            "mp3": str(lossy_path.relative_to(self.cfg.workspace_path)),
            "lrc": str(lrc_path.relative_to(self.cfg.workspace_path)) if lrc_path.exists() else "",
            "updated_at": int(time.time()),
        }

    # ------------------------------------------------------------------
    # 本地处理（msv process 独立模式）
    # ------------------------------------------------------------------

    def _process_local(self, include_translation: bool, translation_format: str, force: bool) -> None:
        pending: list[tuple[Path, int]] = []

        # 1. 从 cache 解析待处理文件
        cache_files = [f for f in self._iter_downloads() if not f.stem.isdigit()]
        if cache_files:
            processed = load_json(self.cfg.processed_state_file, {})
            if not isinstance(processed, dict):
                processed = {}
            for raw_file in cache_files:
                track_id = self._guess_track_id(raw_file, index=processed)
                if track_id is None:
                    logger.info("跳过文件：无法推断 track_id，文件=%s", raw_file.name)
                    continue
                pending.append((raw_file, track_id))

        # 2. 合并 downloads/ 下已有的 FLAC（文件名即 track_id）
        seen_ids = {pid for _, pid in pending}
        for flac_path, track_id in self._scan_existing_flacs():
            if track_id not in seen_ids:
                pending.append((flac_path, track_id))
                seen_ids.add(track_id)

        if not pending:
            logger.info("下载目录中无待处理文件")
            return

        track_playlists = self._build_track_playlists()
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})

        detail_map = self.api.get_tracks_detail([track_id for _, track_id in pending])
        tasks: list[tuple[Path, Track, list[str]]] = []
        for raw_file, track_id in pending:
            track_info = detail_map.get(track_id) or self._fallback_track(track_id, raw_file.stem)
            pids = track_playlists.get(track_id, [])
            names = self._resolve_playlist_names(pids, playlist_index)
            tasks.append((raw_file, track_info, names))

        self._run_process_batch(tasks, "处理中", include_translation, translation_format, force)

    def _build_track_playlists(self) -> dict[int, list[int]]:
        mapping: dict[int, list[int]] = {}
        for pid in self.cfg.get_playlist_ids():
            try:
                tracks = self.api.get_playlist_tracks(pid)
            except Exception:
                logger.info("获取歌单曲目失败 playlist_id=%s，跳过分类", pid)
                continue
            for track in tracks:
                mapping.setdefault(track.id, []).append(pid)
        return mapping

    def _iter_downloads(self) -> Iterable[Path]:
        allowed = {".ncm", ".flac", ".mp3", ".m4a", ".aac", ".wav"}
        if not self.cfg.downloads_cache_dir.exists():
            return
        for file_path in self.cfg.downloads_cache_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in allowed:
                yield file_path

    def _scan_existing_flacs(self) -> list[tuple[Path, int]]:
        """回退扫描：从 downloads/ 目录读取已有 {track_id}.flac 文件"""
        downloads = self.cfg.downloads_dir
        if not downloads.exists():
            return []
        result: list[tuple[Path, int]] = []
        for file_path in sorted(downloads.iterdir()):
            if not file_path.is_file() or file_path.suffix.lower() != ".flac":
                continue
            if not file_path.stem.isdigit():
                continue
            result.append((file_path, int(file_path.stem)))
        return result

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
        return names or [self.cfg.default_playlist_name]

    # ------------------------------------------------------------------
    # track_id 推断
    # ------------------------------------------------------------------

    def _guess_track_id(self, file_path: Path, index: Mapping[str, object] | None = None) -> int | None:
        index_map: Mapping[str, object]
        if index is None:
            index_path = self.cfg.processed_state_file
            loaded = load_json(index_path, {})
            index_map = loaded if isinstance(loaded, dict) else {}
        else:
            index_map = index

        rel = workspace_rel_path(file_path, self.cfg.workspace_path)
        for key, value in index_map.items():
            if not isinstance(value, dict):
                continue
            for field in ("flac", "mp3"):
                if value.get(field) == rel:
                    try:
                        return int(key)
                    except (TypeError, ValueError):
                        pass
        return None

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

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

    @staticmethod
    def _hardlink_or_copy(src: Path, dst: Path) -> None:
        hardlink_or_copy(src, dst)

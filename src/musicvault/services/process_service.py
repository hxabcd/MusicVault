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
)
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.core.preset import Preset, audio_spec_key, build_audio_specs
from musicvault.shared.tui_progress import BatchProgress
from musicvault.shared.utils import (
    create_link,
    format_track_name,
    load_json,
    safe_filename,
    save_json,
    workspace_rel_path,
)

logger = logging.getLogger(__name__)


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
        force: bool,
        playlist_index: dict[str, dict[str, object]] | None = None,
    ) -> None:
        if downloaded:
            playlist_index = playlist_index or {}
            tasks: list[tuple[Path, Track, list[str]]] = []
            for item in downloaded:
                names = self._resolve_playlist_names(item.playlist_ids, playlist_index)
                tasks.append((Path(item.source_file), item.track, names))
            self._run_process_batch(tasks, "处理中", force)
            return
        self._process_local(force)

    # ------------------------------------------------------------------
    # 处理管线
    # ------------------------------------------------------------------

    def _run_process_batch(
        self,
        tasks: list[tuple[Path, Track, list[str]]],
        stage_name: str,
        force: bool,
    ) -> None:
        if not tasks:
            return

        processed_index = self._load_processed_index()
        pending, skipped = self._filter_pending(tasks, processed_index, force=force)
        logger.info("已处理索引过滤：阶段=%s force=%s 跳过=%s 待处理=%s", stage_name, force, skipped, len(pending))
        if not pending:
            return

        total = len(pending)
        workers = min(self.workers, total)
        results: list[tuple[dict[str, Path], Track, list[str]]] = []

        with ThreadPoolExecutor(max_workers=workers) as pool, BatchProgress(total=total, phase=stage_name) as bp:
            future_map = {
                pool.submit(self._process_file, raw_file, track_info): (idx, raw_file)
                for idx, (raw_file, track_info, _names) in enumerate(pending, start=1)
            }

            try:
                for future in as_completed(future_map):
                    idx, raw_file = future_map[future]
                    try:
                        audio_map = future.result()
                        track_info = None
                        playlist_names = None
                        for rf, ti, pn in pending:
                            if rf == raw_file:
                                track_info, playlist_names = ti, pn
                                break
                        self._mark_processed(audio_map, processed_index)
                        if track_info and playlist_names:
                            results.append((audio_map, track_info, playlist_names))
                        bp.advance(success=True, idx=idx, item_name=raw_file.name)
                    except Exception as exc:
                        bp.advance(success=False, idx=idx, item_name=raw_file.name)
                        logger.error("处理失败：阶段=%s #%s %s，原因：%s", stage_name, idx, raw_file.name, exc, exc_info=True)
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                if processed_index:
                    self._save_processed_index(processed_index)
                raise

        self._save_processed_index(processed_index)

        for audio_map, track_info, playlist_names in results:
            self._link_track(audio_map, track_info, playlist_names)

    def _process_file(
        self,
        raw_file: Path,
        prefetched_track: Track | None = None,
    ) -> dict[str, Path]:
        """处理单个文件，返回 {spec_key: canonical_path}。"""
        track_info = prefetched_track
        track_id = prefetched_track.id if prefetched_track else None
        if track_info is None:
            track_id = self._guess_track_id(raw_file)
            if track_id is None:
                raise RuntimeError(f"无法推断 track_id：{raw_file.name}")
            track_info = self._safe_track(track_id, raw_file.stem)

        if track_id is None:
            raise RuntimeError(f"无法推断 track_id：{raw_file.name}")

        # 年份回退
        if not track_info.raw.get("publishTime"):
            al = track_info.raw.get("al") or {}
            album_id = al.get("id")
            if album_id:
                try:
                    import pyncm.apis.album as album_api
                    from musicvault.adapters.providers.pyncm_client import _retry_api
                    alb_resp = _retry_api(album_api.GetAlbumInfo, int(album_id))
                    alb_pt = (alb_resp.get("album") or {}).get("publishTime")
                    if alb_pt:
                        track_info.raw["publishTime"] = alb_pt
                except Exception:
                    pass

        # 判断是否已是 canonical 文件
        is_canonical = (
            raw_file.parent.resolve() == self.cfg.downloads_dir.resolve()
            and raw_file.stem.isdigit()
        )

        audio_specs = build_audio_specs(self.cfg.presets)
        if is_canonical:
            audio_map: dict[str, Path] = {}
            existing_spec = self._spec_from_canonical(raw_file)
            if existing_spec:
                audio_map[audio_spec_key(*existing_spec)] = raw_file
            for spec in audio_specs:
                key = audio_spec_key(*spec)
                if key not in audio_map:
                    result = self.organizer.route_audio(raw_file, track_info, self.cfg.downloads_dir, {spec})
                    if spec in result:
                        audio_map[key] = result[spec]
        else:
            downloaded = DownloadedTrack(
                track=track_info, source_file=str(raw_file),
                is_ncm=raw_file.suffix.lower() == ".ncm",
            )
            decoded = self.decryptor.decrypt_if_needed(downloaded, self.cfg.workspace_path / "decoded")
            raw_result = self.organizer.route_audio(decoded, track_info, self.cfg.downloads_dir, audio_specs)
            audio_map = {audio_spec_key(fmt, br): p for (fmt, br), p in raw_result.items()}

        # 获取歌词（一次 API 调用）
        lyrics = self.api.get_track_lyrics(track_id)

        # 确定每个 canonical 文件的合并策略
        spec_presets: dict[str, list[Preset]] = {}
        for preset in self.cfg.presets:
            key = audio_spec_key(preset.format, preset.bitrate)
            spec_presets.setdefault(key, []).append(preset)

        # 写元数据（每个 canonical 文件一次）
        for spec_key, canon_path in audio_map.items():
            presets_for_spec = spec_presets.get(spec_key, [])
            embed_cover = any(p.embed_cover for p in presets_for_spec)
            embed_lyrics = any(p.embed_lyrics for p in presets_for_spec)
            cover_max_size = max((p.cover_max_size for p in presets_for_spec), default=0)
            mf_union: set[str] = set()
            for p in presets_for_spec:
                mf_union |= set(p.metadata_fields)

            best_lyric = self._pick_best_lyric(lyrics, presets_for_spec)

            self.metadata.write(
                canon_path, track_info,
                lyric_text=best_lyric if embed_lyrics else None,
                embed_cover=embed_cover,
                embed_lyrics=embed_lyrics,
                cover_timeout=self.cfg.network_cover_timeout,
                cover_max_size=cover_max_size,
                metadata_fields=frozenset(mf_union),
            )

        # LRC 文件（按 preset 独立）
        for preset in self.cfg.presets:
            if not preset.write_lrc_file:
                continue
            spec_key = audio_spec_key(preset.format, preset.bitrate)
            canon_path = audio_map.get(spec_key)
            if not canon_path:
                continue
            lyric_text = self._build_lyrics_for_preset(lyrics, preset)
            lrc_path = canon_path.with_name(f"{track_id}.{preset.name}.lrc")
            _write_lrc(lrc_path, lyric_text, encodings=preset.lrc_encodings)

        # 清理临时文件
        if not is_canonical:
            if not self.cfg.keep_downloads:
                if raw_file.exists():
                    raw_file.unlink(missing_ok=True)

        return audio_map

    def _pick_best_lyric(self, lyrics: dict[str, str], presets: list[Preset]) -> str | None:
        if not presets:
            return None

        def score(p: Preset) -> int:
            s = 0
            if p.use_karaoke:
                s += 100
            if p.include_translation:
                s += 10
            if p.include_romaji:
                s += 1
            return s

        best = max(presets, key=score)
        fmt = best.translation_format

        if best.use_karaoke and lyrics.get("yrc"):
            lyr_obj = KaraokeLyrics(lyrics)
        else:
            lyr_obj = StandardLyrics(lyrics)

        if best.include_translation and best.include_romaji:
            return lyr_obj.merge_all(format=fmt)
        if best.include_translation:
            return lyr_obj.merge_translation(format=fmt)
        if best.include_romaji:
            return lyr_obj.merge_romaji(format=fmt)
        return lyr_obj.original

    def _build_lyrics_for_preset(self, lyrics: dict[str, str], preset: Preset) -> str:
        if preset.use_karaoke and lyrics.get("yrc"):
            lyr_obj = KaraokeLyrics(lyrics)
        else:
            lyr_obj = StandardLyrics(lyrics)

        if preset.include_translation and preset.include_romaji:
            return lyr_obj.merge_all(format=preset.translation_format)
        if preset.include_translation:
            return lyr_obj.merge_translation(format=preset.translation_format)
        if preset.include_romaji:
            return lyr_obj.merge_romaji(format=preset.translation_format)
        return lyr_obj.original

    def _spec_from_canonical(self, path: Path) -> tuple[str | None, str | None] | None:
        name = path.stem
        suffix = path.suffix.lower()
        fmt_map = {".flac": "flac", ".mp3": "mp3", ".m4a": "aac", ".ogg": "ogg", ".opus": "opus"}
        fmt = fmt_map.get(suffix)
        if fmt is None:
            return None
        if "_" in name:
            parts = name.split("_", 1)
            if parts[1].rstrip("k").isdigit():
                return (fmt, parts[1])
        return (fmt, None)

    # ------------------------------------------------------------------
    # Library 硬链接
    # ------------------------------------------------------------------

    def _link_track(
        self, audio_map: dict[str, Path], track: Track, playlist_names: list[str],
    ) -> None:
        names = playlist_names or [self.cfg.default_playlist_name]
        for preset in self.cfg.presets:
            spec_key = audio_spec_key(preset.format, preset.bitrate)
            audio_src = audio_map.get(spec_key)
            if not audio_src:
                continue
            link_stem = format_track_name(preset.filename_template, track)
            for pl_name in names:
                dst_dir = self.cfg.preset_dir(preset.name) / pl_name
                dst_dir.mkdir(parents=True, exist_ok=True)
                create_link(audio_src, dst_dir / f"{link_stem}{audio_src.suffix}")
                if preset.write_lrc_file:
                    lrc_src = audio_src.with_name(f"{track.id}.{preset.name}.lrc")
                    if lrc_src.exists():
                        create_link(lrc_src, dst_dir / f"{link_stem}.lrc")

    # ------------------------------------------------------------------
    # processed_files.json
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
        self, audio_map: dict[str, Path], processed_index: dict[str, dict[str, object]],
    ) -> None:
        if not audio_map:
            return
        first_path = next(iter(audio_map.values()))
        track_id = first_path.stem.split("_")[0]
        audios: dict[str, str] = {}
        for spec_key, p in audio_map.items():
            rel = workspace_rel_path(p, self.cfg.workspace_path)
            audios[spec_key] = rel
        processed_index[track_id] = {
            "audios": audios,
            "updated_at": int(time.time()),
        }

    # ------------------------------------------------------------------
    # 本地处理（msv process 独立模式）
    # ------------------------------------------------------------------

    def _process_local(self, force: bool) -> None:
        pending: list[tuple[Path, int]] = []

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

        seen_ids = {pid for _, pid in pending}
        for canon_path, track_id in self._scan_canonical_files():
            if track_id not in seen_ids:
                pending.append((canon_path, track_id))
                seen_ids.add(track_id)

        if not pending:
            logger.info("下载目录中无待处理文件")
            return

        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        logger.info("正在获取曲目详情与歌单数据...")
        detail_map = self.api.get_tracks_detail([track_id for _, track_id in pending])
        track_playlist_map = self._build_track_playlist_map()
        tasks: list[tuple[Path, Track, list[str]]] = []
        for raw_file, track_id in pending:
            track_info = detail_map.get(track_id) or self._fallback_track(track_id, raw_file.stem)
            pids = track_playlist_map.get(track_id, [])
            names = self._resolve_playlist_names(pids, playlist_index)
            tasks.append((raw_file, track_info, names))

        self._run_process_batch(tasks, "处理中", force)

    def _build_track_playlist_map(self) -> dict[int, list[int]]:
        mapping: dict[int, list[int]] = {}
        playlist_ids = self.cfg.get_playlist_ids()
        if playlist_ids:
            logger.info("正在获取 %s 个歌单的曲目列表...", len(playlist_ids))
        for pid in playlist_ids:
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

    def _scan_canonical_files(self) -> list[tuple[Path, int]]:
        downloads = self.cfg.downloads_dir
        if not downloads.exists():
            return []
        seen: set[int] = set()
        result: list[tuple[Path, int]] = []
        for file_path in sorted(downloads.iterdir()):
            if not file_path.is_file() or file_path.suffix.lower() not in (".flac", ".mp3"):
                continue
            stem = file_path.stem.split("_")[0]
            if not stem.isdigit():
                continue
            track_id = int(stem)
            if track_id in seen:
                continue
            result.append((file_path, track_id))
            seen.add(track_id)
        return result

    def _resolve_playlist_names(
        self, playlist_ids: list[int], playlist_index: Mapping[str, Mapping[str, object]],
    ) -> list[str]:
        names: list[str] = []
        for pid in playlist_ids:
            entry = playlist_index.get(str(pid))
            name = str(entry["name"]) if entry and entry.get("name") else str(pid)
            names.append(safe_filename(name))
        return names or [self.cfg.default_playlist_name]

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
            audios = value.get("audios")
            if isinstance(audios, dict):
                for _spec_key, spec_rel in audios.items():
                    if spec_rel == rel:
                        try:
                            return int(key)
                        except (TypeError, ValueError):
                            pass
            for field in ("flac", "mp3", "source"):
                if value.get(field) == rel:
                    try:
                        return int(key)
                    except (TypeError, ValueError):
                        pass
        return None

    def _safe_track(self, track_id: int, fallback_name: str) -> Track:
        detail = self.api.get_track_detail(track_id)
        if detail is not None:
            return detail
        return self._fallback_track(track_id, fallback_name)

    @staticmethod
    def _fallback_track(track_id: int, fallback_name: str) -> Track:
        return Track(id=track_id, name=fallback_name, artists=[], album="Unknown Album", cover_url=None, raw={})


def _write_lrc(target: Path, lyric_text: str, encodings: tuple[str, ...] = ("utf-8",)) -> Path:
    content = lyric_text or ""
    fallback_encodings = tuple(e for e in encodings if str(e).strip())
    if not fallback_encodings:
        fallback_encodings = ("utf-8",)
    for encoding in fallback_encodings:
        try:
            target.write_bytes(content.encode(encoding))
            return target
        except UnicodeEncodeError:
            continue
    target.write_bytes(content.encode("utf-8", errors="replace"))
    return target

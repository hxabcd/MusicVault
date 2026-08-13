from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.application.progress import ProgressReporter
from musicvault.application.source_state import SourceStateRecorder, build_audio_asset_from_file
from musicvault.core.config import Config
from musicvault.domain.lyrics import lyrics_from_json
from musicvault.domain.models import DownloadedTrack, MediaAsset, Track
from musicvault.ports.process_state import ProcessStateRepository
from musicvault.ports.source import SourceClient
from musicvault.ports.source_state import SourceStateRepository
from musicvault.preset_api.v1 import (
    AudioFormat,
    BasePreset,
    LyricEncoding,
    MetadataField,
    MetadataSpec,
    PresetLoadError,
    audio_spec_key,
)
from musicvault.shared.utils import workspace_rel_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """process 运行的结构化结果。"""

    processed: int = 0
    skipped: int = 0
    failed: int = 0


class ProcessUseCase:
    """处理应用用例：解码、转码、元数据与离线歌词文件写出。"""

    def __init__(
        self,
        cfg: Config,
        api: SourceClient,
        decryptor: Decryptor,
        organizer: Organizer,
        metadata: MetadataWriter,
        workers: int,
        state: SourceStateRepository,
        process_state: ProcessStateRepository,
        dry_run: bool = False,
        presets: Mapping[str, BasePreset] | None = None,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.decryptor = decryptor
        self.organizer = organizer
        self.metadata = metadata
        self.workers = max(1, workers)
        self.dry_run = dry_run
        # workspace 各生命周期区域路径的唯一来源（cache/media_store/library/logs）
        self.paths = WorkspacePaths(cfg.workspace_path)
        # 把本次处理产出的媒体资产登记到 SQLite，供 distribute 阶段消费
        self.recorder = SourceStateRecorder(state)
        self.process_state = process_state
        # 歌词/元数据/音频规格只按注入的 preset 实例（v1 脚本）执行，无领域 Preset 回退
        if presets is None:
            raise PresetLoadError("ProcessUseCase 缺少 preset 实例索引（presets 参数）：请经 build_pipeline 组装注入")
        self._validate_preset_keys(presets)
        self.presets = presets
        # 曲目详情实例级缓存：存量 canonical 扫描与后续处理对同一曲目只打一次详情 API。
        # 无并发问题：scan 路径在主线程，_process_file 收到 prefetched_track 时不再走 _safe_track。
        self._track_detail_cache: dict[int, Track | None] = {}

    @staticmethod
    def _validate_preset_keys(presets: Mapping[str, BasePreset]) -> None:
        """校验注入键与 preset.name 一致：LRC 文件名与分发侧按注册名拼路径，键名漂移会静默失配。

        脚本未声明 name（空串）时不校验，仅防止「声明了名字却用别名键注入」的拼写错误。
        """
        for key, preset in presets.items():
            preset_name = getattr(preset, "name", "")
            if preset_name and preset_name != key:
                raise PresetLoadError(
                    f"preset 索引键 '{key}' 与 preset.name '{preset_name}' 不一致："
                    "LRC 文件名与分发侧按注册名拼路径，请用注册名作为注入键"
                )

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def run_process(
        self,
        downloaded: list[DownloadedTrack],
        force: bool,
        *,
        progress: ProgressReporter | None = None,
    ) -> ProcessResult:
        tasks: list[tuple[Path, Track]] = [(Path(item.source_file), item.track) for item in downloaded]
        # 补充扫描 media_store 中已存在的 canonical 曲目：preset 声明变更（新增规格或
        # 缺失的 per-preset 歌词文件）后，存量曲目按需重新处理；force 时无条件全部加入。
        seen_ids = {item.track.id for item in downloaded}
        scan_skipped = 0
        scan_failed = 0
        for canon_path, track_id in self._scan_canonical_files():
            if track_id in seen_ids:
                continue
            if not force and self._is_fully_processed(track_id):
                scan_skipped += 1
                continue
            try:
                track_info = self._safe_track(track_id, canon_path.stem)
            except Exception as exc:
                # 存量曲目详情 API 失败（重试耗尽后抛 NcmApiError/OSError/TimeoutError）：
                # 单曲降级计入 failed 并跳过，不阻塞其余曲目（与 _process_file 失败语义一致）。
                # 只捕获 Exception，KeyboardInterrupt 等 BaseException 照常向上冒泡。
                scan_failed += 1
                logger.error(
                    "存量曲目详情获取失败（跳过）：track_id=%s %s，原因：%s",
                    track_id,
                    canon_path.name,
                    exc,
                    exc_info=True,
                )
                continue
            tasks.append((canon_path, track_info))
            seen_ids.add(track_id)
        result = self._run_process_batch(tasks, "处理中", force, progress)
        return ProcessResult(
            processed=result.processed,
            skipped=result.skipped + scan_skipped,
            failed=result.failed + scan_failed,
        )

    def _scan_canonical_files(self) -> list[tuple[Path, int]]:
        """扫描 media_store/<tid>/ 下的 canonical 音频文件（扁平布局，每曲目取一个代表文件）。"""
        if not self.paths.media_store.is_dir():
            return []
        audio_exts = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
        result: list[tuple[Path, int]] = []
        for track_dir in sorted(self.paths.media_store.iterdir()):
            if not track_dir.is_dir() or not track_dir.name.isdigit():
                continue
            for f in sorted(track_dir.iterdir()):
                if not f.is_file() or f.suffix.lower() not in audio_exts:
                    continue
                if f.stem.split("_", 1)[0] != track_dir.name:
                    continue
                result.append((f, int(track_dir.name)))
                break
        return result

    # ------------------------------------------------------------------
    # 处理管线
    # ------------------------------------------------------------------

    def _run_process_batch(
        self,
        tasks: list[tuple[Path, Track]],
        stage_name: str,
        force: bool,
        progress: ProgressReporter | None = None,
    ) -> ProcessResult:
        if not tasks:
            return ProcessResult()

        pending, skipped = self._filter_pending(tasks, force=force)
        logger.info("已处理索引过滤：阶段=%s force=%s 跳过=%s 待处理=%s", stage_name, force, skipped, len(pending))
        if not pending:
            return ProcessResult(skipped=skipped)

        if self.dry_run:
            return ProcessResult(processed=len(pending), skipped=skipped)

        total = len(pending)
        workers = min(self.workers, total)
        results: list[tuple[dict[str, Path], Track]] = []
        failed = 0

        if progress is not None:
            progress.begin(total=total, phase=stage_name)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self._process_file, raw_file, track_info, force): (idx, raw_file)
                    for idx, (raw_file, track_info) in enumerate(pending, start=1)
                }

                try:
                    for future in as_completed(future_map):
                        idx, raw_file = future_map[future]
                        try:
                            audio_map = future.result()
                            track_info = None
                            for rf, ti in pending:
                                if rf == raw_file:
                                    track_info = ti
                                    break
                            self._mark_processed(audio_map, track_info)
                            if track_info:
                                results.append((audio_map, track_info))
                            if progress is not None:
                                progress.advance(success=True, idx=idx, item_name=raw_file.name)
                        except Exception as exc:
                            failed += 1
                            if progress is not None:
                                progress.advance(success=False, idx=idx, item_name=raw_file.name)
                            logger.error(
                                "处理失败：阶段=%s #%s %s，原因：%s", stage_name, idx, raw_file.name, exc, exc_info=True
                            )
                except KeyboardInterrupt:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
        finally:
            if progress is not None:
                progress.end()

        self._record_processed_results(results)

        return ProcessResult(processed=len(results), skipped=skipped, failed=failed)

    def _record_processed_results(
        self,
        results: list[tuple[dict[str, Path], Track]],
    ) -> None:
        """把本次处理产出的曲目与 canonical 媒体资产写入 SQLite。"""
        tracks: list[Track] = []
        seen: set[int] = set()
        assets: list[MediaAsset] = []
        for audio_map, track_info in results:
            if track_info.id not in seen:
                tracks.append(track_info)
                seen.add(track_info.id)
            for spec_key, path in audio_map.items():
                assets.append(build_audio_asset_from_file(track_info.id, spec_key, path))
        self.recorder.record_source_state(tracks, media_assets=assets)

    def _process_file(
        self,
        raw_file: Path,
        prefetched_track: Track | None = None,
        force: bool = False,
    ) -> dict[str, Path]:
        """处理单个文件：解码 → 路由 → 元数据 → 歌词文件，返回 {spec_key: canonical_path}。"""
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
                    alb_resp = self.api.get_album_info(int(album_id))
                    alb_pt = (alb_resp.get("album") or {}).get("publishTime")
                    if alb_pt:
                        track_info.raw["publishTime"] = alb_pt
                except Exception:
                    pass

        audio_specs = {(p.format, p.bitrate) for p in self.presets.values()}
        track_dir = self.paths.media_store / str(track_id)
        # 判断是否已是 canonical 文件（形态 <ws>/media_store/<tid>/<tid>[_bitrate].ext，扁平布局）
        is_canonical = raw_file.parent == track_dir and raw_file.stem.split("_")[0] == str(track_id)

        if is_canonical:
            audio_map: dict[str, Path] = {}
            existing_spec = self._spec_from_canonical(raw_file)
            if existing_spec:
                audio_map[audio_spec_key(*existing_spec)] = raw_file
            for spec in audio_specs:
                key = audio_spec_key(*spec)
                if key not in audio_map:
                    result = self.organizer.route_audio(raw_file, track_info, track_dir, {spec}, force=force)
                    if spec in result:
                        audio_map[key] = result[spec]
        else:
            downloaded = DownloadedTrack(
                track=track_info,
                source_file=str(raw_file),
                is_ncm=raw_file.suffix.lower() == ".ncm",
            )
            decoded = self.decryptor.decrypt_if_needed(downloaded, self.paths.cache / "decoded")
            raw_result = self.organizer.route_audio(decoded, track_info, track_dir, audio_specs, force=force)
            audio_map = {audio_spec_key(fmt, br): p for (fmt, br), p in raw_result.items()}

        # 离线歌词：从 SQLite 读取，不再调用歌词 API
        payload = self.recorder.state.get_lyrics(track_id)
        lines = lyrics_from_json(payload) if payload else ()

        # 每个 canonical 文件按共享 spec 的 preset 并集写元数据
        spec_presets: dict[str, list[BasePreset]] = {}
        for preset in self.presets.values():
            spec_presets.setdefault(audio_spec_key(preset.format, preset.bitrate), []).append(preset)

        for spec_key, canon_path in audio_map.items():
            presets_for_spec = spec_presets.get(spec_key, [])
            merged_fields = MetadataField.NONE
            for preset in presets_for_spec:
                merged_fields |= preset.metadata.fields
            merged = MetadataSpec(
                embed_cover=any(p.metadata.embed_cover for p in presets_for_spec),
                cover_max_size=max((p.metadata.cover_max_size for p in presets_for_spec), default=0),
                fields=merged_fields,
            )
            self.metadata.write(canon_path, track_info, metadata=merged, cover_timeout=self.cfg.network_cover_timeout)

        # 歌词文件按 preset 独立（每 preset 一个 build_lyrics 输出）
        # build_lyrics 是外部 preset 脚本代码：逐行调用，异常按 preset 隔离——该 preset 歌词文件跳过，
        # 不阻塞其他 preset 的歌词文件与整曲状态（与 _save_lyrics 的降级风格一致）。
        for preset_name, preset in self.presets.items():
            try:
                rendered = []
                for line in lines:
                    text = preset.build_lyric_line(line)
                    if text:
                        rendered.append(text)
                lyric_text = "\n".join(rendered)
            except Exception as exc:  # noqa: BLE001 - preset 脚本异常按 preset 降级
                logger.warning(
                    "preset 歌词生成失败（跳过该 preset）：preset=%s track_id=%s，原因：%s",
                    preset_name,
                    track_id,
                    exc,
                )
                continue
            if not lyric_text:
                continue
            lrc_path = track_dir / f"{track_id}.{preset_name}.lrc"
            try:
                _write_lrc(lrc_path, lyric_text, encoding=preset.lyrics_encoding)
            except UnicodeEncodeError as exc:
                logger.warning(
                    "歌词编码失败（跳过该 preset 的歌词文件）：preset=%s track_id=%s 编码=%s，原因：%s",
                    preset_name,
                    track_id,
                    preset.lyrics_encoding.value,
                    exc,
                )
                continue

        # 清理临时文件
        if not is_canonical and not self.cfg.keep_downloads:
            raw_file.unlink(missing_ok=True)

        return audio_map

    def _spec_from_canonical(self, path: Path) -> tuple[AudioFormat | None, str | None] | None:
        name = path.stem
        suffix = path.suffix.lower()
        fmt_map = {
            ".flac": AudioFormat.FLAC,
            ".mp3": AudioFormat.MP3,
            ".m4a": AudioFormat.AAC,
            ".ogg": AudioFormat.OGG,
            ".opus": AudioFormat.OPUS,
        }
        fmt = fmt_map.get(suffix)
        if fmt is None:
            return None
        if "_" in name:
            parts = name.split("_", 1)
            if parts[1].rstrip("k").isdigit():
                return (fmt, parts[1])
        return (fmt, None)

    # ------------------------------------------------------------------
    # 已处理状态（processed_files.json 已被 SQLite processed_tracks 替代）
    # ------------------------------------------------------------------

    def _is_fully_processed(self, track_id: int) -> bool:
        """曲目是否已完整处理：spec 覆盖且每个 preset 的歌词文件齐全。

        无歌词原稿（get_lyrics 为空）的曲目不产出歌词文件，不要求歌词文件存在。
        """
        required_specs = {audio_spec_key(p.format, p.bitrate) for p in self.presets.values()}
        if not self.process_state.is_processed(track_id, required_specs):
            return False
        if self.recorder.state.get_lyrics(track_id) and self._missing_lyrics_presets(track_id):
            return False
        return True

    def _missing_lyrics_presets(self, track_id: int) -> list[str]:
        """返回缺少 <tid>.<preset>.lrc 歌词文件的 preset 名列表。"""
        track_dir = self.paths.media_store / str(track_id)
        return [name for name in self.presets if not (track_dir / f"{track_id}.{name}.lrc").is_file()]

    def _filter_pending(
        self,
        tasks: list[tuple[Path, Track]],
        force: bool,
    ) -> tuple[list[tuple[Path, Track]], int]:
        if force:
            return tasks, 0

        pending: list[tuple[Path, Track]] = []
        skipped = 0
        for raw_file, track in tasks:
            if self._is_fully_processed(track.id):
                skipped += 1
                logger.info("跳过已处理文件（spec 与歌词文件已覆盖）：track_id=%s", track.id)
                continue
            pending.append((raw_file, track))
        return pending, skipped

    def _mark_processed(self, audio_map: dict[str, Path], track: Track | None = None) -> None:
        if not audio_map:
            return
        first_path = next(iter(audio_map.values()))
        track_id = int(first_path.stem.split("_")[0])
        if track is not None:
            # processing_state 外键引用 tracks，先确保曲目存在
            self.recorder.state.upsert_track(track)
        self.process_state.mark_processed(track_id, time.time())

    def _guess_track_id(self, file_path: Path, index: Mapping[str, object] | None = None) -> int | None:
        """从 processing_state 反查 raw 文件所属 track_id。"""
        del index  # 旧 JSON 索引参数已废弃，保留签名兼容调用方
        rel = workspace_rel_path(file_path, self.cfg.workspace_path)
        return self.process_state.find_track_id_by_path(rel)

    def _safe_track(self, track_id: int, fallback_name: str) -> Track:
        """按需获取曲目详情，实例内单次缓存：同一曲目重复处理不再打详情 API。

        None（API 未命中）也缓存，命中时仍按当前 fallback_name 构造降级 Track。
        """
        if track_id in self._track_detail_cache:
            detail = self._track_detail_cache[track_id]
            if detail is not None:
                return detail
            return self._fallback_track(track_id, fallback_name)
        detail = self.api.get_track_detail(track_id)
        self._track_detail_cache[track_id] = detail
        if detail is not None:
            return detail
        return self._fallback_track(track_id, fallback_name)

    @staticmethod
    def _fallback_track(track_id: int, fallback_name: str) -> Track:
        return Track(id=track_id, name=fallback_name, artists=[], album="Unknown Album", cover_url=None, raw={})


def _write_lrc(
    target: Path,
    lyric_text: str,
    encoding: LyricEncoding = LyricEncoding.UTF_8,
) -> Path:
    target.write_bytes((lyric_text or "").encode(encoding.value))
    return target

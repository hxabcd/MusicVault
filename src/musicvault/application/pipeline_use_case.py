from __future__ import annotations

import os
from dataclasses import dataclass
from collections.abc import Mapping

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.application.process_use_case import ProcessUseCase
from musicvault.application.progress import ProgressReporter
from musicvault.application.source_state import SourceStateRecorder
from musicvault.application.sync_engine import SyncEngine, SyncRunResult
from musicvault.application.sync_use_case import SyncUseCase
from musicvault.core.config import Config
from musicvault.ports.process_state import ProcessStateRepository
from musicvault.ports.source import SourceClient
from musicvault.ports.source_state import SourceStateRepository
from musicvault.ports.target import TargetOperations
from musicvault.preset_api.v1 import BasePreset, PresetRegistry


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """pipeline 运行的结构化结果。"""

    downloaded: int = 0
    processed: int = 0
    pruned: int = 0
    track_count: int = 0
    playlist_count: int = 0
    dry_run_plan: dict | None = None
    distribute: SyncRunResult | None = None


class PipelineUseCase:
    """流水线用例：sync 四阶段（fetch → pull → process → distribute）编排与源侧状态登记"""

    def __init__(
        self,
        cfg: Config,
        api: SourceClient,
        state: SourceStateRepository,
        process_state: ProcessStateRepository,
        dry_run: bool = False,
        presets: Mapping[str, BasePreset] | None = None,
        registry: PresetRegistry | None = None,
        target: TargetOperations | None = None,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.dry_run = dry_run
        self.registry = registry
        self.target = target
        # preset 实例索引：process 阶段消费（歌词/元数据/规格），distribute 阶段注入 SyncEngine
        self.presets = presets
        # workspace 各生命周期区域路径的唯一来源（cache/media_store/library/logs）
        self.paths = WorkspacePaths(cfg.workspace_path)
        # 把同步/处理的源侧状态写入 SQLite，供 distribute 阶段消费
        self.recorder = SourceStateRecorder(state)

        cpu = os.cpu_count() or 4
        auto_download = max(1, min(6, cpu))
        auto_process = max(1, min(4, cpu // 2))
        auto_ffmpeg = max(1, cpu // auto_process)

        download_workers = cfg.download_workers or auto_download
        process_workers = cfg.process_workers or auto_process
        ffmpeg_threads = cfg.ffmpeg_threads or auto_ffmpeg

        self.sync_service = SyncUseCase(
            cfg=cfg,
            api=api,
            downloader=Downloader(),
            workers=max(1, download_workers),
            dry_run=dry_run,
            state=state,
            process_state=process_state,
        )
        self.process_service = ProcessUseCase(
            cfg=cfg,
            api=api,
            decryptor=Decryptor(),
            organizer=Organizer(
                ffmpeg_threads=max(1, ffmpeg_threads),
                ffmpeg_path=cfg.ffmpeg_path,
            ),
            metadata=MetadataWriter(),
            workers=max(1, process_workers),
            dry_run=dry_run,
            state=state,
            process_state=process_state,
            presets=presets,
        )

    def run_pipeline(
        self,
        cookie: str,
        *,
        distribute: bool = True,
        only_distribute: bool = False,
        progress: ProgressReporter | None = None,
    ) -> PipelineResult:
        """sync 四阶段编排：fetch → pull → process → distribute。

        only_distribute 跳过前三阶段直接分发（结果只含 distribute 字段）；
        distribute=False 时跳过收尾分发。
        dry-run 下 fetch 不执行（写 SQLite 有副作用）、pull/process 沿用
        现有 dry-run 语义、distribute 沿用 SyncEngine 的 dry-run。
        """
        if only_distribute:
            return PipelineResult(distribute=self._run_distribute())

        if not self.dry_run:
            self.cfg.ensure_dirs()

        playlist_ids = [pl.id for pl in self.recorder.state.list_playlists()]
        # fetch 写 SQLite 有副作用，dry-run 下跳过；pull 的 dry-run 只计算计划
        if not self.dry_run:
            self.sync_service.run_fetch(cookie=cookie, playlist_ids=playlist_ids)
        sync_result = self.sync_service.run_pull(cookie=cookie, playlist_ids=playlist_ids, progress=progress)
        downloaded = sync_result.downloaded

        # sync 的 dry-run 不跑 process 阶段（新下载文件尚未落地）
        processed = 0
        if not self.dry_run:
            process_result = self.process_service.run_process(
                downloaded=list(downloaded),
                force=self.cfg.force,
                progress=progress,
            )
            processed = process_result.processed

        distribute_result = None
        if distribute:
            distribute_result = self._run_distribute()

        return PipelineResult(
            downloaded=len(downloaded),
            processed=processed,
            pruned=sync_result.pruned,
            track_count=sync_result.track_count,
            playlist_count=sync_result.playlist_count,
            dry_run_plan=sync_result.dry_run_plan,
            distribute=distribute_result,
        )

    def _run_distribute(self) -> SyncRunResult | None:
        """distribute 阶段：按注册表目标分发 SQLite 快照到 library（SyncEngine 驱动）。

        返回 SyncRunResult 供 PipelineResult.distribute 携带（CLI 渲染分发结果）；
        registry/target 未注入时返回 None（旧链路仅拉取/处理的使用场景）。
        """
        if self.registry is None or self.target is None:
            return None
        engine = SyncEngine(
            target=self.target,
            dry_run=self.dry_run,
            media_store_root=self.paths.media_store,
        )
        return engine.run(
            self.recorder.state.create_snapshot(),
            self.registry.target_registrations(enabled_only=True),
            presets=self.presets,
        )

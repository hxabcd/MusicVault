from __future__ import annotations

import logging
import os

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import AppConfig
from musicvault.core.models import DownloadedTrack
from musicvault.core.options import RunOptions
from musicvault.services.process_service import ProcessService
from musicvault.services.sync_service import SyncService

logger = logging.getLogger(__name__)


class RunService:
    """同步与处理总控服务"""

    def __init__(self, cfg: AppConfig, api: PyncmClient) -> None:
        self.cfg = cfg
        self.api = api

        cpu = os.cpu_count() or 4
        auto_download_workers = max(1, min(6, cpu))
        auto_process_workers = max(1, min(4, cpu // 2))
        auto_ffmpeg_threads = max(1, cpu // auto_process_workers)

        download_workers = self._resolve_workers(cfg.download_workers, auto_download_workers)
        process_workers = self._resolve_workers(cfg.process_workers, auto_process_workers)
        ffmpeg_threads = self._resolve_workers(cfg.ffmpeg_threads, auto_ffmpeg_threads)

        self.sync_service = SyncService(
            cfg=cfg,
            api=api,
            downloader=Downloader(),
            workers=download_workers,
        )
        self.process_service = ProcessService(
            cfg=cfg,
            api=api,
            decryptor=Decryptor(),
            organizer=Organizer(ffmpeg_threads=ffmpeg_threads),
            metadata=MetadataWriter(),
            workers=process_workers,
        )

    @staticmethod
    def _resolve_workers(configured: int | None, fallback: int) -> int:
        if configured is None:
            return fallback
        return max(1, int(configured))

    def run_pipeline(self, cookie: str, options: RunOptions) -> None:
        logger.info(
            "任务开始：only_sync=%s only_process=%s include_translation=%s force=%s workspace=%s",
            options.only_sync,
            options.only_process,
            options.include_translation,
            options.force,
            self.cfg.workspace,
        )
        self.cfg.ensure_dirs()

        downloaded: list[DownloadedTrack] = []
        if not options.only_process:
            logger.info("开始同步阶段")
            downloaded = self.sync_service.run_sync(cookie=cookie, playlist_id=options.playlist_id)
            logger.info("同步阶段完成：成功下载 %s 首", len(downloaded))

        if not options.only_sync:
            logger.info("开始处理阶段")
            self.process_service.run_process(
                downloaded=downloaded,
                include_translation=options.include_translation,
                force=options.force,
            )
            logger.info("处理阶段完成")

        logger.info("任务结束")

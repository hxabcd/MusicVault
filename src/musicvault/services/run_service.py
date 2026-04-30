from __future__ import annotations

import logging
import os

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.services.process_service import ProcessService
from musicvault.services.sync_service import SyncService
from musicvault.shared.tui_progress import console, ok

logger = logging.getLogger(__name__)


class RunService:
    def __init__(self, cfg: Config, api: PyncmClient) -> None:
        self.cfg = cfg
        self.api = api

        cpu = os.cpu_count() or 4
        auto_download = max(1, min(6, cpu))
        auto_process = max(1, min(4, cpu // 2))
        auto_ffmpeg = max(1, cpu // auto_process)

        download_workers = cfg.download_workers or auto_download
        process_workers = cfg.process_workers or auto_process
        ffmpeg_threads = cfg.ffmpeg_threads or auto_ffmpeg

        self.sync_service = SyncService(
            cfg=cfg,
            api=api,
            downloader=Downloader(),
            workers=max(1, download_workers),
        )
        self.process_service = ProcessService(
            cfg=cfg,
            api=api,
            decryptor=Decryptor(),
            organizer=Organizer(ffmpeg_threads=max(1, ffmpeg_threads)),
            metadata=MetadataWriter(),
            workers=max(1, process_workers),
        )

    def run_pipeline(self, cookie: str, command: str) -> None:
        self.cfg.ensure_dirs()

        only_pull = command == "pull"
        only_process = command == "process"

        playlist_index: dict[str, dict[str, object]] = {}
        downloaded: list = []
        if not only_process:
            downloaded = self.sync_service.run_sync(cookie=cookie, playlist_ids=self.cfg.playlist_ids)
            if downloaded:
                ok(f"下载了 [bold]{len(downloaded)}[/bold] 首新曲目")
            playlist_index = self.sync_service.playlist_index

        if not only_pull:
            console.print()
            self.process_service.run_process(
                downloaded=downloaded,
                include_translation=self.cfg.include_translation,
                force=self.cfg.force,
                playlist_index=playlist_index,
            )

        console.print()
        ok("全部完成")

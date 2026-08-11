from __future__ import annotations

import logging
import os
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.application.process_use_case import ProcessUseCase
from musicvault.application.source_state import SourceStateRecorder
from musicvault.application.sync_use_case import SyncUseCase
from musicvault.core.config import Config
from musicvault.domain.preset import audio_spec_key
from musicvault.ports.source import SourceClient
from musicvault.ports.state import StateRepository
from musicvault.shared.tui_progress import console, ok
from musicvault.shared.utils import (
    create_link,
    format_track_name,
    safe_filename,
)

logger = logging.getLogger(__name__)


class PipelineUseCase:
    """流水线用例：sync/pull/process 的编排与源侧状态登记"""

    def __init__(
        self,
        cfg: Config,
        api: SourceClient,
        state: StateRepository,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.dry_run = dry_run
        # workspace 各生命周期区域路径的唯一来源（cache/media_store/library/logs）
        self.paths = WorkspacePaths(cfg.workspace_path)
        # 把重建/处理的源侧状态写入 SQLite，供 target-sync 消费
        self.recorder = SourceStateRecorder(state)

        cpu = os.cpu_count() or 4
        auto_download = max(1, min(6, cpu))
        auto_process = max(1, min(4, cpu // 2))
        auto_ffmpeg = max(1, cpu // auto_process)

        download_workers = cfg.download_workers or auto_download
        process_workers = cfg.process_workers or auto_process
        ffmpeg_threads = cfg.ffmpeg_threads or auto_ffmpeg

        first_template = cfg.presets[0].filename_template if cfg.presets else "{artist} - {name}"
        self.sync_service = SyncUseCase(
            cfg=cfg,
            api=api,
            downloader=Downloader(filename_template=first_template),
            workers=max(1, download_workers),
            dry_run=dry_run,
            state=state,
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
        )

    def link_only(self, cookie: str) -> tuple[int, int]:
        """仅创建 library 硬链接，跳过下载、解码、转码、元数据和歌词处理。

        从 SQLite 快照读取 track_id → playlist_ids 映射，
        通过 API 批量获取曲目详情生成文件名，在各 preset 目录中重建硬链接。

        返回 (linked_tracks, playlist_count)。dry-run 模式下只统计将创建的链接，不落盘。
        """
        from musicvault.domain.models import Track

        if not self.dry_run:
            self.cfg.ensure_dirs()

        # 1. 加载同步状态（自 SQLite 快照派生）
        state_map = self.sync_service.load_synced_state()
        if not state_map:
            console.print("[dim]暂无已同步曲目，无需创建链接[/dim]")
            return 0, 0

        # 2. 加载歌单索引
        playlist_index = {
            str(pl.id): {"name": pl.name, "track_count": len(pl.track_ids)}
            for pl in self.recorder.state.list_playlists()
        }
        name_to_pid: dict[str, int] = {}
        for pid_str, entry in playlist_index.items():
            name = entry.get("name") if isinstance(entry, dict) else None
            if name:
                name_to_pid[safe_filename(str(name))] = int(pid_str)

        # 3. 批量获取曲目详情（用于生成正确的链接文件名）
        all_track_ids = list(state_map.keys())
        self.api.login_with_cookie(cookie)
        track_details = self.api.get_tracks_detail(all_track_ids)

        # 4. 遍历曲目，创建缺失的 library 链接
        linked_tracks = 0
        total_links = 0
        for track_id, playlist_ids in state_map.items():
            track = track_details.get(track_id) or Track(
                id=track_id, name=str(track_id), artists=[], album="Unknown Album", raw={}
            )

            # 从 download 目录收集 canonical 文件
            audio_map: dict[str, Path] = {}
            for preset in self.cfg.presets:
                spec_key = audio_spec_key(preset.format, preset.bitrate)
                if spec_key not in audio_map:
                    src = self.sync_service.find_canonical_for_spec(track_id, spec_key)
                    if src:
                        audio_map[spec_key] = src

            if not audio_map:
                continue

            has_linked = False
            for pid in playlist_ids:
                entry = playlist_index.get(str(pid))
                dirname = safe_filename(str(entry["name"])) if entry and entry.get("name") else str(pid)
                for preset in self.cfg.presets:
                    spec_key = audio_spec_key(preset.format, preset.bitrate)
                    audio_src = audio_map.get(spec_key)
                    if not audio_src:
                        continue
                    link_stem = format_track_name(preset.filename_template, track)
                    dst_dir = self.cfg.preset_dir(preset.name) / dirname
                    if not self.dry_run:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                    audio_dst = dst_dir / f"{link_stem}{audio_src.suffix}"
                    if not audio_dst.exists():
                        if self.dry_run:
                            total_links += 1
                        else:
                            create_link(audio_src, audio_dst)
                        has_linked = True
                    if preset.write_lrc_file:
                        lrc_src = audio_src.with_name(f"{track_id}.{preset.name}.lrc")
                        if lrc_src.exists():
                            lrc_dst = dst_dir / f"{link_stem}.lrc"
                            if not lrc_dst.exists():
                                if self.dry_run:
                                    total_links += 1
                                else:
                                    create_link(lrc_src, lrc_dst)
                                has_linked = True

            if has_linked:
                linked_tracks += 1

        playlist_count = len({pid for pids in state_map.values() for pid in pids})

        if self.dry_run:
            console.print(
                f"  [bold yellow]dry-run 预览[/bold yellow]：将创建 [cyan]{total_links}[/cyan] 个硬链接（涉及 [cyan]{linked_tracks}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单）"
            )
            logger.info("dry-run 链接预览：%s 个链接，%s 首曲目", total_links, linked_tracks)
            return linked_tracks, playlist_count

        if linked_tracks:
            console.print(f"  链接完成：[cyan]{linked_tracks}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单")
        else:
            console.print("[dim]所有 library 链接均已就绪[/dim]")

        logger.info("仅链接模式完成：%s 首曲目已创建链接", linked_tracks)
        return linked_tracks, playlist_count

    def run_pipeline(self, cookie: str, command: str) -> None:
        if not self.dry_run:
            self.cfg.ensure_dirs()

        only_pull = command == "pull"
        only_process = command == "process"

        playlist_index: dict[str, dict[str, object]] = {}
        downloaded: list = []
        if not only_process:
            downloaded = self.sync_service.run_sync(
                cookie=cookie,
                playlist_ids=[pl.id for pl in self.recorder.state.list_playlists()],
            )
            playlist_index = self.sync_service.playlist_index
            if self.dry_run and not only_pull:
                n_new = len(self.sync_service.plan.get("with_url") or [])
                console.print(f"  [dim]随后将进入后处理：新下载的 {n_new} 首曲目（转码/元数据/歌词/硬链接）[/dim]")

        # sync 的 dry-run 不跑 process 阶段（新下载文件尚未落地），process --dry-run 仍执行本地扫描预览
        if not only_pull and (not self.dry_run or only_process):
            self.process_service.run_process(
                downloaded=downloaded,
                force=self.cfg.force,
                playlist_index=playlist_index,
            )

        if not self.dry_run:
            # 清理未分类 中无索引归属的孤立文件（上一版 bug 的残留，以及后续边界情况）
            self._cleanup_uncategorized_orphans()
            ok("完成")
        else:
            console.print("  [bold yellow]dry-run 结束：未下载、未修改任何文件[/bold yellow]")

    def _cleanup_uncategorized_orphans(self) -> None:
        """清理 library/*/未分类 下无索引归属的硬链接。"""
        synced = self.sync_service.load_synced_state()
        valid_ids = set(synced.keys())
        if not valid_ids:
            return

        # 构建 canonical 文件的 inode → track_id 映射
        inode_to_tid: dict[tuple[int, int], int] = {}
        media_root = self.paths.media_store
        if media_root.is_dir():
            for track_dir in media_root.iterdir():
                if not track_dir.is_dir() or not track_dir.name.isdigit():
                    continue
                audio_dir = track_dir / "audio"
                if not audio_dir.is_dir():
                    continue
                for f in audio_dir.iterdir():
                    if not f.is_file():
                        continue
                    try:
                        st = f.stat()
                        inode_to_tid[(st.st_dev, st.st_ino)] = int(track_dir.name)
                    except OSError:
                        continue

        if not inode_to_tid:
            return

        removed = 0
        for preset in self.cfg.presets:
            uncat = self.cfg.preset_dir(preset.name) / self.cfg.default_playlist_name
            if not uncat.is_dir():
                continue
            for f in list(uncat.iterdir()):
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                    tid = inode_to_tid.get((st.st_dev, st.st_ino))
                except OSError:
                    continue
                if tid is not None and tid not in valid_ids:
                    f.unlink()
                    removed += 1
                    logger.info("清理无归属的未分类文件：%s", f.name)
            # 清空后删除目录
            try:
                if not any(uncat.iterdir()):
                    uncat.rmdir()
            except OSError:
                pass

        if removed:
            logger.info("未分类清理完成：已删除 %s 个孤立文件", removed)

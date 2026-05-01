from __future__ import annotations

import logging
import os
import time

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.services.process_service import ProcessService
from musicvault.services.sync_service import SyncService
from musicvault.shared.tui_progress import console, ok
from musicvault.shared.utils import create_link, load_json, safe_filename, save_json, workspace_rel_path

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
            downloader=Downloader(filename_template=cfg.filename_lossless),
            workers=max(1, download_workers),
        )
        self.process_service = ProcessService(
            cfg=cfg,
            api=api,
            decryptor=Decryptor(),
            organizer=Organizer(
                ffmpeg_threads=max(1, ffmpeg_threads),
                lossy_bitrate=cfg.lossy_bitrate,
                lossy_format=cfg.lossy_format,
                ffmpeg_path=cfg.ffmpeg_path,
            ),
            metadata=MetadataWriter(
                embed_cover=cfg.embed_cover,
                embed_lyrics=cfg.lyrics_embed_in_metadata,
                cover_timeout=cfg.network_cover_timeout,
                cover_max_size=cfg.cover_max_size,
                metadata_fields=cfg.metadata_fields,
            ),
            workers=max(1, process_workers),
        )

    def rebuild_index(self) -> tuple[int, int]:
        """通过 downloads/ 和 library/ 目录重建 synced_tracks.json 和 processed_files.json。

        返回 (track_count, playlist_count)。
        """
        self.cfg.ensure_dirs()
        downloads = self.cfg.downloads_dir

        # 1. 扫描 downloads/ 中的 canonical 文件（跳过 cache/ 子目录）
        track_ids: set[int] = set()
        for f in downloads.iterdir():
            if not f.is_file():
                continue
            if f.stem.isdigit() and f.suffix.lower() in (".flac", ".mp3"):
                track_ids.add(int(f.stem))

        if not track_ids:
            console.print("[dim]downloads 目录中未找到任何 canonical 文件[/dim]")
            return 0, 0

        # 2. 构建 playlist 目录名 → playlist_id 的反向映射
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        dirname_to_pid: dict[str, int] = {}
        for pid_str, entry in playlist_index.items():
            name = entry.get("name") if isinstance(entry, dict) else None
            if name:
                dirname_to_pid[safe_filename(str(name))] = int(pid_str)

        # 3. 构建 canonical 文件的 inode 映射，用于硬链接匹配
        inode_to_tid: dict[tuple[int, int], int] = {}
        for tid in track_ids:
            for ext in (".flac", ".mp3"):
                canon = downloads / f"{tid}{ext}"
                try:
                    st = canon.stat()
                    inode_to_tid[(st.st_dev, st.st_ino)] = tid
                except OSError:
                    continue

        # 4. 扫描 library 目录，通过 inode 匹配硬链接 → track_id
        track_playlists: dict[int, set[int]] = {tid: set() for tid in track_ids}
        for parent, exts in ((self.cfg.lossless_dir, (".flac", ".mp3")), (self.cfg.lossy_dir, (".mp3",))):
            if not parent.is_dir():
                continue
            for pl_dir in parent.iterdir():
                if not pl_dir.is_dir():
                    continue
                pid = dirname_to_pid.get(pl_dir.name)
                if pid is None:
                    continue
                for f in pl_dir.iterdir():
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in exts:
                        continue
                    try:
                        st = f.stat()
                        tid = inode_to_tid.get((st.st_dev, st.st_ino))
                    except OSError:
                        continue
                    if tid is not None:
                        track_playlists.setdefault(tid, set()).add(pid)

        # 5. 重建 synced_tracks.json
        synced: dict[int, list[int]] = {}
        for tid in sorted(track_ids):
            synced[tid] = sorted(track_playlists.get(tid, set()))
        SyncService._save_synced_state(self.cfg, synced)

        # 6. 重建 processed_files.json
        processed: dict[str, dict[str, object]] = {}
        for tid in sorted(track_ids):
            flac_path = downloads / f"{tid}.flac"
            mp3_path = downloads / f"{tid}.mp3"
            lrc_path = downloads / f"{tid}.lrc"
            entry: dict[str, object] = {}
            if flac_path.exists():
                entry["flac"] = workspace_rel_path(flac_path, self.cfg.workspace_path)
            if mp3_path.exists():
                entry["mp3"] = workspace_rel_path(mp3_path, self.cfg.workspace_path)
            if lrc_path.exists():
                entry["lrc"] = workspace_rel_path(lrc_path, self.cfg.workspace_path)
            if entry:
                entry["updated_at"] = int(time.time())
                processed[str(tid)] = entry
        save_json(self.cfg.processed_state_file, processed)

        orphaned = sum(1 for tid in track_ids if not track_playlists.get(tid))
        playlist_count = len({pid for pids in track_playlists.values() for pid in pids})

        console.print(f"  重建完成：[cyan]{len(track_ids)}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单")
        if orphaned:
            console.print(f"  [dim]（其中 {orphaned} 首未关联到任何歌单）[/dim]")

        logger.info(
            "索引重建完成：%s 首曲目，%s 个歌单，synced_tracks.json + processed_files.json 已更新",
            len(track_ids),
            playlist_count,
        )
        return len(track_ids), playlist_count

    def link_only(self, cookie: str) -> tuple[int, int]:
        """仅创建 library 硬链接，跳过下载、解码、转码、元数据和歌词处理。

        从 synced_tracks.json 读取 track_id → playlist_ids 映射，
        通过 API 批量获取曲目详情生成文件名，在 library 中重建硬链接。

        返回 (linked_tracks, playlist_count)。
        """
        from musicvault.core.models import Track

        self.cfg.ensure_dirs()

        # 1. 加载同步状态
        state_map = SyncService._load_synced_state(self.cfg)
        if not state_map:
            console.print("[dim]synced_tracks.json 为空，无需创建链接[/dim]")
            return 0, 0

        # 2. 加载歌单索引
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
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
            flac_src = self.process_service._find_lossless_canonical(track_id)
            mp3_src = self.cfg.downloads_dir / f"{track_id}.mp3"
            if not flac_src or not mp3_src.exists():
                continue

            lrc_src = mp3_src.with_suffix(".lrc")
            has_linked = False
            for pid in playlist_ids:
                entry = playlist_index.get(str(pid))
                dirname = safe_filename(str(entry["name"])) if entry and entry.get("name") else str(pid)
                ll_dst = (
                    self.cfg.lossless_dir / dirname / self.process_service._lossless_link_name(track, flac_src.suffix)
                )
                ly_dst = self.cfg.lossy_dir / dirname / self.process_service._lossy_link_name(track)
                if not ll_dst.exists():
                    create_link(flac_src, ll_dst)
                    has_linked = True
                if not ly_dst.exists():
                    create_link(mp3_src, ly_dst)
                    has_linked = True
                if lrc_src.exists():
                    lrc_dst = ly_dst.with_suffix(".lrc")
                    if not lrc_dst.exists():
                        create_link(lrc_src, lrc_dst)
                        has_linked = True

            if has_linked:
                linked_tracks += 1
                total_links += 1

        playlist_count = len({pid for pids in state_map.values() for pid in pids})

        if linked_tracks:
            console.print(f"  链接完成：[cyan]{linked_tracks}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单")
        else:
            console.print("[dim]所有 library 链接均已就绪[/dim]")

        logger.info("仅链接模式完成：%s 首曲目已创建链接", linked_tracks)
        return linked_tracks, playlist_count

    def run_pipeline(self, cookie: str, command: str) -> None:
        self.cfg.ensure_dirs()

        only_pull = command == "pull"
        only_process = command == "process"

        playlist_index: dict[str, dict[str, object]] = {}
        downloaded: list = []
        if not only_process:
            downloaded = self.sync_service.run_sync(cookie=cookie, playlist_ids=self.cfg.get_playlist_ids())
            playlist_index = self.sync_service.playlist_index

        if not only_pull:
            self.process_service.run_process(
                downloaded=downloaded,
                include_translation=self.cfg.include_translation,
                translation_format=self.cfg.translation_format,
                force=self.cfg.force,
                playlist_index=playlist_index,
            )

        ok("完成")

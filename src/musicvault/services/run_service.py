from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.downloader import Downloader
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.application.source_state import SourceStateRecorder, build_audio_asset_from_file
from musicvault.core.config import Config
from musicvault.core.models import Track
from musicvault.core.preset import audio_spec_key, compute_preset_hash
from musicvault.domain.models import Playlist
from musicvault.ports.state import StateRepository
from musicvault.services.process_service import ProcessService
from musicvault.services.sync_service import SyncService
from musicvault.shared.tui_progress import console, ok
from musicvault.shared.utils import (
    create_link,
    format_track_name,
    load_json,
    safe_filename,
    save_json,
    workspace_rel_path,
)

logger = logging.getLogger(__name__)


class RunService:
    def __init__(
        self,
        cfg: Config,
        api: NeteaseClient,
        dry_run: bool = False,
        state: StateRepository | None = None,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.dry_run = dry_run
        # 可选：把重建/处理的源侧状态写入新 SQLite，供 target-sync 消费
        self.recorder = SourceStateRecorder(state) if state is not None else None

        cpu = os.cpu_count() or 4
        auto_download = max(1, min(6, cpu))
        auto_process = max(1, min(4, cpu // 2))
        auto_ffmpeg = max(1, cpu // auto_process)

        download_workers = cfg.download_workers or auto_download
        process_workers = cfg.process_workers or auto_process
        ffmpeg_threads = cfg.ffmpeg_threads or auto_ffmpeg

        first_template = cfg.presets[0].filename_template if cfg.presets else "{artist} - {name}"
        self.sync_service = SyncService(
            cfg=cfg,
            api=api,
            downloader=Downloader(filename_template=first_template),
            workers=max(1, download_workers),
            dry_run=dry_run,
            state=state,
        )
        self.process_service = ProcessService(
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

    def rebuild_index(self) -> tuple[int, int]:
        """通过 downloads/ 和 library/ 目录重建 synced_tracks.json 和 processed_files.json。

        返回 (track_count, playlist_count)。
        """
        self.cfg.ensure_dirs()
        downloads = self.cfg.downloads_dir

        # 1. 扫描 downloads/ 中的 canonical 文件（跳过 cache/ 子目录）
        audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
        track_ids: set[int] = set()
        for f in downloads.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in audio_exts:
                continue
            stem = f.stem.split("_")[0]  # strip bitrate suffix (e.g., 12345_192k → 12345)
            if stem.isdigit():
                track_ids.add(int(stem))

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
            for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus"):
                canon = downloads / f"{tid}{ext}"
                try:
                    st = canon.stat()
                    inode_to_tid[(st.st_dev, st.st_ino)] = tid
                except OSError:
                    continue
                # Also check bitrate-suffixed variants
                for f in downloads.iterdir():
                    if not f.is_file():
                        continue
                    if f.stem.startswith(f"{tid}_") and f.suffix.lower() == ext:
                        try:
                            st2 = f.stat()
                            inode_to_tid[(st2.st_dev, st2.st_ino)] = tid
                        except OSError:
                            continue

        # 4. 扫描 library 目录（按 preset），通过 inode 匹配硬链接 → track_id
        track_playlists: dict[int, set[int]] = {tid: set() for tid in track_ids}
        for preset in self.cfg.presets:
            parent = self.cfg.preset_dir(preset.name)
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

        # 6. 重建 processed_files.json（新版 audios 格式）
        processed: dict[str, dict[str, object]] = {}
        for tid in sorted(track_ids):
            audios: dict[str, str] = {}
            # Scan downloads for all canonical files matching this track_id
            for f in downloads.iterdir():
                if not f.is_file() or f.suffix.lower() not in audio_exts:
                    continue
                stem, tid_str = f.stem, str(tid)
                if stem == tid_str or stem.startswith(f"{tid_str}_"):
                    spec_key = _guess_spec_from_filename(f.name)
                    if spec_key:
                        audios[spec_key] = workspace_rel_path(f, self.cfg.workspace_path)
            if audios:
                processed[str(tid)] = {
                    "audios": audios,
                    "preset_hash": compute_preset_hash(self.cfg.presets),
                    "updated_at": int(time.time()),
                }
        save_json(self.cfg.processed_state_file, processed)

        if self.recorder is not None:
            self._record_rebuilt_state(track_ids, track_playlists, playlist_index, processed)

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

    def _record_rebuilt_state(
        self,
        track_ids: set[int],
        track_playlists: dict[int, set[int]],
        playlist_index: dict[str, dict[str, object]],
        processed: dict[str, dict[str, object]],
    ) -> None:
        """把磁盘重建的源侧状态写入 SQLite，供 target-sync 消费。"""
        assert self.recorder is not None
        # 已存在的曲目保留真实元数据，占位记录只补缺失项，避免覆盖 sync 写入的信息。
        known_ids = {track.id for track in self.recorder.state.create_snapshot().tracks}
        tracks = [
            Track(id=tid, name=str(tid), artists=[], album="Unknown Album", raw={})
            for tid in sorted(track_ids)
            if tid not in known_ids
        ]
        playlists: list[Playlist] = []
        for pid_str, entry in playlist_index.items():
            if not pid_str.lstrip("-").isdigit() or not isinstance(entry, dict):
                continue
            pid = int(pid_str)
            name = str(entry.get("name") or pid)
            member_ids = tuple(sorted(tid for tid, pids in track_playlists.items() if pid in pids))
            # 跳过空歌单，避免清空 sync 已录的 playlist_tracks 关系。
            if member_ids:
                playlists.append(Playlist(pid, name, member_ids))
        assets = [
            build_audio_asset_from_file(
                int(tid_str), spec_key, _abs_path(self.cfg.workspace_path, rel), source="pipeline:reindex"
            )
            for tid_str, entry in processed.items()
            for spec_key, rel in (entry.get("audios") or {}).items()
        ]
        managed = [song_id for song_id in self.cfg.get_song_ids() if song_id in track_ids]
        self.recorder.record_source_state(tracks, playlists, managed, assets)

    def link_only(self, cookie: str) -> tuple[int, int]:
        """仅创建 library 硬链接，跳过下载、解码、转码、元数据和歌词处理。

        从 synced_tracks.json 读取 track_id → playlist_ids 映射，
        通过 API 批量获取曲目详情生成文件名，在各 preset 目录中重建硬链接。

        返回 (linked_tracks, playlist_count)。dry-run 模式下只统计将创建的链接，不落盘。
        """
        from musicvault.core.models import Track

        if not self.dry_run:
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

            # 从 download 目录收集 canonical 文件
            audio_map: dict[str, Path] = {}
            for preset in self.cfg.presets:
                spec_key = audio_spec_key(preset.format, preset.bitrate)
                if spec_key not in audio_map:
                    src = self.sync_service._find_canonical_for_spec(track_id, spec_key)
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
            downloaded = self.sync_service.run_sync(cookie=cookie, playlist_ids=self.cfg.get_playlist_ids())
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
        synced = SyncService._load_synced_state(self.cfg)
        valid_ids = set(synced.keys())
        if not valid_ids:
            return

        # 构建 canonical 文件的 inode → track_id 映射
        inode_to_tid: dict[tuple[int, int], int] = {}
        audio_exts = (".flac", ".mp3", ".m4a", ".ogg", ".opus")
        for ext in audio_exts:
            for f in self.cfg.downloads_dir.glob(f"*{ext}"):
                if not f.is_file():
                    continue
                stem = f.stem.split("_")[0]
                if not stem.isdigit():
                    continue
                try:
                    st = f.stat()
                    inode_to_tid[(st.st_dev, st.st_ino)] = int(stem)
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


def _guess_spec_from_filename(filename: str) -> str | None:
    """从 canonical 文件名猜测 spec_key。

    例如 '12345.flac' → 'FLAC', '12345_192k.mp3' → 'MP3-192k'。
    """
    from pathlib import Path

    p = Path(filename)
    suffix = p.suffix.lower()
    fmt_map = {".flac": "flac", ".mp3": "mp3", ".m4a": "aac", ".ogg": "ogg", ".opus": "opus"}
    fmt = fmt_map.get(suffix)
    if fmt is None:
        return None
    stem = p.stem
    if "_" in stem:
        parts = stem.split("_", 1)
        if parts[1].rstrip("k").isdigit():
            return audio_spec_key(fmt, parts[1])
    return audio_spec_key(fmt, None)


def _abs_path(workspace: Path, stored: str) -> Path:
    """把 processed_files.json 中保存的路径还原为绝对路径。

    保存的是 workspace 相对路径；跨盘符时保存的就是绝对路径。
    """
    path = Path(stored)
    return path if path.is_absolute() else (workspace / path).resolve()

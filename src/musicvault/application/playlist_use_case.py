"""歌单与单曲管理用例：cli 管理命令（add/remove/list）经此访问状态库与文件清理。"""

from __future__ import annotations

import logging
import shutil

from musicvault.core.config import Config
from musicvault.domain.models import Playlist, Track
from musicvault.ports.state import StateRepository
from musicvault.shared.utils import safe_filename

logger = logging.getLogger(__name__)

# canonical 文件扩展名（旧 downloads 布局，C 阶段迁移前保持不变）
_CANONICAL_EXTS = (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".lrc")
_SONG_REMOVE_EXTS = (".flac", ".mp3", ".lrc")


class PlaylistUseCase:
    """歌单与单曲管理的应用用例。"""

    def __init__(self, cfg: Config, state: StateRepository) -> None:
        self.cfg = cfg
        self.state = state

    # ------------------------------------------------------------------
    # 歌单
    # ------------------------------------------------------------------

    def list_playlists(self) -> list[Playlist]:
        return self.state.list_playlists()

    def get_playlist(self, playlist_id: int) -> Playlist | None:
        return self.state.get_playlist(playlist_id)

    def has_playlist(self, playlist_id: int) -> bool:
        return self.state.get_playlist(playlist_id) is not None

    def add_playlist(self, playlist_id: int, name: str = "") -> None:
        """登记歌单；曲目关系由 sync 拉取后填充。"""
        self.state.upsert_playlist(Playlist(playlist_id, name, ()))

    def remove_playlist(self, playlist_id: int) -> None:
        """移除歌单：删除 library 中的歌单目录（硬链接）与无归属 canonical 文件，并清理状态库。"""
        playlist = self.state.get_playlist(playlist_id)
        if playlist is not None:
            name = playlist.name
        else:
            name = ""
        dir_name = safe_filename(str(name)) if name else safe_filename(str(playlist_id))

        # 删除 library 目录（仅含硬链接，直接 rmtree）
        deleted_dirs = 0
        for preset in self.cfg.presets:
            target = self.cfg.preset_dir(preset.name) / dir_name
            if target.is_dir():
                shutil.rmtree(target)
                deleted_dirs += 1

        # 找出不再属于任何歌单的曲目（删除本歌单后）
        ids_to_remove: set[int] = set()
        if playlist is not None:
            self.state.remove_playlist(playlist_id)
            snapshot = self.state.create_snapshot()
            for track_id in playlist.track_ids:
                still_member = any(
                    other.track_ids and track_id in other.track_ids
                    for other in snapshot.playlists
                    if other.id != playlist_id
                )
                if not still_member:
                    ids_to_remove.add(track_id)

        # 记录 canonical 文件的 inode（删除前），用于后续匹配 未分类 中的硬链接
        canonical_inodes: set[tuple[int, int]] = set()
        for track_id in ids_to_remove:
            for ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus"):
                p = self.cfg.downloads_dir / f"{track_id}{ext}"
                if p.exists():
                    try:
                        st = p.stat()
                        canonical_inodes.add((st.st_dev, st.st_ino))
                    except OSError:
                        pass

        # 删除无歌单归属的 canonical 文件（覆盖所有格式及 bitrate 后缀变体）
        for track_id in ids_to_remove:
            for ext in _CANONICAL_EXTS:
                (self.cfg.downloads_dir / f"{track_id}{ext}").unlink(missing_ok=True)
            if self.cfg.downloads_dir.is_dir():
                for f in list(self.cfg.downloads_dir.iterdir()):
                    if f.is_file() and f.stem.startswith(f"{track_id}_"):
                        f.unlink(missing_ok=True)
            self.state.remove_track(track_id)

        # 删除 未分类 中对应的硬链接（它们指向同一 inode，unlink 不会自动消失）
        if canonical_inodes:
            for preset in self.cfg.presets:
                uncat_dir = self.cfg.preset_dir(preset.name) / self.cfg.default_playlist_name
                if not uncat_dir.is_dir():
                    continue
                for f in list(uncat_dir.iterdir()):
                    if not f.is_file():
                        continue
                    try:
                        st = f.stat()
                        if (st.st_dev, st.st_ino) in canonical_inodes:
                            f.unlink()
                    except OSError:
                        continue
                # 删除空的 未分类 目录
                try:
                    if not any(uncat_dir.iterdir()):
                        uncat_dir.rmdir()
                except OSError:
                    pass

        if deleted_dirs:
            logger.info("已删除 [bold]%s[/bold] 的音乐文件（%s 个目录）", dir_name, deleted_dirs)
        elif name:
            logger.info("未找到 %s 的音乐目录，已跳过文件删除", dir_name)

    # ------------------------------------------------------------------
    # 单曲
    # ------------------------------------------------------------------

    def list_songs(self) -> list[int]:
        return self.state.list_managed_songs()

    def has_song(self, song_id: int) -> bool:
        return self.state.has_managed_song(song_id)

    def add_song(self, song_id: int) -> None:
        """登记单独管理的单曲；track 不存在时先写占位记录（managed_songs 外键约束）。"""
        if self.state.has_managed_song(song_id):
            return
        if self.state.get_track(song_id) is None:
            # 占位曲目由 sync 获取真实元数据后覆盖（与 reindex 的占位模式一致）
            self.state.upsert_track(Track(id=song_id, name=str(song_id), artists=[], album="", raw={}))
        self.state.add_managed_song(song_id)

    def remove_song(self, song_id: int) -> None:
        """移除单曲管理登记并删除其 canonical 文件。"""
        self.state.remove_managed_song(song_id)
        for ext in _SONG_REMOVE_EXTS:
            (self.cfg.downloads_dir / f"{song_id}{ext}").unlink(missing_ok=True)

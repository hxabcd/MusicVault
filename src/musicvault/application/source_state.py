from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from musicvault.domain.models import Track
from musicvault.domain.models import MediaAsset, Playlist
from musicvault.ports.state import StateRepository
from musicvault.shared.utils import sha256_file


class SourceStateRecorder:
    """把旧流水线产生的源侧状态持久化到 StateRepository（SQLite）。

    这是旧 `sync`/`process` 接入新状态接缝的第一步：结果写入 SQLite，
    供 `target-sync` 通过 SourceSnapshot 消费，而不再只落在旧 JSON 状态文件中。
    """

    def __init__(self, state: StateRepository) -> None:
        self.state = state

    def record_source_state(
        self,
        tracks: Iterable[Track],
        playlists: Iterable[Playlist] = (),
        managed_songs: Iterable[int] = (),
        media_assets: Iterable[MediaAsset] = (),
    ) -> None:
        """在单个事务内写入曲目、歌单关系、单独管理单曲与媒体资产。"""
        with self.state.transaction() as connection:
            for track in tracks:
                self.state.upsert_track(track, connection=connection)
            for playlist in playlists:
                self.state.upsert_playlist(playlist, connection=connection)
            for song_id in managed_songs:
                self.state.add_managed_song(int(song_id), connection=connection)
            for asset in media_assets:
                self.state.upsert_media_asset(asset, connection=connection)


def build_audio_asset_from_file(
    track_id: int,
    spec: str,
    path: Path,
    *,
    source: str = "pipeline:downloads",
) -> MediaAsset:
    """读取 canonical 音频文件并构造媒体资产记录（路径、大小、SHA-256、来源、更新时间）。"""
    return MediaAsset(
        track_id=track_id,
        asset_type="audio",
        spec=spec,
        path=Path(path),
        size=path.stat().st_size,
        sha256=sha256_file(path),
        source=source,
        updated_at=time.time(),
    )

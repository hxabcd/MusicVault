from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteProcessStateRepository, SQLiteSourceStateRepository, SQLiteState
from musicvault.application.source_state import SourceStateRecorder
from musicvault.core.config import Config
from musicvault.domain.models import Track
from musicvault.domain.models import Playlist
from musicvault.application.sync_use_case import SyncUseCase

# ---------------------------------------------------------------------------
# 同步状态加载（SQLite 快照派生，替代旧 synced_tracks.json 格式解析）
# ---------------------------------------------------------------------------


def _seed_state(cfg: Config, state_map: dict[int, list[int]]) -> None:
    """把 {track_id: [playlist_ids]} 写入 SQLite，供 load_synced_state 派生。"""
    repo = SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file))
    playlists: dict[int, Playlist] = {}
    tracks = [Track(id=tid, name=f"曲目 {tid}", artists=[], album="专辑", raw={}) for tid in state_map]
    for _, pids in state_map.items():
        for pid in pids:
            playlists.setdefault(pid, Playlist(pid, f"歌单{pid}", ()))
    for pid, playlist in playlists.items():
        object.__setattr__(
            playlist,
            "track_ids",
            tuple(tid for tid, pids in state_map.items() if pid in pids),
        )
    SourceStateRecorder(repo).record_source_state(tracks, playlists.values())


class TestLoadSyncedState:
    def test_derives_playlist_assignments(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        _seed_state(cfg, {123: [10, 20], 456: [10]})

        svc = SyncUseCase(
            cfg,
            MagicMock(),
            MagicMock(),
            workers=1,
            state=SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file)),
            process_state=SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file)),
        )
        result = svc.load_synced_state()
        assert result == {123: [10, 20], 456: [10]}

    def test_isolated_song_has_empty_playlists(self, tmp_path: Path) -> None:
        """无歌单归属的单独管理单曲保留空列表。"""
        cfg = _make_config(tmp_path)
        _seed_state(cfg, {789: []})

        svc = SyncUseCase(
            cfg,
            MagicMock(),
            MagicMock(),
            workers=1,
            state=SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file)),
            process_state=SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file)),
        )
        result = svc.load_synced_state()
        assert result == {789: []}

    def test_empty_snapshot_returns_empty(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file))

        svc = SyncUseCase(
            cfg,
            MagicMock(),
            MagicMock(),
            workers=1,
            state=SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file)),
            process_state=SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file)),
        )
        result = svc.load_synced_state()
        assert result == {}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> Config:
    cfg = MagicMock(spec=Config)
    cfg.workspace_path = tmp_path
    cfg.state_db_file = tmp_path / "state.db"
    cfg.media_store_dir = tmp_path / "media_store"
    cfg.library_dir = tmp_path / "library"
    return cfg

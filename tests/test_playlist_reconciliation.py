from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.source_state import SourceStateRecorder
from musicvault.core.config import Config
from musicvault.domain.models import Track
from musicvault.core.preset import Preset
from musicvault.domain.models import Playlist
from musicvault.services.sync_service import SyncService

# ---------------------------------------------------------------------------
# 同步状态加载（SQLite 快照派生，替代旧 synced_tracks.json 格式解析）
# ---------------------------------------------------------------------------


def _seed_state(cfg: Config, state_map: dict[int, list[int]]) -> None:
    """把 {track_id: [playlist_ids]} 写入 SQLite，供 _load_synced_state 派生。"""
    repo = SQLiteStateRepository(SQLiteState(cfg.state_db_file))
    playlists: dict[int, Playlist] = {}
    tracks = [Track(id=tid, name=f"曲目 {tid}", artists=[], album="专辑", raw={}) for tid in state_map]
    for tid, pids in state_map.items():
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

        svc = SyncService(
            cfg, MagicMock(), MagicMock(), workers=1, state=SQLiteStateRepository(SQLiteState(cfg.state_db_file))
        )
        result = svc._load_synced_state()
        assert result == {123: [10, 20], 456: [10]}

    def test_isolated_song_has_empty_playlists(self, tmp_path: Path) -> None:
        """无歌单归属的单独管理单曲保留空列表。"""
        cfg = _make_config(tmp_path)
        _seed_state(cfg, {789: []})

        svc = SyncService(
            cfg, MagicMock(), MagicMock(), workers=1, state=SQLiteStateRepository(SQLiteState(cfg.state_db_file))
        )
        result = svc._load_synced_state()
        assert result == {789: []}

    def test_empty_snapshot_returns_empty(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        SQLiteStateRepository(SQLiteState(cfg.state_db_file))

        svc = SyncService(
            cfg, MagicMock(), MagicMock(), workers=1, state=SQLiteStateRepository(SQLiteState(cfg.state_db_file))
        )
        result = svc._load_synced_state()
        assert result == {}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> Config:
    from musicvault.core.preset import Preset

    cfg = MagicMock(spec=Config)
    cfg.workspace_path = tmp_path
    cfg.state_db_file = tmp_path / "state.db"
    cfg.state_dir = tmp_path / "state"
    cfg.downloads_dir = tmp_path / "downloads"
    cfg.library_dir = tmp_path / "library"
    cfg.preset_dir = lambda name: tmp_path / "library" / name
    cfg.presets = [
        Preset(
            name="archive",
            quality="hires",
            format="flac",
            filename_template="{artist} - {name}",
            embed_cover=True,
            embed_lyrics=True,
            use_karaoke=True,
            include_translation=True,
            translation_format="separate",
            write_lrc_file=False,
        ),
        Preset(
            name="portable",
            quality="hires",
            format="mp3",
            bitrate="192k",
            filename_template="{alias} {name} - {artist}",
            embed_cover=False,
            embed_lyrics=False,
            use_karaoke=False,
            include_translation=True,
            translation_format="inline",
            write_lrc_file=True,
            lrc_encodings=("utf-8", "gb18030"),
        ),
    ]
    return cfg


def _make_playlist_index() -> dict[str, dict[str, object]]:
    return {
        "10": {"name": "歌单A", "track_count": 10},
        "20": {"name": "歌单B", "track_count": 5},
    }


def _make_track(track_id: int) -> Track:
    return Track(
        id=track_id,
        name="Test Song",
        artists=["Test Artist"],
        album="Test Album",
        cover_url=None,
        raw={},
    )


# ---------------------------------------------------------------------------
# 协调测试（新架构：纯链接操作）
# ---------------------------------------------------------------------------


class TestReconcileNoChange:
    def _svc(self, cfg: Config) -> SyncService:
        return SyncService(
            cfg, MagicMock(), MagicMock(), workers=1, state=SQLiteStateRepository(SQLiteState(cfg.state_db_file))
        )

    def test_empty_old_state(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        _seed_state(cfg, {})

        svc = self._svc(cfg)
        svc._reconcile_playlist_assignments({123: [10]}, _make_playlist_index(), {})
        # 不应抛异常

    def test_assignments_unchanged(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        _seed_state(cfg, {123: [10, 20]})

        svc = self._svc(cfg)
        svc._reconcile_playlist_assignments({123: [10, 20]}, _make_playlist_index(), {})

        result = svc._load_synced_state()
        assert result[123] == [10, 20]

    def test_no_track_in_all_tracks(self, tmp_path: Path) -> None:
        """如果 track 不在 all_tracks 中，应静默跳过。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        _seed_state(cfg, {123: [10]})

        svc = self._svc(cfg)
        svc._reconcile_playlist_assignments({123: [20]}, _make_playlist_index(), {})

        # 无 track 信息，状态保持不变（歌单分配以 run_sync 末尾 recorder 为准）
        result = svc._load_synced_state()
        assert result[123] == [10]


class TestReconcilePlaylistChanged:
    def _svc(self, cfg: Config) -> SyncService:
        return SyncService(
            cfg, MagicMock(), MagicMock(), workers=1, state=SQLiteStateRepository(SQLiteState(cfg.state_db_file))
        )

    def test_add_playlist_creates_link(self, tmp_path: Path) -> None:
        """曲目新增到歌单B → 在B目录创建硬链接。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        _seed_state(cfg, {123: [10]})

        track = _make_track(123)
        # 创建 canonical 源文件
        flac_src = cfg.downloads_dir / "123.flac"
        mp3_src = cfg.downloads_dir / "123.mp3"
        lrc_src = cfg.downloads_dir / "123.portable.lrc"
        flac_src.write_text("flac")
        mp3_src.write_text("mp3")
        lrc_src.write_text("lrc")

        svc = self._svc(cfg)
        svc._reconcile_playlist_assignments({123: [10, 20]}, _make_playlist_index(), {123: track})

        # B 目录中应有链接
        assert (cfg.preset_dir("archive") / "歌单B" / "Test Artist - Test Song.flac").exists()
        assert (cfg.preset_dir("portable") / "歌单B" / "Test Song - Test Artist.mp3").exists()
        assert (cfg.preset_dir("portable") / "歌单B" / "Test Song - Test Artist.lrc").exists()

    def test_remove_playlist_deletes_link(self, tmp_path: Path) -> None:
        """曲目从歌单B移除 → B目录中的链接被删除。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        _seed_state(cfg, {123: [10, 20]})

        track = _make_track(123)
        # 创建 canonical 源文件
        (cfg.downloads_dir / "123.flac").write_text("flac")
        (cfg.downloads_dir / "123.mp3").write_text("mp3")

        # 在 B 目录创建现有链接
        b_arc = cfg.preset_dir("archive") / "歌单B" / "Test Artist - Test Song.flac"
        b_port = cfg.preset_dir("portable") / "歌单B" / "Test Song - Test Artist.mp3"
        b_arc.parent.mkdir(parents=True)
        b_port.parent.mkdir(parents=True)
        b_arc.write_text("flac")
        b_port.write_text("mp3")

        svc = self._svc(cfg)
        svc._reconcile_playlist_assignments({123: [10]}, _make_playlist_index(), {123: track})

        # B 目录中的链接应被删除
        assert not b_arc.exists()
        assert not b_port.exists()

    def test_canonical_missing_skips(self, tmp_path: Path) -> None:
        """canonical 源文件不存在时静默跳过。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        _seed_state(cfg, {123: [10]})

        track = _make_track(123)
        # 不创建 canonical 文件

        svc = self._svc(cfg)
        svc._reconcile_playlist_assignments({123: [10, 20]}, _make_playlist_index(), {123: track})

        # 不应创建任何链接（canonical 缺失）
        b_dir = cfg.preset_dir("archive") / "歌单B"
        assert not b_dir.exists() or not any(b_dir.iterdir())


# ---------------------------------------------------------------------------
# 链接文件名生成
# ---------------------------------------------------------------------------


class TestLinkNames:
    def _svc(self, cfg: Config) -> SyncService:
        # 仅测链接文件名，状态接口用 stub 占位
        return SyncService(cfg, MagicMock(), MagicMock(), workers=1, state=MagicMock())

    def test_link_name_archive(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.presets = []
        svc = self._svc(cfg)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", cover_url=None, raw={})
        preset = Preset(name="archive", format="flac", filename_template="{artist} - {name}")
        name = svc._link_name(track, preset, ".flac")
        assert name == "Artist - Song.flac"

    def test_link_name_portable_no_alias(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.presets = []
        svc = self._svc(cfg)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", cover_url=None, raw={})
        preset = Preset(
            name="portable", format="mp3", bitrate="192k",
            filename_template="{alias} {name} - {artist}",
        )
        name = svc._link_name(track, preset, ".mp3")
        assert name == "Song - Artist.mp3"

    def test_link_name_portable_with_alias(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.presets = []
        svc = self._svc(cfg)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", aliases=["Alias"], cover_url=None, raw={})
        preset = Preset(
            name="portable", format="mp3", bitrate="192k",
            filename_template="{alias} {name} - {artist}",
        )
        name = svc._link_name(track, preset, ".mp3")
        assert name == "Alias Song - Artist.mp3"

    def test_link_name_portable_no_alias_again(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.presets = []
        svc = self._svc(cfg)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", cover_url=None, raw={})
        preset = Preset(
            name="portable", format="mp3", bitrate="192k",
            filename_template="{alias} {name} - {artist}",
        )
        name = svc._link_name(track, preset, ".mp3")
        assert name == "Song - Artist.mp3"

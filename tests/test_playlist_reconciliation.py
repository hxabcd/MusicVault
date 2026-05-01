from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from musicvault.core.config import Config
from musicvault.core.models import Track
from musicvault.services.sync_service import SyncService


# ---------------------------------------------------------------------------
# synced_tracks.json 格式加载/保存
# ---------------------------------------------------------------------------


class TestLoadSyncedState:
    def test_old_format_flat_list(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(json.dumps({"ids": [123, 456, 789]}), encoding="utf-8")

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            result = SyncService._load_synced_state(cfg)
            assert result == {123: [], 456: [], 789: []}

    def test_new_format_dict(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(json.dumps({"ids": {"123": [10, 20], "456": [10]}}), encoding="utf-8")

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            result = SyncService._load_synced_state(cfg)
            assert result == {123: [10, 20], 456: [10]}

    def test_missing_file_returns_empty(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "nonexistent.json"

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            result = SyncService._load_synced_state(cfg)
            assert result == {}


class TestSaveSyncedState:
    def test_save_and_reload(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            SyncService._save_synced_state(cfg, {123: [10, 20], 456: [10]})

            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            assert loaded == {"ids": {"123": [10, 20], "456": [10]}}

    def test_playlist_ids_are_sorted(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            SyncService._save_synced_state(cfg, {999: [30, 10, 20]})

            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            assert loaded["ids"]["999"] == [10, 20, 30]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> Config:
    cfg = MagicMock(spec=Config)
    cfg.workspace_path = tmp_path
    cfg.synced_state_file = tmp_path / "state" / "synced_tracks.json"
    cfg.processed_state_file = tmp_path / "state" / "processed_files.json"
    cfg.state_dir = tmp_path / "state"
    cfg.lossless_dir = tmp_path / "library" / "lossless"
    cfg.lossy_dir = tmp_path / "library" / "lossy"
    cfg.downloads_dir = tmp_path / "downloads"
    cfg.filename_lossless = "{artist} - {name}"
    cfg.filename_lossy = "{prefix}{name} - {artist}"
    cfg.include_alias_in_filename = True
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
    def test_empty_old_state(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        SyncService._save_synced_state(cfg, {})

        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments({123: [10]}, _make_playlist_index(), {})
        # 不应抛异常

    def test_assignments_unchanged(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        SyncService._save_synced_state(cfg, {123: [10, 20]})
        cfg.processed_state_file.parent.mkdir(parents=True, exist_ok=True)

        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments({123: [10, 20]}, _make_playlist_index(), {})

        result = SyncService._load_synced_state(cfg)
        assert result[123] == [10, 20]

    def test_no_track_in_all_tracks(self, tmp_path: Path) -> None:
        """如果 track 不在 all_tracks 中，应静默跳过。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        SyncService._save_synced_state(cfg, {123: [10]})

        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments({123: [20]}, _make_playlist_index(), {})

        # 状态应更新但无文件操作（无 track 信息）
        result = SyncService._load_synced_state(cfg)
        assert result[123] == [20]


class TestReconcilePlaylistChanged:
    def test_add_playlist_creates_link(self, tmp_path: Path) -> None:
        """曲目新增到歌单B → 在B目录创建硬链接。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        SyncService._save_synced_state(cfg, {123: [10]})

        track = _make_track(123)
        # 创建 canonical 源文件
        flac_src = cfg.downloads_dir / "123.flac"
        mp3_src = cfg.downloads_dir / "123.mp3"
        lrc_src = cfg.downloads_dir / "123.lrc"
        flac_src.write_text("flac")
        mp3_src.write_text("mp3")
        lrc_src.write_text("lrc")

        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments({123: [10, 20]}, _make_playlist_index(), {123: track})

        # B 目录中应有链接
        assert (cfg.lossless_dir / "歌单B" / "Test Artist - Test Song.flac").exists()
        assert (cfg.lossy_dir / "歌单B" / "Test Song - Test Artist.mp3").exists()
        assert (cfg.lossy_dir / "歌单B" / "Test Song - Test Artist.lrc").exists()

        # 状态已更新
        result = SyncService._load_synced_state(cfg)
        assert result[123] == [10, 20]

    def test_remove_playlist_deletes_link(self, tmp_path: Path) -> None:
        """曲目从歌单B移除 → B目录中的链接被删除。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        SyncService._save_synced_state(cfg, {123: [10, 20]})

        track = _make_track(123)
        # 创建 canonical 源文件
        (cfg.downloads_dir / "123.flac").write_text("flac")
        (cfg.downloads_dir / "123.mp3").write_text("mp3")

        # 在 B 目录创建现有链接
        b_ll = cfg.lossless_dir / "歌单B" / "Test Artist - Test Song.flac"
        b_ly = cfg.lossy_dir / "歌单B" / "Test Song - Test Artist.mp3"
        b_ll.parent.mkdir(parents=True)
        b_ly.parent.mkdir(parents=True)
        b_ll.write_text("flac")
        b_ly.write_text("mp3")

        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments({123: [10]}, _make_playlist_index(), {123: track})

        # B 目录中的链接应被删除
        assert not b_ll.exists()
        assert not b_ly.exists()

        # A 目录不受影响（没有创建因为 canonical 文件不在 A）
        result = SyncService._load_synced_state(cfg)
        assert result[123] == [10]

    def test_canonical_missing_skips(self, tmp_path: Path) -> None:
        """canonical 源文件不存在时静默跳过。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        cfg.downloads_dir.mkdir(parents=True)
        SyncService._save_synced_state(cfg, {123: [10]})

        track = _make_track(123)
        # 不创建 canonical 文件

        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments({123: [10, 20]}, _make_playlist_index(), {123: track})

        # 不应创建任何链接（canonical 缺失）
        b_dir = cfg.lossless_dir / "歌单B"
        assert not b_dir.exists() or not any(b_dir.iterdir())


# ---------------------------------------------------------------------------
# 链接文件名生成
# ---------------------------------------------------------------------------


class TestLinkNames:
    def test_lossless_link_name(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.filename_lossless = "{artist} - {name}"
        cfg.include_alias_in_filename = True
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", cover_url=None, raw={})
        name = svc._lossless_link_name(track)
        assert name == "Artist - Song.flac"

    def test_lossy_link_name(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.filename_lossy = "{prefix}{name} - {artist}"
        cfg.include_alias_in_filename = True
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", cover_url=None, raw={})
        name = svc._lossy_link_name(track)
        assert name == "Song - Artist.mp3"

    def test_lossy_link_name_with_alias(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.filename_lossy = "{prefix}{name} - {artist}"
        cfg.include_alias_in_filename = True
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", aliases=["Alias"], cover_url=None, raw={})
        name = svc._lossy_link_name(track)
        assert name == "Alias Song - Artist.mp3"

    def test_lossy_link_name_alias_disabled(self) -> None:
        cfg = MagicMock(spec=Config)
        cfg.filename_lossy = "{prefix}{name} - {artist}"
        cfg.include_alias_in_filename = False
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        track = Track(id=1, name="Song", artists=["Artist"], album="A", aliases=["Alias"], cover_url=None, raw={})
        name = svc._lossy_link_name(track)
        assert name == "Song - Artist.mp3"

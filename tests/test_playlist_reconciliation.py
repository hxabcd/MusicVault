from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from musicvault.core.config import Config
from musicvault.services.sync_service import SyncService


# ---------------------------------------------------------------------------
# synced_tracks.json 格式加载/保存
# ---------------------------------------------------------------------------


class TestLoadSyncedState:
    def test_old_format_flat_list(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(json.dumps({"ids": [123, 456, 789]}), encoding="utf-8")

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            result = SyncService._load_synced_state(cfg)
            assert result == {123: [], 456: [], 789: []}

    def test_new_format_dict(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(
                json.dumps({"ids": {"123": [10, 20], "456": [10]}}), encoding="utf-8"
            )

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            result = SyncService._load_synced_state(cfg)
            assert result == {123: [10, 20], 456: [10]}

    def test_missing_file_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "nonexistent.json"

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            result = SyncService._load_synced_state(cfg)
            assert result == {}


class TestSaveSyncedState:
    def test_save_and_reload(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            SyncService._save_synced_state(cfg, {123: [10, 20], 456: [10]})

            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            assert loaded == {"ids": {"123": [10, 20], "456": [10]}}

    def test_playlist_ids_are_sorted(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            state_file = ws / "state" / "synced_tracks.json"

            cfg = MagicMock(spec=Config)
            cfg.synced_state_file = state_file

            SyncService._save_synced_state(cfg, {999: [30, 10, 20]})

            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            assert loaded["ids"]["999"] == [10, 20, 30]


# ---------------------------------------------------------------------------
# 歌单分配协调
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> Config:
    """创建测试用 Config，指向临时 workspace。"""
    cfg = MagicMock(spec=Config)
    cfg.workspace_path = tmp_path
    cfg.synced_state_file = tmp_path / "state" / "synced_tracks.json"
    cfg.processed_state_file = tmp_path / "state" / "processed_files.json"
    cfg.state_dir = tmp_path / "state"
    cfg.lossless_dir = tmp_path / "library" / "lossless"
    cfg.lossy_dir = tmp_path / "library" / "lossy"
    cfg.downloads_dir = tmp_path / "downloads"
    return cfg


def _make_playlist_index() -> dict[str, dict[str, object]]:
    return {
        "10": {"name": "歌单A", "track_count": 10},
        "20": {"name": "歌单B", "track_count": 5},
        "30": {"name": "歌单C", "track_count": 3},
    }


class TestReconcileNoChange:
    def test_empty_old_state(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)

        # 旧状态为空
        SyncService._save_synced_state(cfg, {})

        svc._reconcile_playlist_assignments(
            {123: [10]}, _make_playlist_index()
        )
        # 不应抛异常，processed_files 不应被修改

    def test_assignments_unchanged(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)

        # 创建旧状态
        SyncService._save_synced_state(cfg, {123: [10, 20]})

        # 创建 processed_files.json
        processed = {
            "downloads/track_123.mp3": {
                "track_id": 123,
                "lossless": "library/lossless/歌单A/Artist - Song.flac",
                "lossy": "library/lossy/歌单A/Song - Artist.mp3",
                "links": [
                    {
                        "lossless": "library/lossless/歌单B/Artist - Song.flac",
                        "lossy": "library/lossy/歌单B/Song - Artist.mp3",
                    }
                ],
            }
        }
        cfg.processed_state_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.processed_state_file.write_text(json.dumps(processed), encoding="utf-8")

        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments(
            {123: [10, 20]}, _make_playlist_index()
        )

        # 状态不应变化
        result = SyncService._load_synced_state(cfg)
        assert result[123] == [10, 20]

        # processed_files 不应变化
        reloaded = json.loads(cfg.processed_state_file.read_text(encoding="utf-8"))
        assert reloaded == processed


class TestReconcilePrimaryChanged:
    def test_move_files_to_new_primary(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)

        # 旧状态：主歌单为 10 (歌单A)
        SyncService._save_synced_state(cfg, {123: [10]})

        # 创建旧文件
        old_ll_dir = cfg.lossless_dir / "歌单A"
        old_ly_dir = cfg.lossy_dir / "歌单A"
        old_ll_dir.mkdir(parents=True)
        old_ly_dir.mkdir(parents=True)
        old_ll = old_ll_dir / "Artist - Song.flac"
        old_ly = old_ly_dir / "Song - Artist.mp3"
        old_ll.write_text("lossless content")
        old_ly.write_text("lossy content")

        processed = {
            "downloads/track_123.mp3": {
                "track_id": 123,
                "lossless": "library/lossless/歌单A/Artist - Song.flac",
                "lossy": "library/lossy/歌单A/Song - Artist.mp3",
                "links": [],
            }
        }
        cfg.processed_state_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.processed_state_file.write_text(json.dumps(processed), encoding="utf-8")

        # 远程现在返回主歌单为 20 (歌单B)
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments(
            {123: [20]}, _make_playlist_index()
        )

        # 文件应移动到新目录
        new_ll = cfg.lossless_dir / "歌单B" / "Artist - Song.flac"
        new_ly = cfg.lossy_dir / "歌单B" / "Song - Artist.mp3"
        assert new_ll.exists()
        assert new_ly.exists()
        assert not old_ll.exists()
        assert not old_ly.exists()

        # processed_files 路径已更新
        reloaded = json.loads(cfg.processed_state_file.read_text(encoding="utf-8"))
        entry = reloaded["downloads/track_123.mp3"]
        assert entry["lossless"] == "library/lossless/歌单B/Artist - Song.flac"
        assert entry["lossy"] == "library/lossy/歌单B/Song - Artist.mp3"

    def test_primary_swap_with_link_back(self, tmp_path: Path) -> None:
        """主歌单从 A 变为 B，但 A 仍在分配中 → 移动 + 创建链接回 A。"""
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)

        SyncService._save_synced_state(cfg, {123: [10, 20]})

        # 旧文件在歌单A（主歌单）
        old_ll_dir = cfg.lossless_dir / "歌单A"
        old_ly_dir = cfg.lossy_dir / "歌单A"
        old_ll_dir.mkdir(parents=True)
        old_ly_dir.mkdir(parents=True)
        old_ll = old_ll_dir / "Artist - Song.flac"
        old_ly = old_ly_dir / "Song - Artist.mp3"
        old_ll.write_text("lossless")
        old_ly.write_text("lossy")

        # 旧链接在歌单B
        link_ll_dir = cfg.lossless_dir / "歌单B"
        link_ly_dir = cfg.lossy_dir / "歌单B"
        link_ll_dir.mkdir(parents=True)
        link_ly_dir.mkdir(parents=True)

        processed = {
            "downloads/track_123.mp3": {
                "track_id": 123,
                "lossless": "library/lossless/歌单A/Artist - Song.flac",
                "lossy": "library/lossy/歌单A/Song - Artist.mp3",
                "links": [
                    {
                        "lossless": "library/lossless/歌单B/Artist - Song.flac",
                        "lossy": "library/lossy/歌单B/Song - Artist.mp3",
                    }
                ],
            }
        }
        cfg.processed_state_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.processed_state_file.write_text(json.dumps(processed), encoding="utf-8")

        # 远程：B 变为主歌单，A 变为次要 [20, 10]
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments(
            {123: [20, 10]}, _make_playlist_index()
        )

        # 主文件应移动到 B
        new_ll = cfg.lossless_dir / "歌单B" / "Artist - Song.flac"
        new_ly = cfg.lossy_dir / "歌单B" / "Song - Artist.mp3"
        assert new_ll.exists()
        assert new_ly.exists()

        # A 中也有文件（通过硬链接回链，因为 A 仍在分配中）
        a_ll = cfg.lossless_dir / "歌单A" / "Artist - Song.flac"
        a_ly = cfg.lossy_dir / "歌单A" / "Song - Artist.mp3"
        assert a_ll.exists()
        assert a_ly.exists()

        # processed_files 中 lossless/lossy 指向 B，links 指向 A
        reloaded = json.loads(cfg.processed_state_file.read_text(encoding="utf-8"))
        entry = reloaded["downloads/track_123.mp3"]
        assert "歌单B" in entry["lossless"]
        assert "歌单B" in entry["lossy"]
        assert any("歌单A" in lnk["lossless"] for lnk in entry.get("links", []))


class TestReconcileLinkAdded:
    def test_add_link_for_new_playlist(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)

        SyncService._save_synced_state(cfg, {123: [10]})

        # 主文件在歌单A
        ll_dir = cfg.lossless_dir / "歌单A"
        ly_dir = cfg.lossy_dir / "歌单A"
        ll_dir.mkdir(parents=True)
        ly_dir.mkdir(parents=True)
        ll_file = ll_dir / "Artist - Song.flac"
        ly_file = ly_dir / "Song - Artist.mp3"
        ll_file.write_text("lossless")
        ly_file.write_text("lossy")

        processed = {
            "downloads/track_123.mp3": {
                "track_id": 123,
                "lossless": "library/lossless/歌单A/Artist - Song.flac",
                "lossy": "library/lossy/歌单A/Song - Artist.mp3",
                "links": [],
            }
        }
        cfg.processed_state_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.processed_state_file.write_text(json.dumps(processed), encoding="utf-8")

        # 远程：歌曲同时属于 A 和 B
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments(
            {123: [10, 20]}, _make_playlist_index()
        )

        # B 中应有链接
        b_ll = cfg.lossless_dir / "歌单B" / "Artist - Song.flac"
        b_ly = cfg.lossy_dir / "歌单B" / "Song - Artist.mp3"
        assert b_ll.exists()
        assert b_ly.exists()

        # processed_files 的 links 已更新
        reloaded = json.loads(cfg.processed_state_file.read_text(encoding="utf-8"))
        entry = reloaded["downloads/track_123.mp3"]
        assert len(entry.get("links", [])) == 1
        assert "歌单B" in entry["links"][0]["lossless"]


class TestReconcileLinkRemoved:
    def test_remove_link_for_removed_playlist(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.state_dir.mkdir(parents=True)

        SyncService._save_synced_state(cfg, {123: [10, 20]})

        # 主文件在歌单A
        ll_dir_a = cfg.lossless_dir / "歌单A"
        ly_dir_a = cfg.lossy_dir / "歌单A"
        ll_dir_a.mkdir(parents=True)
        ly_dir_a.mkdir(parents=True)
        (ll_dir_a / "Artist - Song.flac").write_text("lossless")
        (ly_dir_a / "Song - Artist.mp3").write_text("lossy")

        # 链接在歌单B
        ll_dir_b = cfg.lossless_dir / "歌单B"
        ly_dir_b = cfg.lossy_dir / "歌单B"
        ll_dir_b.mkdir(parents=True)
        ly_dir_b.mkdir(parents=True)
        (ll_dir_b / "Artist - Song.flac").write_text("lossless")
        (ly_dir_b / "Song - Artist.mp3").write_text("lossy")

        processed = {
            "downloads/track_123.mp3": {
                "track_id": 123,
                "lossless": "library/lossless/歌单A/Artist - Song.flac",
                "lossy": "library/lossy/歌单A/Song - Artist.mp3",
                "links": [
                    {
                        "lossless": "library/lossless/歌单B/Artist - Song.flac",
                        "lossy": "library/lossy/歌单B/Song - Artist.mp3",
                    }
                ],
            }
        }
        cfg.processed_state_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.processed_state_file.write_text(json.dumps(processed), encoding="utf-8")

        # 远程：歌曲仅属于 A（从 B 移除）
        svc = SyncService(cfg, MagicMock(), MagicMock(), workers=1)
        svc._reconcile_playlist_assignments(
            {123: [10]}, _make_playlist_index()
        )

        # B 中的链接文件应被删除
        b_ll = cfg.lossless_dir / "歌单B" / "Artist - Song.flac"
        b_ly = cfg.lossy_dir / "歌单B" / "Song - Artist.mp3"
        assert not b_ll.exists()
        assert not b_ly.exists()

        # A 中的主文件应保留
        assert (ll_dir_a / "Artist - Song.flac").exists()

        # processed_files 的 links 应清空
        reloaded = json.loads(cfg.processed_state_file.read_text(encoding="utf-8"))
        entry = reloaded["downloads/track_123.mp3"]
        assert entry.get("links", []) == []


class TestReplaceDirInPath:
    def test_basic(self) -> None:
        result = SyncService._replace_dir_in_path(
            "library/lossless/OldName/file.flac", "OldName", "NewName"
        )
        assert result == "library/lossless/NewName/file.flac"

    def test_no_match_unchanged(self) -> None:
        result = SyncService._replace_dir_in_path(
            "library/lossless/Other/file.flac", "OldName", "NewName"
        )
        assert result == "library/lossless/Other/file.flac"

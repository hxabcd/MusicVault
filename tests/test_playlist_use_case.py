"""PlaylistUseCase（歌单/单曲管理）测试。

原 Config 的 JSON 管理方法（songs.json/playlists.json）退役后，管理语义
由本用例 + SQLite 状态库承载，此处覆盖其行为。
"""

from __future__ import annotations

import os
from pathlib import Path

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.playlist_use_case import PlaylistUseCase
from musicvault.core.config import Config
from musicvault.domain.models import Playlist


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def _repository(cfg: Config) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(cfg.state_db_file))


def _use_case(tmp_path: Path) -> tuple[Config, SQLiteStateRepository, PlaylistUseCase]:
    cfg = _make_cfg(tmp_path)
    repo = _repository(cfg)
    return cfg, repo, PlaylistUseCase(cfg, repo)


class TestSongManagement:
    def test_add_and_list_songs(self, tmp_path: Path) -> None:
        cfg, repo, use_case = _use_case(tmp_path)
        assert use_case.list_songs() == []

        use_case.add_song(123)
        use_case.add_song(456)
        assert use_case.list_songs() == [123, 456]
        assert repo.list_managed_songs() == [123, 456]

    def test_add_duplicate_is_idempotent(self, tmp_path: Path) -> None:
        _, _, use_case = _use_case(tmp_path)
        use_case.add_song(100)
        use_case.add_song(100)
        assert use_case.list_songs() == [100]

    def test_has_song(self, tmp_path: Path) -> None:
        _, _, use_case = _use_case(tmp_path)
        use_case.add_song(42)
        assert use_case.has_song(42) is True
        assert use_case.has_song(99) is False

    def test_add_song_creates_placeholder_track(self, tmp_path: Path) -> None:
        """managed_songs 外键约束：track 未同步前先登记占位曲目。"""
        _, repo, use_case = _use_case(tmp_path)
        use_case.add_song(999)
        snapshot = repo.create_snapshot()
        assert snapshot.track(999) is not None
        assert use_case.has_song(999) is True

    def test_remove_song(self, tmp_path: Path) -> None:
        cfg, repo, use_case = _use_case(tmp_path)
        canonical = cfg.media_store_dir / "1" / "1.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake")
        use_case.add_song(1)
        use_case.add_song(2)
        use_case.remove_song(1)
        assert use_case.list_songs() == [2]
        assert not canonical.exists()
        # media_store 扁平化：整个 track 目录被删除
        assert not canonical.parent.exists()

    def test_songs_survive_roundtrip(self, tmp_path: Path) -> None:
        cfg, repo, _ = _use_case(tmp_path)
        use_case = PlaylistUseCase(cfg, repo)
        use_case.add_song(10)
        use_case.add_song(20)

        repo2 = _repository(cfg)
        assert PlaylistUseCase(cfg, repo2).list_songs() == [10, 20]


class TestPlaylistManagement:
    def test_add_and_list_playlists(self, tmp_path: Path) -> None:
        _, repo, use_case = _use_case(tmp_path)
        assert use_case.list_playlists() == []

        use_case.add_playlist(10, name="歌单A")
        assert [pl.id for pl in use_case.list_playlists()] == [10]
        assert use_case.list_playlists()[0].name == "歌单A"

    def test_has_playlist(self, tmp_path: Path) -> None:
        _, _, use_case = _use_case(tmp_path)
        use_case.add_playlist(10)
        assert use_case.has_playlist(10) is True
        assert use_case.has_playlist(99) is False

    def test_remove_playlist_cleans_state(self, tmp_path: Path) -> None:
        cfg, repo, use_case = _use_case(tmp_path)
        use_case.add_playlist(10, name="歌单A")
        repo.upsert_track(_make_track(111))
        repo.upsert_playlist(Playlist(10, "歌单A", (111,)))
        # library 中的歌单目录（hardlink sync_target 导出根）
        library_dir = cfg.library_dir / "歌单A"
        library_dir.mkdir(parents=True, exist_ok=True)
        (library_dir / "111.flac").write_bytes(b"fake")
        # media_store 扁平布局：canonical 在 media_store/<tid>/ 下
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake")
        # 未分类 中的硬链接（与 canonical 同一 inode）
        uncat_dir = cfg.library_dir / cfg.default_playlist_name
        uncat_dir.mkdir(parents=True, exist_ok=True)
        os.link(canonical, uncat_dir / "111.flac")

        use_case.remove_playlist(10)

        assert use_case.has_playlist(10) is False
        snapshot = repo.create_snapshot()
        assert snapshot.tracks == ()
        assert not library_dir.exists()
        assert not canonical.exists()
        assert not canonical.parent.exists()
        # 未分类 硬链接同步清除（指向同一 inode），空目录一并删除
        assert not uncat_dir.exists()


def _make_track(track_id: int):
    from musicvault.domain.models import Track

    return Track(id=track_id, name=f"曲目{track_id}", artists=["歌手"], album="专辑", raw={})

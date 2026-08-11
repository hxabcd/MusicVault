"""PlaylistUseCase（歌单/单曲管理）测试。

原 Config 的 JSON 管理方法（songs.json/playlists.json）退役后，管理语义
由本用例 + SQLite 状态库承载，此处覆盖其行为。
"""

from __future__ import annotations

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
        cfg.downloads_dir.mkdir(parents=True)
        canonical = cfg.downloads_dir / "1.flac"
        canonical.write_bytes(b"fake")
        use_case.add_song(1)
        use_case.add_song(2)
        use_case.remove_song(1)
        assert use_case.list_songs() == [2]
        assert not canonical.exists()

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
        cfg.downloads_dir.mkdir(parents=True)
        (cfg.downloads_dir / "111.flac").write_bytes(b"fake")

        use_case.remove_playlist(10)

        assert use_case.has_playlist(10) is False
        snapshot = repo.create_snapshot()
        assert snapshot.tracks == ()
        assert not (cfg.downloads_dir / "111.flac").exists()


def _make_track(track_id: int):
    from musicvault.domain.models import Track

    return Track(id=track_id, name=f"曲目{track_id}", artists=["歌手"], album="专辑", raw={})

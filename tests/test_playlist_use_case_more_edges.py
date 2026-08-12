"""PlaylistUseCase（歌单/单曲管理）边界路径补充测试。

覆盖 get_playlist 查询、移除不存在的歌单、media_store/library 目录的
形态异常（缺目录、子目录、stat 失败、rmdir 失败）等降级分支。
"""

from __future__ import annotations

import logging
import os
import pathlib
from pathlib import Path

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.playlist_use_case import PlaylistUseCase
from musicvault.core.config import Config
from musicvault.domain.models import Playlist, Track


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def _repository(cfg: Config) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(cfg.state_db_file))


def _use_case(tmp_path: Path) -> tuple[Config, SQLiteStateRepository, PlaylistUseCase]:
    cfg = _make_cfg(tmp_path)
    repo = _repository(cfg)
    return cfg, repo, PlaylistUseCase(cfg, repo)


def _make_track(track_id: int) -> Track:
    return Track(id=track_id, name=f"曲目{track_id}", artists=["歌手"], album="专辑", raw={})


class TestPlaylistQuery:
    def test_get_playlist_returns_playlist_or_none(self, tmp_path: Path) -> None:
        """get_playlist：已登记返回歌单对象，未登记返回 None。"""
        _, _, use_case = _use_case(tmp_path)
        use_case.add_playlist(10, name="歌单A")

        playlist = use_case.get_playlist(10)
        assert playlist is not None
        assert playlist.name == "歌单A"
        assert use_case.get_playlist(99) is None


class TestRemovePlaylistEdges:
    def test_remove_missing_playlist_is_noop(self, tmp_path: Path) -> None:
        """移除未登记的歌单：无任何副作用。"""
        cfg, repo, use_case = _use_case(tmp_path)
        use_case.add_playlist(10, name="歌单A")

        use_case.remove_playlist(999)

        assert use_case.has_playlist(10) is True
        assert repo.list_tracks() == []
        assert not cfg.library_dir.exists()

    def test_remove_playlist_without_library_dir_logs_skip(self, tmp_path: Path, caplog) -> None:
        """library 中无歌单目录：不删除文件，仅记录「未找到目录」日志。"""
        _, repo, use_case = _use_case(tmp_path)
        use_case.add_playlist(10, name="歌单A")

        with caplog.at_level(logging.INFO, logger="musicvault.application.playlist_use_case"):
            use_case.remove_playlist(10)

        assert use_case.has_playlist(10) is False
        assert any("未找到" in record.message for record in caplog.records)

    def test_remove_playlist_missing_track_dir(self, tmp_path: Path) -> None:
        """歌单曲目的 media_store 目录不存在：跳过 inode 收集，仍清理状态。"""
        _, repo, use_case = _use_case(tmp_path)
        repo.upsert_track(_make_track(111))
        repo.upsert_playlist(Playlist(10, "歌单A", (111,)))

        use_case.remove_playlist(10)

        assert use_case.has_playlist(10) is False
        assert repo.get_track(111) is None

    def test_remove_playlist_skips_subdirs_in_track_dir(self, tmp_path: Path) -> None:
        """media_store/<tid>/ 内的子目录：跳过收集（只收普通文件）。"""
        cfg, repo, use_case = _use_case(tmp_path)
        repo.upsert_track(_make_track(111))
        repo.upsert_playlist(Playlist(10, "歌单A", (111,)))
        track_dir = cfg.media_store_dir / "111"
        track_dir.mkdir(parents=True)
        (track_dir / "sub").mkdir()
        (track_dir / "111.flac").write_bytes(b"fake flac")

        use_case.remove_playlist(10)

        # 整个 track 目录被删除（含子目录与 canonical）
        assert not track_dir.exists()

    def test_remove_playlist_ignores_stat_failure(self, tmp_path: Path, monkeypatch) -> None:
        """track 目录与未分类目录中个别文件 stat 失败：跳过不中断删除。"""
        cfg, repo, use_case = _use_case(tmp_path)
        repo.upsert_track(_make_track(111))
        repo.upsert_playlist(Playlist(10, "歌单A", (111,)))
        track_dir = cfg.media_store_dir / "111"
        track_dir.mkdir(parents=True)
        (track_dir / "blocked.flac").write_bytes(b"fake")
        (track_dir / "good.flac").write_bytes(b"fake")  # 正常文件保证 canonical_inodes 非空
        uncat_dir = cfg.library_dir / cfg.default_playlist_name
        uncat_dir.mkdir(parents=True)
        uncat_blocked = uncat_dir / "blocked2.flac"
        uncat_blocked.write_bytes(b"fake")

        def _flaky_stat(self, **kwargs):
            del kwargs
            if self.name in {"blocked.flac", "blocked2.flac"}:
                raise OSError("拒绝访问")
            return os.stat(self)

        monkeypatch.setattr(pathlib.Path, "stat", _flaky_stat)
        monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)

        use_case.remove_playlist(10)

        assert use_case.has_playlist(10) is False
        # rmtree 删除整个 track 目录；stat 失败的文件未被 unlink，留在未分类目录
        assert not track_dir.exists()
        assert os.path.exists(uncat_blocked)

    def test_remove_playlist_skips_subdirs_in_uncategorized(self, tmp_path: Path) -> None:
        """未分类目录中的子目录：跳过 unlink 匹配（只处理普通文件）。"""
        cfg, repo, use_case = _use_case(tmp_path)
        repo.upsert_track(_make_track(111))
        repo.upsert_playlist(Playlist(10, "歌单A", (111,)))
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b"fake flac")
        uncat_dir = cfg.library_dir / cfg.default_playlist_name
        uncat_dir.mkdir(parents=True)
        os.link(canonical, uncat_dir / "111.flac")
        (uncat_dir / "sub").mkdir()

        use_case.remove_playlist(10)

        assert not (uncat_dir / "111.flac").exists()
        # 子目录保留（不参与 inode 匹配，rmdir 前仍有内容故跳过空目录检查的删除）
        assert (uncat_dir / "sub").is_dir()

    def test_remove_playlist_ignores_rmdir_failure(self, tmp_path: Path, monkeypatch) -> None:
        """未分类目录 rmdir 失败（如并发写入）：异常被忽略，不影响整体流程。"""
        cfg, repo, use_case = _use_case(tmp_path)
        repo.upsert_track(_make_track(111))
        repo.upsert_playlist(Playlist(10, "歌单A", (111,)))
        canonical = cfg.media_store_dir / "111" / "111.flac"
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b"fake flac")
        uncat_dir = cfg.library_dir / cfg.default_playlist_name
        uncat_dir.mkdir(parents=True)
        os.link(canonical, uncat_dir / "111.flac")

        def _flaky_rmdir(self, **kwargs):
            del self, kwargs
            raise OSError("目录删除失败")

        monkeypatch.setattr(pathlib.Path, "rmdir", _flaky_rmdir)

        use_case.remove_playlist(10)

        assert not (uncat_dir / "111.flac").exists()
        # rmdir 失败被忽略，目录保留
        assert uncat_dir.is_dir()

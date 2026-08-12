"""SQLite 状态仓储补充单测：显式连接分支、全量查询与目标端登记。

覆盖：remove_track/remove_playlist/register_preset/save_lyrics 的传入连接分支、
list_media_assets 全量查询、register_target 校验与 upsert。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.domain.models import Track
from musicvault.domain.models import MediaAsset, Playlist


def _track(track_id: int = 1) -> Track:
    return Track(id=track_id, name=f"曲目 {track_id}", artists=["歌手"], album="专辑", raw={})


def _repository(tmp_path: Path) -> SQLiteStateRepository:
    return SQLiteStateRepository(SQLiteState(tmp_path / "state.db"))


def test_remove_track_with_explicit_connection(tmp_path: Path) -> None:
    """remove_track 传入外部连接时在事务内执行并级联删除歌词。"""
    repo = _repository(tmp_path)
    repo.upsert_track(_track(1))
    repo.save_lyrics(1, "[]", 0.0)

    with repo.transaction() as connection:
        repo.remove_track(1, connection=connection)

    assert repo.get_track(1) is None
    assert repo.get_lyrics(1) is None


def test_remove_playlist_with_explicit_connection(tmp_path: Path) -> None:
    """remove_playlist 传入外部连接时删除歌单及其曲目关系。"""
    repo = _repository(tmp_path)
    repo.save_source_state([_track(1)], [Playlist(id=10, name="歌单", track_ids=(1,))], [])

    with repo.transaction() as connection:
        repo.remove_playlist(10, connection=connection)

    assert repo.get_playlist(10) is None
    with repo.database.connect() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = 10").fetchone()[0]
    assert remaining == 0


def test_list_media_assets_all(tmp_path: Path) -> None:
    """list_media_assets 无参返回全部资产（跨曲目）。"""
    repo = _repository(tmp_path)
    repo.upsert_track(_track(1))
    repo.upsert_track(_track(2))
    repo.upsert_media_asset(MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=tmp_path / "1.flac"))
    repo.upsert_media_asset(MediaAsset(track_id=2, asset_type="audio", spec="FLAC", path=tmp_path / "2.flac"))

    assets = repo.list_media_assets()
    assert len(assets) == 2
    assert {asset.track_id for asset in assets} == {1, 2}


def test_register_preset_with_explicit_connection(tmp_path: Path) -> None:
    """register_preset 传入外部连接时写入生效。"""
    repo = _repository(tmp_path)
    with repo.transaction() as connection:
        repo.register_preset("x", "builtin:x", "v1", kind="preset", connection=connection)

    registered = repo.list_registered_presets()
    assert len(registered) == 1
    assert registered[0].name == "x"
    assert registered[0].kind == "preset"


def test_save_lyrics_with_explicit_connection(tmp_path: Path) -> None:
    """save_lyrics 传入外部连接时写入生效。"""
    repo = _repository(tmp_path)
    with repo.transaction() as connection:
        repo.save_lyrics(1, "[歌词]", 0.0, connection=connection)

    assert repo.get_lyrics(1) == "[歌词]"


def test_register_target_upsert(tmp_path: Path) -> None:
    """register_target 首次登记与同 id 更新（upsert）。"""
    repo = _repository(tmp_path)
    repo.register_target("library", "filesystem", "append", {"root": "/x"})
    repo.register_target("library", "filesystem", "managed")

    with repo.database.connect() as connection:
        row = connection.execute("SELECT * FROM export_targets WHERE id = 'library'").fetchone()
    assert row["deletion_policy"] == "managed"
    assert row["config_json"] == "{}"


def test_register_target_rejects_invalid_policy(tmp_path: Path) -> None:
    """未知删除策略 → ValueError。"""
    repo = _repository(tmp_path)
    with pytest.raises(ValueError, match="删除策略"):
        repo.register_target("t", "filesystem", "nuke")

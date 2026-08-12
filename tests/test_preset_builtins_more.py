"""内置 preset（archive/hardlink）补充单测：分发边界与陈旧链接清理。

覆盖：sync_item 无资产/无歌单归属、_remove_stale_links 缺失文件与 owned 目录
跳过、_inode 失败容错、finalize 目标根不存在、hardlink 依赖注入类型校验。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, Track
from musicvault.preset_api.builtins import ArchivePreset, HardlinkDistributor, register_builtin_presets
from musicvault.preset_api.v1 import PresetContext, PresetRegistration, PresetRegistry


def _track(track_id: int = 1) -> Track:
    return Track(id=track_id, name="song", artists=[], album="")


def _context(snapshot: SourceSnapshot, media_store_root: Path, library: Path) -> PresetContext:
    return PresetContext(snapshot=snapshot, target=FilesystemTarget(library), media_store_root=media_store_root)


def _write_audio(media_store: Path, track_id: int = 1) -> Path:
    """在 media_store/<tid>/ 下写入音频文件并返回路径。"""
    audio_dir = media_store / str(track_id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio = audio_dir / f"{track_id}.flac"
    audio.write_bytes(b"FLAC")
    return audio


def test_sync_item_without_asset_returns_none(tmp_path: Path) -> None:
    """快照中无该曲目资产 → sync_item 直接返回，不产生链接。"""
    snapshot = SourceSnapshot.from_data((_track(),), (), ())
    context = _context(snapshot, tmp_path / "media_store", tmp_path / "library")
    distributor = HardlinkDistributor(ArchivePreset(), "archive", tmp_path / "library")

    assert distributor.sync_item(_track(), context) is None
    assert context.operations == ()


def test_sync_item_falls_back_to_default_name(tmp_path: Path) -> None:
    """曲目不归属任何歌单 → 链接到默认目录。"""
    media_store = tmp_path / "media_store"
    audio = _write_audio(media_store)
    snapshot = SourceSnapshot.from_data(
        (_track(),),
        (),
        (MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=audio),),
    )
    library = tmp_path / "library"
    context = _context(snapshot, media_store, library)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")

    distributor.sync_item(_track(), context)

    assert (library / "未分类" / "Unknown Artist - song.flac").exists()


def test_sync_item_with_missing_audio_file_skips_cleanup(tmp_path: Path) -> None:
    """资产声明存在但磁盘文件缺失 → _inode 失败，陈旧链接清理跳过且不报错。"""
    media_store = tmp_path / "media_store"
    missing = media_store / "1" / "1.flac"
    library = tmp_path / "library"
    stale_dir = library / "old"
    stale_dir.mkdir(parents=True)
    (stale_dir / "x.flac").write_bytes(b"x")

    snapshot = SourceSnapshot.from_data(
        (_track(),),
        (Playlist(1, "fav", (1,)),),
        (MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=missing),),
    )
    context = _context(snapshot, media_store, library)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library)

    distributor.sync_item(_track(), context)

    # 链接失败被 executor 记录为 FAILED，不抛异常；陈旧链接保留
    assert [op.status.name for op in context.operations] == ["FAILED"]
    assert (stale_dir / "x.flac").exists()


def test_sync_item_preserves_owned_directory_links(tmp_path: Path) -> None:
    """_remove_stale_links 跳过 owned_names 内目录，仅清理外部目录的陈旧链接。"""
    media_store = tmp_path / "media_store"
    audio = _write_audio(media_store)
    library = tmp_path / "library"
    # 两个目录都指向同一音频：fav 属于 owned，old 是陈旧目录
    for dirname in ("fav", "old"):
        directory = library / dirname
        directory.mkdir(parents=True)
        os.link(audio, directory / "song.flac")

    snapshot = SourceSnapshot.from_data(
        (_track(),),
        (Playlist(1, "fav", (1,)),),
        (MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=audio),),
    )
    context = _context(snapshot, media_store, library)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library)

    distributor.sync_item(_track(), context)

    assert (library / "fav" / "song.flac").exists()  # owned 目录链接保留
    assert not (library / "old" / "song.flac").exists()  # 陈旧目录链接被清理


def test_finalize_without_target_root_returns_none(tmp_path: Path) -> None:
    """目标根不存在时 finalize 直接返回。"""
    snapshot = SourceSnapshot.from_data((_track(),), (Playlist(1, "fav", (1,)),), ())
    context = _context(snapshot, tmp_path / "media_store", tmp_path / "library")
    distributor = HardlinkDistributor(ArchivePreset(), "archive", tmp_path / "nonexistent")

    assert distributor.finalize(context) is None


def test_create_target_rejects_non_base_preset_archive(tmp_path: Path) -> None:
    """hardlink 依赖的 archive 非 BasePreset → TypeError。"""
    registry = PresetRegistry()
    register_builtin_presets(registry, tmp_path / "library")
    # 覆盖 archive 注册为返回普通对象的 factory
    registry._registrations["archive"] = PresetRegistration(name="archive", factory=lambda: object(), source="test")

    with pytest.raises(TypeError, match="类型不合法"):
        registry.create_target("hardlink")


def test_archive_preset_build_lyrics_enhanced(tmp_path: Path) -> None:
    """ArchivePreset.build_lyrics 使用增强歌词（翻译+罗马音）。"""
    from musicvault.domain.lyrics import LyricLine

    lines = (LyricLine(1000, 3000, "hello", translation="你好", romaji="haro"),)
    assert ArchivePreset().build_lyrics(lines) == ("[00:01.000]hello\n[00:01.000]你好\n[00:01.000]haro")

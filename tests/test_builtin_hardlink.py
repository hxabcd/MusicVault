"""内置 archive preset 与 hardlink 幂等分发测试。

覆盖：ArchivePreset 声明（FLAC/hires/全量元数据/增强歌词）；HardlinkDistributor
按歌单目录硬链接音频与歌词并清理陈旧链接；finalize 删除快照外歌单目录；注册表注册。
"""

from __future__ import annotations

import os
from pathlib import Path

from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, Track
from musicvault.domain.operations import OperationStatus
from musicvault.preset_api.builtins import ArchivePreset, register_builtin_presets
from musicvault.preset_api.v1 import AudioFormat, PresetRegistry
from musicvault.target_api.builtins import HardlinkDistributor, register_builtin_targets
from musicvault.target_api.v1 import TargetContext, TargetRegistry


class FakeTarget:
    """记录 link 调用，并真实创建硬链接（忠实模拟 FilesystemTarget.link）。"""

    def __init__(self) -> None:
        self.links: list[tuple[Path, Path]] = []

    def link(self, source, destination) -> None:
        source = Path(source)
        destination = Path(destination)
        self.links.append((source, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, destination)

    def copy(self, source, destination) -> None:
        del source, destination

    def write_text(self, destination, content, encoding="utf-8") -> None:
        del destination, content, encoding


def _make_context(snapshot: SourceSnapshot, media_store_root: Path, *, dry_run: bool = False) -> TargetContext:
    return TargetContext(snapshot=snapshot, target=FakeTarget(), dry_run=dry_run, media_store_root=media_store_root)


def _snapshot(track: Track, playlists: tuple[Playlist, ...], assets: tuple[MediaAsset, ...]) -> SourceSnapshot:
    return SourceSnapshot.from_data(tracks=(track,), playlists=playlists, media_assets=assets)


def test_archive_preset_declares_flac_full_metadata() -> None:
    preset = ArchivePreset()
    assert preset.format == AudioFormat.FLAC
    assert preset.quality.value == "hires"
    assert preset.metadata.embed_cover is True


def test_hardlink_links_audio_and_lyrics_to_owned_playlist(tmp_path: Path) -> None:
    media = tmp_path / "media_store"
    audio_dir = media / "1"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "1.flac"
    audio.write_bytes(b"FLAC")
    (audio_dir / "1.archive.lrc").write_bytes(b"LRC")
    library = tmp_path / "library"
    library.mkdir()

    track = Track(id=1, name="song", artists=[], album="", raw={})
    snapshot = _snapshot(
        track,
        (Playlist(1, "fav", (1,)),),
        (MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=audio, size=4),),
    )
    context = _make_context(snapshot, media)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.sync_item(track, context)

    # format_track_name("{artist} - {name}") 对空 artists 回退 "Unknown Artist"
    assert isinstance(context.target, FakeTarget)
    assert len(context.target.links) == 2
    assert context.target.links[0][1] == library / "fav" / "Unknown Artist - song.flac"
    assert context.target.links[1][1] == library / "fav" / "Unknown Artist - song.lrc"


def test_hardlink_removes_stale_link_on_playlist_change(tmp_path: Path) -> None:
    media = tmp_path / "media_store"
    audio_dir = media / "1"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "1.flac"
    audio.write_bytes(b"FLAC")
    library = tmp_path / "library"
    old_dir = library / "old"
    old_dir.mkdir(parents=True)
    old_link = old_dir / "song.flac"
    os.link(audio, old_link)

    track = Track(id=1, name="song", artists=[], album="", raw={})
    snapshot = _snapshot(
        track,
        (Playlist(1, "fav", (1,)),),
        (MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=audio, size=4),),
    )
    context = _make_context(snapshot, media)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.sync_item(track, context)

    assert not old_link.exists()
    assert (library / "fav" / "Unknown Artist - song.flac").exists()


def test_hardlink_sync_item_dry_run_does_not_create_dirs(tmp_path: Path) -> None:
    """dry-run：不创建歌单目录（目录创建是副作用，仅链接走 PLANNED 语义）。"""
    media = tmp_path / "media_store"
    audio_dir = media / "1"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "1.flac"
    audio.write_bytes(b"FLAC")
    library = tmp_path / "library"
    library.mkdir()

    track = Track(id=1, name="song", artists=[], album="", raw={})
    snapshot = _snapshot(
        track,
        (Playlist(1, "fav", (1,)),),
        (MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=audio, size=4),),
    )
    context = _make_context(snapshot, media, dry_run=True)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.sync_item(track, context)

    assert not (library / "fav").exists()
    assert list(library.iterdir()) == []


def test_hardlink_sync_item_dry_run_skips_deletion_and_only_plans_links(tmp_path: Path) -> None:
    media = tmp_path / "media_store"
    audio_dir = media / "1"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "1.flac"
    audio.write_bytes(b"FLAC")
    library = tmp_path / "library"
    old_dir = library / "old"
    old_dir.mkdir(parents=True)
    old_link = old_dir / "song.flac"
    os.link(audio, old_link)

    track = Track(id=1, name="song", artists=[], album="", raw={})
    snapshot = _snapshot(
        track,
        (Playlist(1, "fav", (1,)),),
        (MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=audio, size=4),),
    )
    context = _make_context(snapshot, media, dry_run=True)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.sync_item(track, context)

    assert old_link.exists()  # dry-run 不删除陈旧链接
    assert not (library / "fav" / "Unknown Artist - song.flac").exists()  # 链接仅记录为计划
    assert [op.status for op in context.operations] == [OperationStatus.PLANNED]


def test_hardlink_finalize_removes_stale_playlist_dirs(tmp_path: Path) -> None:
    library = tmp_path / "library"
    stale = library / "old_playlist"
    stale.mkdir(parents=True)
    (stale / "x.flac").write_bytes(b"x")
    default_dir = library / "未分类"
    default_dir.mkdir()
    snapshot = _snapshot(Track(id=1, name="song", artists=[], album="", raw={}), (Playlist(1, "fav", ()),), ())
    context = _make_context(snapshot, tmp_path / "media_store")
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.finalize(context)
    assert not stale.exists()
    assert default_dir.exists()  # 未分类目录绝不被 finalize 删除


def test_hardlink_finalize_dry_run_keeps_stale_dirs(tmp_path: Path) -> None:
    library = tmp_path / "library"
    stale = library / "old_playlist"
    stale.mkdir(parents=True)
    (stale / "x.flac").write_bytes(b"x")
    snapshot = _snapshot(Track(id=1, name="song", artists=[], album="", raw={}), (Playlist(1, "fav", ()),), ())
    context = _make_context(snapshot, tmp_path / "media_store", dry_run=True)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.finalize(context)
    assert stale.exists()  # dry-run 不删除快照外目录


def test_register_builtin_presets_registers_archive() -> None:
    registry = PresetRegistry()
    register_builtin_presets(registry)
    assert {r.name for r in registry.preset_registrations()} == {"archive"}


def test_register_builtin_targets_registers_hardlink() -> None:
    registry = TargetRegistry()
    register_builtin_targets(registry, Path("library"))
    assert {r.name for r in registry.target_registrations()} == {"hardlink"}

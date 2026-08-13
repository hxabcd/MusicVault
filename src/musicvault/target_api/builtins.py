from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

from musicvault.domain.models import TargetDescriptor
from musicvault.shared.utils import audio_spec_key, format_track_name, safe_filename
from musicvault.target_api.v1 import TargetRegistration, TargetRegistry


class HardlinkDistributor:
    """按歌单目录硬链接分发指定 preset 的音频与歌词（按曲目幂等）。"""

    def __init__(self, preset: object, preset_name: str, target_root: Path, default_name: str = "未分类") -> None:
        self.preset = preset
        self.preset_name = preset_name
        self.target_root = Path(target_root)
        self.default_name = default_name
        self.filename_template = "{artist} - {name}"

    def prepare(self, _context) -> None:
        del _context
        return None

    def sync_item(self, track, context) -> None:
        spec_key = audio_spec_key(self.preset.format, self.preset.bitrate)
        asset = context.media_asset(track.id, spec=spec_key)
        if asset is None:
            return None
        lrc = context.lyrics_file(track.id, self.preset_name)
        owned_names = {safe_filename(pl.name) for pl in context.playlists if track.id in pl.track_ids}
        if not owned_names:
            owned_names = {safe_filename(self.default_name)}

        if not context.dry_run:
            # 删除类副作用不进入 OperationExecutor 记录，dry-run 下必须跳过。
            self._remove_stale_links(asset.path, lrc, owned_names)
        stem = format_track_name(self.filename_template, track)
        for dirname in owned_names:
            dst_dir = self.target_root / dirname
            if not context.dry_run:
                # 目录创建是副作用，dry-run 只计划链接（executor 的 PLANNED 语义）
                dst_dir.mkdir(parents=True, exist_ok=True)
            context.link(asset.path, dst_dir / f"{stem}{asset.path.suffix}")
            if lrc is not None:
                context.link(lrc, dst_dir / f"{stem}.lrc")
        return None

    def _remove_stale_links(self, audio_path: Path, lrc_path: Path | None, owned_names: set[str]) -> None:
        inodes = {inode for inode in (self._inode(audio_path), self._inode(lrc_path)) if inode is not None}
        if not inodes or not self.target_root.is_dir():
            return
        for child in self.target_root.iterdir():
            if not child.is_dir() or child.name in owned_names:
                continue
            for f in list(child.iterdir()):
                if f.is_file() and self._inode(f) in inodes:
                    f.unlink(missing_ok=True)

    @staticmethod
    def _inode(path: Path | None) -> tuple[int, int] | None:
        if path is None:
            return None
        try:
            st = path.stat()
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    def finalize(self, context) -> None:
        if context.dry_run:
            # 目录删除不进入 OperationExecutor 记录，dry-run 下必须跳过。
            return None
        snapshot_names = {safe_filename(pl.name) for pl in context.playlists}
        default = safe_filename(self.default_name)
        if not self.target_root.is_dir():
            return None
        for child in list(self.target_root.iterdir()):
            if child.is_dir() and child.name != default and child.name not in snapshot_names:
                shutil.rmtree(child, ignore_errors=True)
        return None


def register_builtin_targets(
    registry: TargetRegistry,
    target_root: str | Path,
    default_playlist_name: str = "未分类",
) -> None:
    target_root_path = Path(target_root)

    def hardlink_factory(presets: Mapping[str, object]):
        preset = presets["archive"]
        if not (hasattr(preset, "format") and hasattr(preset, "bitrate")):
            raise TypeError(f"hardlink 依赖的 'archive' preset 缺少 format/bitrate 声明：{type(preset).__name__}")
        return HardlinkDistributor(preset, "archive", target_root_path, default_playlist_name)

    registry.register_target(
        TargetRegistration(
            name="hardlink",
            factory=hardlink_factory,
            depends_on=("archive",),
            source="builtin:hardlink",
            target=TargetDescriptor(
                identifier="hardlink",
                target_type="filesystem",
                deletion_policy="append",
            ),
        )
    )

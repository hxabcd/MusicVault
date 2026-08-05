from __future__ import annotations

from pathlib import Path

from musicvault.domain.models import TargetDescriptor
from musicvault.preset_api.v1 import PresetContext, PresetRegistry, PresetRegistration
from musicvault.shared.utils import format_track_name, safe_filename


class PlaylistLinksSynchronizer:
    """用硬链接表达歌单关系的最小内置 TargetSynchronizer。"""

    def __init__(self, target_root: str | Path) -> None:
        self.target_root = Path(target_root)

    def prepare(self, context: PresetContext) -> None:
        return None

    def sync_item(self, track, context: PresetContext) -> None:
        asset = context.media_asset(track.id, asset_type="audio")
        if asset is None:
            return None
        for playlist in context.playlists:
            if track.id not in playlist.track_ids:
                continue
            stem = safe_filename(format_track_name("{artist} - {name}", track))
            destination = self.target_root / safe_filename(playlist.name) / f"{stem}{asset.path.suffix}"
            context.link(asset.path, destination)
        return None

    def finalize(self, context: PresetContext) -> None:
        return None


def register_builtin_presets(registry: PresetRegistry, target_root: str | Path) -> None:
    registry.register(
        PresetRegistration(
            name="playlist_links",
            factory=lambda: PlaylistLinksSynchronizer(target_root),
            source="builtin:playlist_links",
            target=TargetDescriptor(
                identifier="playlist_links",
                target_type="filesystem",
                # manifest 尚未实现，当前只追加/更新，避免误删用户文件。
                deletion_policy="append",
            ),
        )
    )

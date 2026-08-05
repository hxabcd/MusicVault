from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from musicvault.core.models import Track


@dataclass(frozen=True, slots=True)
class Playlist:
    """歌单及其在源侧的有序曲目关系。"""

    id: int
    name: str
    track_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "track_ids", tuple(int(track_id) for track_id in self.track_ids))


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """media_store 中一个可复用的媒体资产。"""

    track_id: int
    asset_type: str
    spec: str
    path: Path
    size: int | None = None
    sha256: str | None = None
    source: str | None = None
    updated_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        if not self.asset_type.strip():
            raise ValueError("媒体资产类型不能为空")
        if not self.spec.strip():
            raise ValueError("媒体资产规格不能为空")


@dataclass(frozen=True, slots=True)
class TargetDescriptor:
    """目标端的稳定描述，不包含具体适配器。"""

    identifier: str
    target_type: str = "filesystem"
    deletion_policy: str = "append"

    def __post_init__(self) -> None:
        if self.deletion_policy not in {"append", "managed", "mirror"}:
            raise ValueError(f"不支持的目标删除策略：{self.deletion_policy}")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """一次 sync 运行内共享的不可变源侧视图。"""

    tracks: tuple[Track, ...]
    playlists: tuple[Playlist, ...]
    media_assets: tuple[MediaAsset, ...]
    snapshot_hash: str

    @classmethod
    def from_data(
        cls,
        tracks: Iterable[Track],
        playlists: Iterable[Playlist],
        media_assets: Iterable[MediaAsset],
    ) -> "SourceSnapshot":
        copied_tracks = tuple(copy.deepcopy(track) for track in tracks)
        copied_playlists = tuple(sorted(playlists, key=lambda playlist: playlist.id))
        copied_assets = tuple(
            sorted(media_assets, key=lambda asset: (asset.track_id, asset.asset_type, asset.spec, str(asset.path)))
        )
        canonical = {
            "tracks": [_track_data(track) for track in sorted(copied_tracks, key=lambda item: item.id)],
            "playlists": [asdict(playlist) for playlist in copied_playlists],
            "media_assets": [_asset_data(asset) for asset in copied_assets],
        }
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(copied_tracks, copied_playlists, copied_assets, digest)

    def track(self, track_id: int) -> Track | None:
        return next((track for track in self.tracks if track.id == track_id), None)

    def playlist(self, playlist_id: int) -> Playlist | None:
        return next((playlist for playlist in self.playlists if playlist.id == playlist_id), None)

    def assets_for(self, track_id: int, asset_type: str | None = None) -> tuple[MediaAsset, ...]:
        return tuple(
            asset
            for asset in self.media_assets
            if asset.track_id == track_id and (asset_type is None or asset.asset_type == asset_type)
        )


def _track_data(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "name": track.name,
        "artists": list(track.artists),
        "album": track.album,
        "aliases": list(track.aliases),
        "cover_url": track.cover_url,
        "duration_ms": track.duration_ms,
        "raw": _json_safe(track.raw),
    }


def _asset_data(asset: MediaAsset) -> dict[str, Any]:
    result = asdict(asset)
    result["path"] = str(asset.path)
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ALIAS_SPLIT_RE = re.compile(r"[\/、;；]+")


@dataclass(slots=True)
class Track:
    """统一曲目模型"""

    # 统一曲目模型，屏蔽上游接口字段差异。
    id: int
    name: str
    artists: list[str]
    album: str
    aliases: list[str] = field(default_factory=list)
    cover_url: str | None = None
    duration_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def artist_text(self) -> str:
        """返回用于展示/文件名的歌手字符串"""
        return "/".join(self.artists) if self.artists else "Unknown Artist"

    @property
    def alias(self) -> str | None:
        """获取第一个别名"""
        return self.aliases[0] if self.aliases else None

    @staticmethod
    def _clean_metadata_text(value: str) -> str:
        # normalized = unicodedata.normalize("NFKC", value) 避免过度清理
        cleaned_chars: list[str] = []
        for ch in value:
            if ch in {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"}:
                continue
            if "\ufff0" <= ch <= "\uffff":
                continue
            if unicodedata.category(ch).startswith("C") and ch not in {"\n", "\r", "\t"}:
                continue
            cleaned_chars.append(ch)
        cleaned = "".join(cleaned_chars)
        compacted = re.sub(r"[^\S\r\n]+", " ", cleaned)
        return compacted.strip()

    @classmethod
    def from_ncm_payload(
        cls,
        payload: dict[str, Any],
        *,
        clean_text: bool = True,
        alias_split_re: re.Pattern[str] | None = None,
    ) -> "Track":
        """从网易云接口数据构建 Track"""
        split_re = alias_split_re or ALIAS_SPLIT_RE

        def clean(value: str) -> str:
            return cls._clean_metadata_text(value) if clean_text else value

        artists = payload.get("ar") or payload.get("artists") or []
        artist_names = [clean(a.get("name", "")) for a in artists if a.get("name")]
        aliases_raw = (payload.get("tns") or []) + (payload.get("alia") or [])
        aliases: list[str] = []
        for item in aliases_raw:
            text = clean(str(item))
            if not text:
                continue
            parts = [part.strip() for part in split_re.split(text)]
            for part in parts:
                if part and part not in aliases:
                    aliases.append(part)
        album = payload.get("al") or payload.get("album") or {}
        return cls(
            id=int(payload["id"]),
            name=clean(payload.get("name", f"track_{payload['id']}")),
            aliases=aliases,
            artists=artist_names,
            album=clean(album.get("name", "Unknown Album")),
            cover_url=album.get("picUrl"),
            duration_ms=payload.get("dt"),
            raw=payload,
        )


@dataclass(slots=True)
class DownloadedTrack:
    """下载阶段的文件与曲目信息"""

    # 表示下载产物及其来源信息，供解密与分流阶段使用。
    track: Track
    source_file: str
    is_ncm: bool
    playlist_ids: list[int] = field(default_factory=list)


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

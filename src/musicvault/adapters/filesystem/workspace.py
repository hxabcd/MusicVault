from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from musicvault.domain.models import Track
from musicvault.domain.models import MediaAsset, Playlist
from musicvault.shared.utils import same_file_content, sha256_file

if TYPE_CHECKING:
    from musicvault.adapters.state.sqlite import SQLiteStateRepository

_LEGACY_AUDIO_RE = re.compile(r"^(?P<track_id>\d+)(?:_(?P<spec>[^.]+))?(?P<suffix>\.[^.]+)$")
_AUDIO_FORMATS = {".flac": "FLAC", ".mp3": "MP3", ".m4a": "AAC", ".ogg": "OGG", ".opus": "OPUS", ".wav": "WAV"}


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """workspace 各生命周期区域的路径。"""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def media_store(self) -> Path:
        return self.root / "media_store"

    @property
    def library(self) -> Path:
        return self.root / "library"

    @property
    def state_db(self) -> Path:
        return self.root / "state.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def legacy_downloads(self) -> Path:
        return self.root / "downloads"

    def ensure(self) -> None:
        for path in (self.root, self.cache, self.media_store, self.library, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def media_asset_path(self, track_id: int, asset_type: str, filename: str) -> Path:
        return self.media_store / str(track_id) / asset_type / filename


@dataclass(frozen=True, slots=True)
class MigrationReport:
    copied_assets: int = 0
    skipped_assets: int = 0
    ignored_files: int = 0
    copied_cache_files: int = 0
    skipped_cache_files: int = 0
    imported_tracks: int = 0
    imported_playlists: int = 0


class WorkspaceMigration:
    """将旧 downloads 根目录中的 canonical 音频安全复制到 media_store。

    默认保留旧文件作为可恢复备份；重复执行只跳过内容相同的目标，不覆盖已存在的不同内容。
    """

    def __init__(self, paths: WorkspacePaths, state: SQLiteStateRepository | None = None) -> None:
        self.paths = paths
        self.state = state

    def migrate(self) -> MigrationReport:
        self.paths.ensure()
        legacy = self.paths.legacy_downloads

        copied = skipped = ignored = imported_tracks = imported_playlists = 0
        copied_cache, skipped_cache = self._migrate_cache()
        legacy_files = sorted(legacy.iterdir()) if legacy.is_dir() else ()
        for source in legacy_files:
            if not source.is_file():
                ignored += 1
                continue
            match = _LEGACY_AUDIO_RE.match(source.name)
            if not match or source.suffix.lower() not in _AUDIO_FORMATS:
                ignored += 1
                continue
            track_id = int(match.group("track_id"))
            fmt = _AUDIO_FORMATS[source.suffix.lower()]
            suffix_spec = match.group("spec")
            spec = f"{fmt}-{suffix_spec}" if suffix_spec else fmt
            destination = self.paths.media_asset_path(track_id, "audio", source.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if same_file_content(source, destination):
                    skipped += 1
                else:
                    raise FileExistsError(f"迁移目标已存在且内容不同，未覆盖：{destination}")
            else:
                shutil.copy2(source, destination)
                copied += 1
            if self.state is not None:
                imported_tracks += self._register_asset(track_id, spec, destination)
        imported_playlists = self._import_legacy_state()
        return MigrationReport(
            copied_assets=copied,
            skipped_assets=skipped,
            ignored_files=ignored,
            copied_cache_files=copied_cache,
            skipped_cache_files=skipped_cache,
            imported_tracks=imported_tracks,
            imported_playlists=imported_playlists,
        )

    def _register_asset(self, track_id: int, spec: str, path: Path) -> int:
        assert self.state is not None
        track = self.state.get_track(track_id)
        imported = 0
        if track is None:
            self.state.upsert_track(
                Track(id=track_id, name=str(track_id), artists=[], album="Unknown Album", raw={"migrated": True})
            )
            imported = 1
        self.state.upsert_media_asset(
            MediaAsset(
                track_id=track_id,
                asset_type="audio",
                spec=spec,
                path=path,
                size=path.stat().st_size,
                sha256=sha256_file(path),
                source="legacy:downloads",
            )
        )
        return imported

    def _migrate_cache(self) -> tuple[int, int]:
        legacy_cache = self.paths.legacy_downloads / "cache"
        if not legacy_cache.is_dir():
            return 0, 0
        copied = skipped = 0
        for source in sorted(item for item in legacy_cache.rglob("*") if item.is_file()):
            relative = source.relative_to(legacy_cache)
            destination = self.paths.cache / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if same_file_content(source, destination):
                    skipped += 1
                    continue
                raise FileExistsError(f"缓存迁移目标已存在且内容不同，未覆盖：{destination}")
            shutil.copy2(source, destination)
            copied += 1
        return copied, skipped

    def _import_legacy_state(self) -> int:
        if self.state is None:
            return 0
        state_dir = self.paths.root / "state"
        synced_path = state_dir / "synced_tracks.json"
        playlists_path = state_dir / "playlists.json"
        songs_path = state_dir / "songs.json"
        if not synced_path.exists() and not playlists_path.exists() and not songs_path.exists():
            return 0
        synced_raw = _read_json(synced_path, {}).get("ids", {})
        if isinstance(synced_raw, list):
            synced: dict[int, list[int]] = {int(track_id): [] for track_id in synced_raw}
        elif isinstance(synced_raw, dict):
            synced = {
                int(track_id): [int(pid) for pid in playlist_ids] for track_id, playlist_ids in synced_raw.items()
            }
        else:
            synced = {}
        songs_raw = _read_json(songs_path, {}).get("ids", [])
        song_ids = {int(track_id) for track_id in songs_raw} if isinstance(songs_raw, list) else set()
        for track_id in set(synced) | song_ids:
            if self.state.get_track(track_id) is None:
                self.state.upsert_track(
                    Track(id=track_id, name=str(track_id), artists=[], album="Unknown Album", raw={"migrated": True})
                )
            self.state.add_managed_song(track_id)

        playlist_raw = _read_json(playlists_path, {})
        imported = 0
        if isinstance(playlist_raw, dict):
            for playlist_id, entry in playlist_raw.items():
                if not str(playlist_id).lstrip("-").isdigit() or not isinstance(entry, dict):
                    continue
                pid = int(playlist_id)
                track_ids = tuple(track_id for track_id, pids in synced.items() if pid in pids)
                self.state.upsert_playlist(Playlist(pid, str(entry.get("name") or pid), track_ids))
                imported += 1
        return imported


def _read_json(path: Path, default: object) -> dict[str, object]:
    import json

    if not path.exists():
        return default if isinstance(default, dict) else {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default if isinstance(default, dict) else {}
    return value if isinstance(value, dict) else {}

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from musicvault.domain.models import Track
from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot

SCHEMA_VERSION = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    artists_json TEXT NOT NULL,
    album TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    cover_url TEXT,
    duration_ms INTEGER,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    track_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id),
    UNIQUE (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS managed_songs (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_assets (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL,
    spec TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    sha256 TEXT,
    source TEXT,
    updated_at REAL,
    PRIMARY KEY (track_id, asset_type, spec)
);

CREATE TABLE IF NOT EXISTS preset_registry (
    name TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    api_version TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    script_hash TEXT
);

CREATE TABLE IF NOT EXISTS export_targets (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    deletion_policy TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_tracks (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    preset_hash TEXT NOT NULL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS pending_files (
    path TEXT PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lyrics (
    track_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""

# v2：preset_registry 加 kind 列（preset/target 两类注册；旧行默认 'target' 兼容）。
# 注意：v1 建表 SQL 保持原样（新库也经 v1 建表 → v2 补列，避免迁移链重复加列）。
_MIGRATION_V2 = "ALTER TABLE preset_registry ADD COLUMN kind TEXT NOT NULL DEFAULT 'target';"

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_SQL, 2: _MIGRATION_V2}


@dataclass(frozen=True, slots=True)
class RegisteredPreset:
    """preset_registry 表的只读查询结果。"""

    name: str
    source: str
    api_version: str
    enabled: bool
    script_hash: str | None
    kind: str = "target"


class SQLiteState:
    """SQLite 数据库连接与顺序迁移管理。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """在事务内初始化或升级 schema；失败时保留 SQLite 的原子回滚语义。"""
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"数据库版本 {current} 高于当前程序支持的版本 {SCHEMA_VERSION}")
            for version in range(current + 1, SCHEMA_VERSION + 1):
                connection.executescript(_MIGRATIONS[version])
                connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))


class SQLiteStateRepository:
    """隔离 SQL 的最小状态 Repository。"""

    def __init__(self, database: SQLiteState) -> None:
        self.database = database
        self.database.initialize()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.database.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save_source_state(
        self,
        tracks: list[Track] | tuple[Track, ...],
        playlists: list[Playlist] | tuple[Playlist, ...],
        media_assets: list[MediaAsset] | tuple[MediaAsset, ...],
    ) -> None:
        with self.transaction() as connection:
            for track in tracks:
                self._upsert_track(track, connection)
            for playlist in playlists:
                self._upsert_playlist(playlist, connection)
                self._set_playlist_tracks(playlist.id, playlist.track_ids, connection)
            for asset in media_assets:
                self._upsert_media_asset(asset, connection)

    def upsert_track(self, track: Track, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None:
            with self.transaction() as owned:
                self._upsert_track(track, owned)
        else:
            self._upsert_track(track, connection)

    @staticmethod
    def _upsert_track(track: Track, connection: sqlite3.Connection) -> None:
        connection.execute(
            """INSERT INTO tracks
               (id, name, artists_json, album, aliases_json, cover_url, duration_ms, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, artists_json=excluded.artists_json, album=excluded.album,
                 aliases_json=excluded.aliases_json, cover_url=excluded.cover_url,
                 duration_ms=excluded.duration_ms, raw_json=excluded.raw_json""",
            (
                track.id,
                track.name,
                _json(track.artists),
                track.album,
                _json(track.aliases),
                track.cover_url,
                track.duration_ms,
                _json(track.raw),
            ),
        )

    def get_track(self, track_id: int) -> Track | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            return None
        return _track_from_row(row)

    def list_tracks(self) -> list[Track]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM tracks ORDER BY id").fetchall()
        return [_track_from_row(row) for row in rows]

    def upsert_playlist(self, playlist: Playlist, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None:
            with self.transaction() as owned:
                self._upsert_playlist(playlist, owned)
                self._set_playlist_tracks(playlist.id, playlist.track_ids, owned)
        else:
            self._upsert_playlist(playlist, connection)
            self._set_playlist_tracks(playlist.id, playlist.track_ids, connection)

    @staticmethod
    def _upsert_playlist(playlist: Playlist, connection: sqlite3.Connection) -> None:
        connection.execute(
            """INSERT INTO playlists(id, name, track_count) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, track_count=excluded.track_count""",
            (playlist.id, playlist.name, len(playlist.track_ids)),
        )

    @staticmethod
    def _set_playlist_tracks(playlist_id: int, track_ids: tuple[int, ...], connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        for position, track_id in enumerate(track_ids):
            connection.execute(
                "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?, ?, ?)",
                (playlist_id, track_id, position),
            )

    def list_playlists(self) -> list[Playlist]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT id, name FROM playlists ORDER BY id").fetchall()
            result = []
            for row in rows:
                track_rows = connection.execute(
                    "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
                    (row["id"],),
                ).fetchall()
                result.append(Playlist(row["id"], row["name"], tuple(item["track_id"] for item in track_rows)))
        return result

    def get_playlist(self, playlist_id: int) -> Playlist | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT id, name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            if row is None:
                return None
            track_rows = connection.execute(
                "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            ).fetchall()
        return Playlist(row["id"], row["name"], tuple(item["track_id"] for item in track_rows))

    def add_managed_song(self, track_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None:
            with self.transaction() as owned:
                owned.execute("INSERT OR IGNORE INTO managed_songs(track_id) VALUES (?)", (track_id,))
        else:
            connection.execute("INSERT OR IGNORE INTO managed_songs(track_id) VALUES (?)", (track_id,))

    def has_managed_song(self, track_id: int) -> bool:
        with self.database.connect() as connection:
            row = connection.execute("SELECT 1 FROM managed_songs WHERE track_id = ?", (track_id,)).fetchone()
        return row is not None

    def list_managed_songs(self) -> list[int]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT track_id FROM managed_songs ORDER BY track_id").fetchall()
        return [int(row["track_id"]) for row in rows]

    def remove_managed_song(self, track_id: int) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM managed_songs WHERE track_id = ?", (track_id,))

    def remove_track(self, track_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        """删除曲目及其级联关系（playlist_tracks / managed_songs / media_assets / lyrics）。"""
        if connection is None:
            with self.transaction() as owned:
                owned.execute("DELETE FROM lyrics WHERE track_id = ?", (track_id,))
                owned.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        else:
            connection.execute("DELETE FROM lyrics WHERE track_id = ?", (track_id,))
            connection.execute("DELETE FROM tracks WHERE id = ?", (track_id,))

    def remove_playlist(self, playlist_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        """删除歌单及其曲目关系（playlist_tracks 级联）。"""
        if connection is None:
            with self.transaction() as owned:
                owned.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        else:
            connection.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    def upsert_media_asset(self, asset: MediaAsset, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None:
            with self.transaction() as owned:
                self._upsert_media_asset(asset, owned)
        else:
            self._upsert_media_asset(asset, connection)

    @staticmethod
    def _upsert_media_asset(asset: MediaAsset, connection: sqlite3.Connection) -> None:
        connection.execute(
            """INSERT INTO media_assets
               (track_id, asset_type, spec, path, size, sha256, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(track_id, asset_type, spec) DO UPDATE SET
                 path=excluded.path, size=excluded.size, sha256=excluded.sha256,
                 source=excluded.source, updated_at=excluded.updated_at""",
            (
                asset.track_id,
                asset.asset_type,
                asset.spec,
                str(asset.path),
                asset.size,
                asset.sha256,
                asset.source,
                asset.updated_at,
            ),
        )

    def list_media_assets(self, track_id: int | None = None) -> list[MediaAsset]:
        with self.database.connect() as connection:
            if track_id is None:
                rows = connection.execute("SELECT * FROM media_assets ORDER BY track_id, asset_type, spec").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM media_assets WHERE track_id = ? ORDER BY asset_type, spec", (track_id,)
                ).fetchall()
        return [_asset_from_row(row) for row in rows]

    def register_preset(
        self,
        name: str,
        source: str,
        api_version: str,
        enabled: bool = True,
        script_hash: str | None = None,
        *,
        kind: str = "target",
        connection: sqlite3.Connection | None = None,
    ) -> None:
        values = (name, source, api_version, int(enabled), script_hash, kind)
        sql = """INSERT INTO preset_registry(name, source, api_version, enabled, script_hash, kind)
                 VALUES (?, ?, ?, ?, ?, ?)
                 ON CONFLICT(name) DO UPDATE SET source=excluded.source,
                 api_version=excluded.api_version, enabled=excluded.enabled,
                 script_hash=excluded.script_hash, kind=excluded.kind"""
        if connection is None:
            with self.transaction() as owned:
                owned.execute(sql, values)
        else:
            connection.execute(sql, values)

    def list_registered_presets(self) -> list[RegisteredPreset]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM preset_registry ORDER BY name").fetchall()
        return [
            RegisteredPreset(
                name=str(row["name"]),
                source=str(row["source"]),
                api_version=str(row["api_version"]),
                enabled=bool(row["enabled"]),
                script_hash=row["script_hash"],
                kind=str(row["kind"]),
            )
            for row in rows
        ]

    def register_target(
        self,
        identifier: str,
        target_type: str,
        deletion_policy: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        if deletion_policy not in {"append", "managed", "mirror"}:
            raise ValueError(f"不支持的目标删除策略：{deletion_policy}")
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO export_targets(id, type, deletion_policy, config_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET type=excluded.type,
                   deletion_policy=excluded.deletion_policy, config_json=excluded.config_json""",
                (identifier, target_type, deletion_policy, _json(config or {})),
            )

    def create_snapshot(self) -> SourceSnapshot:
        # 所有实体从同一连接读取，避免快照在三个查询之间观察到部分写入。
        with self.transaction() as connection:
            connection.execute("BEGIN")
            tracks = self._list_tracks(connection)
            playlists = self._list_playlists(connection)
            assets = self._list_media_assets(connection)
        return SourceSnapshot.from_data(tracks, playlists, assets)

    # -- processed_tracks / pending_files（替代旧 processed_files.json） --

    def record_processed(self, track_id: int, preset_hash: str, updated_at: float) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO processed_tracks(track_id, preset_hash, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                     preset_hash=excluded.preset_hash, updated_at=excluded.updated_at""",
                (track_id, preset_hash, updated_at),
            )

    def is_processed(self, track_id: int, required_specs: set[str]) -> bool:
        """track 已覆盖全部必需 spec 且存在处理记录时返回 True。"""
        with self.transaction() as connection:
            row = connection.execute("SELECT 1 FROM processed_tracks WHERE track_id = ?", (track_id,)).fetchone()
            if row is None:
                return False
            rows = connection.execute(
                "SELECT spec FROM media_assets WHERE track_id = ? AND asset_type = 'audio'",
                (track_id,),
            ).fetchall()
        covered = {row["spec"] for row in rows}
        return required_specs <= covered

    def add_pending_file(self, path: str, track_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO pending_files(path, track_id) VALUES (?, ?)",
                (path, track_id),
            )

    def find_track_id_by_path(self, path: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT track_id FROM pending_files WHERE path = ?", (path,)).fetchone()
        return int(row["track_id"]) if row is not None else None

    def list_pending_track_ids(self) -> list[int]:
        """列出已有待处理 raw 文件的 track_id（表示该曲目已下载但尚未处理完）。"""
        with self.database.connect() as connection:
            rows = connection.execute("SELECT DISTINCT track_id FROM pending_files ORDER BY track_id").fetchall()
        return [int(row["track_id"]) for row in rows]

    # -- lyrics（源端歌词原稿，按 track 一行 upsert） --

    def save_lyrics(
        self,
        track_id: int,
        payload: str,
        fetched_at: float,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        sql = (
            "INSERT INTO lyrics (track_id, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(track_id) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at"
        )
        if connection is None:
            with self.transaction() as owned:
                owned.execute(sql, (track_id, payload, fetched_at))
        else:
            connection.execute(sql, (track_id, payload, fetched_at))

    def get_lyrics(self, track_id: int) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT payload FROM lyrics WHERE track_id = ?", (track_id,)).fetchone()
        return str(row["payload"]) if row is not None else None

    def _list_tracks(self, connection: sqlite3.Connection) -> list[Track]:
        rows = connection.execute("SELECT * FROM tracks ORDER BY id").fetchall()
        return [_track_from_row(row) for row in rows]

    def _list_playlists(self, connection: sqlite3.Connection) -> list[Playlist]:
        rows = connection.execute("SELECT id, name FROM playlists ORDER BY id").fetchall()
        result = []
        for row in rows:
            track_rows = connection.execute(
                "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
                (row["id"],),
            ).fetchall()
            result.append(Playlist(row["id"], row["name"], tuple(item["track_id"] for item in track_rows)))
        return result

    def _list_media_assets(self, connection: sqlite3.Connection) -> list[MediaAsset]:
        rows = connection.execute("SELECT * FROM media_assets ORDER BY track_id, asset_type, spec").fetchall()
        return [_asset_from_row(row) for row in rows]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _track_from_row(row: sqlite3.Row) -> Track:
    return Track(
        id=int(row["id"]),
        name=str(row["name"]),
        artists=list(json.loads(row["artists_json"])),
        album=str(row["album"]),
        aliases=list(json.loads(row["aliases_json"])),
        cover_url=row["cover_url"],
        duration_ms=row["duration_ms"],
        raw=dict(json.loads(row["raw_json"])),
    )


def _asset_from_row(row: sqlite3.Row) -> MediaAsset:
    return MediaAsset(
        track_id=int(row["track_id"]),
        asset_type=str(row["asset_type"]),
        spec=str(row["spec"]),
        path=Path(row["path"]),
        size=row["size"],
        sha256=row["sha256"],
        source=row["source"],
        updated_at=row["updated_at"],
    )

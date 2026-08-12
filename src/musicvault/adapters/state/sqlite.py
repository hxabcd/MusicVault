"""SQLite 状态适配器：源侧状态与处理管线状态两个 Repository。

表结构（单版本、无迁移链；旧格式库检测后拒绝初始化，见 SQLiteState.initialize）：
- tracks / playlists / playlist_tracks / managed_tracks：源侧元数据与关系
- media_assets：媒体资产；歌词原稿行 asset_type='lyrics'、data_json 存统一歌词格式 payload
- processing_state：处理管线状态（downloaded → processed）
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, Track

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
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id),
    UNIQUE (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS managed_tracks (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_assets (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL,
    spec TEXT NOT NULL,
    path TEXT,
    size INTEGER,
    sha256 TEXT,
    source TEXT,
    updated_at REAL,
    data_json TEXT,
    PRIMARY KEY (track_id, asset_type, spec),
    CHECK (asset_type = 'lyrics' OR path IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS processing_state (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('downloaded', 'processed')),
    raw_path TEXT,
    updated_at REAL NOT NULL
);
"""

# 旧格式（迁移链时代）业务表：新库若存在任一旧表名则拒绝初始化，避免新旧表混杂。
_LEGACY_TABLE_NAMES = frozenset(
    {
        "preset_registry",
        "export_targets",
        "lyrics",
        "processed_tracks",
        "pending_files",
        "managed_songs",
        "schema_version",
    }
)

_LYRICS_ASSET_TYPE = "lyrics"
_LYRICS_SPEC = "unified"
_LYRICS_SOURCE = "netease"


class SQLiteState:
    """SQLite 数据库连接与建库；检测旧格式库。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """幂等建库；旧格式库抛 RuntimeError，提示用户删除后重建。"""
        with self.connect() as connection:
            existing = {
                row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            legacy = sorted(existing & _LEGACY_TABLE_NAMES)
            if legacy:
                raise RuntimeError(
                    f"检测到旧格式数据库 {self.path}（旧表：{', '.join(legacy)}），"
                    "本版本不再迁移旧数据，请删除该文件后重新运行"
                )
            connection.executescript(_SCHEMA_SQL)


@contextmanager
def _transaction(database: SQLiteState) -> Generator[sqlite3.Connection]:
    connection = database.connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class SQLiteSourceStateRepository:
    """源侧状态（曲目/歌单/管理标记/媒体资产/歌词原稿）Repository。"""

    def __init__(self, database: SQLiteState) -> None:
        self.database = database
        self.database.initialize()

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return _transaction(self.database)

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

    def remove_track(self, track_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        """删除曲目及其级联关系（playlist_tracks / managed_tracks / media_assets / processing_state）。"""
        if connection is None:
            with self.transaction() as owned:
                owned.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        else:
            connection.execute("DELETE FROM tracks WHERE id = ?", (track_id,))

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
            "INSERT INTO playlists(id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (playlist.id, playlist.name),
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

    def remove_playlist(self, playlist_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        """删除歌单及其曲目关系（playlist_tracks 级联）。"""
        if connection is None:
            with self.transaction() as owned:
                owned.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        else:
            connection.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    def add_managed_track(self, track_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None:
            with self.transaction() as owned:
                owned.execute("INSERT OR IGNORE INTO managed_tracks(track_id) VALUES (?)", (track_id,))
        else:
            connection.execute("INSERT OR IGNORE INTO managed_tracks(track_id) VALUES (?)", (track_id,))

    def has_managed_track(self, track_id: int) -> bool:
        with self.database.connect() as connection:
            row = connection.execute("SELECT 1 FROM managed_tracks WHERE track_id = ?", (track_id,)).fetchone()
        return row is not None

    def list_managed_tracks(self) -> list[int]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT track_id FROM managed_tracks ORDER BY track_id").fetchall()
        return [int(row["track_id"]) for row in rows]

    def remove_managed_track(self, track_id: int) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM managed_tracks WHERE track_id = ?", (track_id,))

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
               (track_id, asset_type, spec, path, size, sha256, source, updated_at, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
                rows = connection.execute(
                    "SELECT * FROM media_assets WHERE asset_type <> ? ORDER BY track_id, asset_type, spec",
                    (_LYRICS_ASSET_TYPE,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM media_assets WHERE track_id = ? AND asset_type <> ? ORDER BY asset_type, spec",
                    (track_id, _LYRICS_ASSET_TYPE),
                ).fetchall()
        return [_asset_from_row(row) for row in rows]

    def save_lyrics(
        self,
        track_id: int,
        payload: str,
        fetched_at: float,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """歌词原稿以 media_assets 行存储（asset_type='lyrics'，data_json 存统一歌词格式 payload）。"""
        sql = (
            "INSERT INTO media_assets "
            "(track_id, asset_type, spec, path, size, sha256, source, updated_at, data_json) "
            "VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?) "
            "ON CONFLICT(track_id, asset_type, spec) DO UPDATE SET "
            "source=excluded.source, updated_at=excluded.updated_at, data_json=excluded.data_json"
        )
        if connection is None:
            with self.transaction() as owned:
                owned.execute(sql, (track_id, _LYRICS_ASSET_TYPE, _LYRICS_SPEC, _LYRICS_SOURCE, fetched_at, payload))
        else:
            connection.execute(sql, (track_id, _LYRICS_ASSET_TYPE, _LYRICS_SPEC, _LYRICS_SOURCE, fetched_at, payload))

    def get_lyrics(self, track_id: int) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM media_assets WHERE track_id = ? AND asset_type = ?",
                (track_id, _LYRICS_ASSET_TYPE),
            ).fetchone()
        return str(row["data_json"]) if row is not None else None

    def create_snapshot(self) -> SourceSnapshot:
        # 所有实体从同一连接读取，避免快照在三个查询之间观察到部分写入。
        with self.transaction() as connection:
            tracks = self._list_tracks(connection)
            playlists = self._list_playlists(connection)
            assets = self._list_media_assets(connection)
        return SourceSnapshot.from_data(tracks, playlists, assets)

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
        rows = connection.execute(
            "SELECT * FROM media_assets WHERE asset_type <> ? ORDER BY track_id, asset_type, spec",
            (_LYRICS_ASSET_TYPE,),
        ).fetchall()
        return [_asset_from_row(row) for row in rows]


class SQLiteProcessStateRepository:
    """处理管线状态（downloaded → processed）Repository。"""

    def __init__(self, database: SQLiteState) -> None:
        self.database = database
        self.database.initialize()

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return _transaction(self.database)

    def mark_downloaded(self, path: str, track_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO processing_state(track_id, state, raw_path, updated_at)
                   VALUES (?, 'downloaded', ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                     state='downloaded', raw_path=excluded.raw_path, updated_at=excluded.updated_at""",
                (track_id, path, time.time()),
            )

    def list_downloaded_track_ids(self) -> list[int]:
        """列出已下载但尚未处理完成的 track_id。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT track_id FROM processing_state WHERE state = 'downloaded' ORDER BY track_id"
            ).fetchall()
        return [int(row["track_id"]) for row in rows]

    def find_track_id_by_path(self, path: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT track_id FROM processing_state WHERE raw_path = ?", (path,)).fetchone()
        return int(row["track_id"]) if row is not None else None

    def mark_processed(self, track_id: int, updated_at: float) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO processing_state(track_id, state, raw_path, updated_at)
                   VALUES (?, 'processed', NULL, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                     state='processed', raw_path=NULL, updated_at=excluded.updated_at""",
                (track_id, updated_at),
            )

    def is_processed(self, track_id: int, required_specs: set[str]) -> bool:
        """track 状态为 processed 且 media_assets 覆盖全部必需 spec 时返回 True。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processing_state WHERE track_id = ? AND state = 'processed'", (track_id,)
            ).fetchone()
            if row is None:
                return False
            rows = connection.execute(
                "SELECT spec FROM media_assets WHERE track_id = ? AND asset_type = 'audio'",
                (track_id,),
            ).fetchall()
        covered = {row["spec"] for row in rows}
        return required_specs <= covered


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

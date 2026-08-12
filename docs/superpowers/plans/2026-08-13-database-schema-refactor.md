# SQLite 表结构职责化重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写 `state.db` 表结构使其职责清晰（源侧状态 / 处理管线状态两类、一张表一个职责），废弃迁移链，删除死表与只写不读的注册镜像表。

**Architecture:** 单 `state.db` 六张业务表（tracks / playlists / playlist_tracks / managed_tracks / media_assets / processing_state）；`SQLiteState` 负责连接、建库与旧库检测；Repository 拆为 `SQLiteSourceStateRepository` + `SQLiteProcessStateRepository` 两个类，对应两个新端口 Protocol；application 用例按需注入一个或两个端口。

**Tech Stack:** Python 3.12、sqlite3 标准库、pytest、ruff（line-length=120）。

**Spec:** [docs/superpowers/specs/2026-08-13-database-schema-refactor-design.md](../specs/2026-08-13-database-schema-refactor-design.md)

## Global Constraints

- 一律使用中文回答用户、书写注释、docstring 与 commit message；领域术语遵循 `CONTEXT.md` 术语表（曲目（Track）、目标端（Target）、源快照（Source Snapshot）等）。
- 依赖方向固定：`ports/` 只描述业务能力（Protocol），`adapters/` 只依赖 domain，不允许反向依赖（`tests/test_architecture.py` 守护）。
- 破坏性重建：无迁移链，旧格式库（含 `preset_registry` 等旧表）检测后抛 `RuntimeError` 提示删除，不做数据迁移。
- 测试命令：`uv python -m pytest tests/ -q`；lint：`uv python -m ruff check src/ tests/`、`uv python -m ruff format --check src/ tests/`。
- 每个任务结束时运行该任务列出的验证命令，全部通过后再提交；任务间允许其余测试暂时失败（迁移期红），Task 5 必须全量绿。

---

### Task 1: SQLite 适配器重写（新 schema、旧库检测、双 Repository）

**Files:**
- Modify: `src/musicvault/adapters/state/sqlite.py`（整体重写）
- Modify: `tests/test_sqlite_state.py`（整体重写）
- Modify: `tests/test_sqlite_more.py`（删 preset/target 用例，换源侧 repo）
- Modify: `tests/test_state_lyrics.py`（换源侧 repo + 新增歌词行隐藏断言）

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces:
  - `class SQLiteState(path)`：`connect() -> sqlite3.Connection`、`initialize() -> None`（旧库抛 `RuntimeError`，消息含「旧格式数据库」）
  - `class SQLiteSourceStateRepository(database: SQLiteState)`：`save_source_state(tracks, playlists, media_assets)`、`create_snapshot() -> SourceSnapshot`、`get_track(track_id) -> Track | None`、`upsert_track(track, *, connection=None)`、`remove_track(track_id, *, connection=None)`、`upsert_playlist(playlist, *, connection=None)`、`get_playlist(playlist_id) -> Playlist | None`、`list_playlists() -> list[Playlist]`、`remove_playlist(playlist_id, *, connection=None)`、`add_managed_track(track_id, *, connection=None)`、`has_managed_track(track_id) -> bool`、`list_managed_tracks() -> list[int]`、`remove_managed_track(track_id)`、`upsert_media_asset(asset, *, connection=None)`、`list_media_assets(track_id=None) -> list[MediaAsset]`、`save_lyrics(track_id, payload, fetched_at, *, connection=None)`、`get_lyrics(track_id) -> str | None`、`transaction()`（contextmanager）
  - `class SQLiteProcessStateRepository(database: SQLiteState)`：`mark_downloaded(path, track_id)`、`list_downloaded_track_ids() -> list[int]`、`find_track_id_by_path(path) -> int | None`、`mark_processed(track_id, updated_at)`、`is_processed(track_id, required_specs: set[str]) -> bool`、`transaction()`（contextmanager）
  - 删除导出：`SCHEMA_VERSION`、`RegisteredPreset`、`SQLiteStateRepository`、`register_preset`、`list_registered_presets`、`register_target`、`record_processed`、`add_pending_file`、`list_pending_track_ids`、`add_managed_song` 系列（旧名）

- [ ] **Step 1: 重写测试文件 `tests/test_sqlite_state.py`**

删除全部旧内容，写入（注意 `mark_downloaded` 依赖 tracks 外键，先 `upsert_track`）：

```python
from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.state.sqlite import (
    SQLiteProcessStateRepository,
    SQLiteSourceStateRepository,
    SQLiteState,
)
from musicvault.domain.models import MediaAsset, Playlist, Track


def _track(track_id: int = 1) -> Track:
    return Track(
        id=track_id,
        name=f"曲目 {track_id}",
        artists=["歌手"],
        album="专辑",
        aliases=["别名"],
        raw={"source": "test"},
    )


def _source_repo(tmp_path: Path) -> SQLiteSourceStateRepository:
    return SQLiteSourceStateRepository(SQLiteState(tmp_path / "state.db"))


def test_initialize_creates_new_schema(tmp_path: Path) -> None:
    database = SQLiteState(tmp_path / "state.db")

    database.initialize()

    with database.connect() as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert {
        "tracks",
        "playlists",
        "playlist_tracks",
        "managed_tracks",
        "media_assets",
        "processing_state",
    } == tables


def test_initialize_rejects_legacy_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with SQLiteState(path).connect() as connection:
        connection.execute("CREATE TABLE preset_registry (name TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="旧格式数据库"):
        SQLiteState(path).initialize()


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = SQLiteState(tmp_path / "state.db")

    database.initialize()
    database.initialize()

    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count == 0


def test_repository_round_trip_and_snapshot_are_atomic(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    track = _track()
    playlist = Playlist(id=10, name="歌单", track_ids=(track.id,))
    asset = MediaAsset(
        track_id=track.id,
        asset_type="audio",
        spec="FLAC",
        path=tmp_path / "media_store" / "1" / "audio" / "1.flac",
        sha256="abc",
    )

    repo.save_source_state([track], [playlist], [asset])

    loaded = repo.get_track(track.id)
    snapshot = repo.create_snapshot()
    assert loaded is not None
    assert loaded.name == track.name
    assert snapshot.tracks[0].id == track.id
    assert snapshot.playlists[0].track_ids == (track.id,)
    assert snapshot.media_assets[0].sha256 == "abc"
    assert snapshot.snapshot_hash

    with pytest.raises(RuntimeError), repo.transaction() as connection:
        repo.upsert_track(_track(2), connection=connection)
        raise RuntimeError("模拟事务回滚")
    assert repo.get_track(2) is None


def test_unique_media_asset_is_replaced_not_duplicated(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    first = MediaAsset(track_id=1, asset_type="audio", spec="MP3-192k", path=tmp_path / "a.mp3")
    second = MediaAsset(track_id=1, asset_type="audio", spec="MP3-192k", path=tmp_path / "b.mp3")

    repo.upsert_track(_track())
    repo.upsert_media_asset(first)
    repo.upsert_media_asset(second)

    assets = repo.list_media_assets(track_id=1)
    assert len(assets) == 1
    assert assets[0].path == tmp_path / "b.mp3"


def test_lyrics_rows_are_hidden_from_media_assets(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    repo.save_lyrics(42, "[]", 0.0)

    assert repo.list_media_assets(track_id=42) == []
    assert repo.get_lyrics(42) == "[]"


def test_transaction_rollback_keeps_prior_committed_state(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    repo.upsert_track(_track(1))

    with pytest.raises(RuntimeError), repo.transaction() as connection:
        repo.upsert_track(_track(2), connection=connection)
        raise RuntimeError("模拟回滚")

    assert repo.get_track(1) is not None
    assert repo.get_track(2) is None


def test_deleting_playlist_cascades_playlist_tracks(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    repo.save_source_state([_track()], [Playlist(id=10, name="歌单", track_ids=(1,))], [])

    with repo.database.connect() as connection:
        connection.execute("DELETE FROM playlists WHERE id = 10")

    with repo.database.connect() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = 10").fetchone()[0]
    assert remaining == 0


def test_is_processed_requires_processed_state_and_spec_coverage(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    process = SQLiteProcessStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))
    repo.upsert_media_asset(MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=tmp_path / "1.flac"))
    process.mark_processed(1, 0.0)

    assert process.is_processed(1, {"FLAC"})
    # spec 未覆盖 → 未处理
    assert not process.is_processed(1, {"FLAC", "MP3-192k"})
    # 无处理状态 → 未处理
    repo.upsert_track(_track(2))
    assert not process.is_processed(2, {"FLAC"})


def test_downloaded_state_transitions_to_processed(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    process = SQLiteProcessStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))

    process.mark_downloaded("cache/1.mp3", 1)
    assert process.list_downloaded_track_ids() == [1]
    assert process.find_track_id_by_path("cache/1.mp3") == 1
    assert not process.is_processed(1, set())

    process.mark_processed(1, 0.0)
    assert process.list_downloaded_track_ids() == []
    assert process.find_track_id_by_path("cache/1.mp3") is None
    assert process.is_processed(1, set())


def test_remove_track_cascades_processing_state_and_lyrics(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    process = SQLiteProcessStateRepository(SQLiteState(tmp_path / "state.db"))
    repo.upsert_track(_track(1))
    process.mark_downloaded("cache/1.mp3", 1)
    repo.save_lyrics(1, "[]", 0.0)

    repo.remove_track(1)

    assert repo.get_track(1) is None
    assert process.find_track_id_by_path("cache/1.mp3") is None
    assert not process.is_processed(1, set())
    assert repo.get_lyrics(1) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv python -m pytest tests/test_sqlite_state.py -q`
Expected: FAIL，`ImportError: cannot import name 'SQLiteProcessStateRepository'`

- [ ] **Step 3: 重写 `src/musicvault/adapters/state/sqlite.py`**

删除全部旧内容，写入：

```python
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
from contextlib import contextmanager
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
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
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

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        yield from _transaction(self.database)

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

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        yield from _transaction(self.database)

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
```

- [ ] **Step 4: 重写 `tests/test_sqlite_more.py`**

删除旧内容，写入（保留 remove_track/remove_playlist/list_media_assets 用例，删 register_preset/register_target 用例）：

```python
"""SQLite 源侧状态仓储补充单测：显式连接分支与全量查询。

覆盖：remove_track/remove_playlist/save_lyrics 的传入连接分支、list_media_assets 全量查询。
"""

from __future__ import annotations

from pathlib import Path

from musicvault.adapters.state.sqlite import SQLiteSourceStateRepository, SQLiteState
from musicvault.domain.models import MediaAsset, Playlist, Track


def _track(track_id: int = 1) -> Track:
    return Track(id=track_id, name=f"曲目 {track_id}", artists=["歌手"], album="专辑", raw={})


def _repository(tmp_path: Path) -> SQLiteSourceStateRepository:
    return SQLiteSourceStateRepository(SQLiteState(tmp_path / "state.db"))


def test_remove_track_with_explicit_connection(tmp_path: Path) -> None:
    """remove_track 传入外部连接时在事务内执行并级联删除歌词行。"""
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
    """list_media_assets 无参返回全部音频资产（跨曲目），不含歌词行。"""
    repo = _repository(tmp_path)
    repo.upsert_track(_track(1))
    repo.upsert_track(_track(2))
    repo.upsert_media_asset(MediaAsset(track_id=1, asset_type="audio", spec="FLAC", path=tmp_path / "1.flac"))
    repo.upsert_media_asset(MediaAsset(track_id=2, asset_type="audio", spec="FLAC", path=tmp_path / "2.flac"))
    repo.save_lyrics(1, "[]", 0.0)

    assets = repo.list_media_assets()
    assert len(assets) == 2
    assert {asset.track_id for asset in assets} == {1, 2}


def test_save_lyrics_with_explicit_connection(tmp_path: Path) -> None:
    """save_lyrics 传入外部连接时写入生效。"""
    repo = _repository(tmp_path)
    with repo.transaction() as connection:
        repo.save_lyrics(1, "[歌词]", 0.0, connection=connection)

    assert repo.get_lyrics(1) == "[歌词]"
```

- [ ] **Step 5: 重写 `tests/test_state_lyrics.py`**

删除旧内容，写入：

```python
from musicvault.adapters.state.sqlite import SQLiteSourceStateRepository, SQLiteState


def test_lyrics_upsert_and_read(tmp_path):
    state = SQLiteSourceStateRepository(SQLiteState(tmp_path / "test.db"))
    assert state.get_lyrics(42) is None
    state.save_lyrics(42, '[{"start_ms":1,"duration_ms":0,"text":"x"}]', 123.0)
    state.save_lyrics(42, '[{"start_ms":2,"duration_ms":0,"text":"y"}]', 456.0)  # upsert 覆盖
    assert state.get_lyrics(42) == '[{"start_ms":2,"duration_ms":0,"text":"y"}]'


def test_lyrics_row_hidden_from_media_assets_and_snapshot(tmp_path):
    """歌词原稿行不进入媒体资产列表与源快照。"""
    state = SQLiteSourceStateRepository(SQLiteState(tmp_path / "test.db"))
    state.save_lyrics(42, "[]", 0.0)

    assert state.list_media_assets(track_id=42) == []
    assert state.create_snapshot().media_assets == ()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv python -m pytest tests/test_sqlite_state.py tests/test_sqlite_more.py tests/test_state_lyrics.py -q`
Expected: 全 PASS（其他测试文件此刻会因 `SQLiteStateRepository` 已删除而失败，属预期迁移期红，Task 5 收尾）

- [ ] **Step 7: 提交**

```bash
git add src/musicvault/adapters/state/sqlite.py tests/test_sqlite_state.py tests/test_sqlite_more.py tests/test_state_lyrics.py
git commit -m "refactor: 重写 SQLite 适配器为新表结构并拆分双 Repository"
```

---

### Task 2: 端口拆分与源侧用例迁移

**Files:**
- Create: `src/musicvault/ports/source_state.py`
- Create: `src/musicvault/ports/process_state.py`
- Delete: `src/musicvault/ports/state.py`
- Modify: `src/musicvault/application/source_state.py`
- Modify: `src/musicvault/application/playlist_use_case.py`
- Modify: `tests/test_source_state_recorder.py`
- Modify: `tests/test_playlist_use_case.py`
- Modify: `tests/test_playlist_use_case_more_edges.py`

**Interfaces:**
- Consumes: Task 1 产出的 `SQLiteSourceStateRepository` / `SQLiteProcessStateRepository`
- Produces:
  - `musicvault.ports.source_state.SourceStateRepository`（Protocol，方法集与 Task 1 的 `SQLiteSourceStateRepository` 公开方法一致，不含 `save_source_state`、`list_tracks`）
  - `musicvault.ports.process_state.ProcessStateRepository`（Protocol：`mark_downloaded` / `list_downloaded_track_ids` / `find_track_id_by_path` / `mark_processed` / `is_processed` / `transaction`）
  - `SourceStateRecorder(state: SourceStateRepository)`、`PlaylistUseCase(cfg, state: SourceStateRepository)`，方法名 `*_managed_track`

- [ ] **Step 1: 创建 `src/musicvault/ports/source_state.py`**

```python
"""源侧状态端口：曲目/歌单/管理标记/媒体资产/歌词原稿的读写能力。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, Track


class SourceStateRepository(Protocol):
    """源侧状态的最小公开查询与写入能力。"""

    def create_snapshot(self) -> SourceSnapshot: ...

    def get_track(self, track_id: int) -> Track | None: ...

    def upsert_track(self, track: Track, *, connection: Any = None) -> None: ...

    def remove_track(self, track_id: int, *, connection: Any = None) -> None: ...

    def upsert_playlist(self, playlist: Playlist, *, connection: Any = None) -> None: ...

    def get_playlist(self, playlist_id: int) -> Playlist | None: ...

    def list_playlists(self) -> list[Playlist]: ...

    def remove_playlist(self, playlist_id: int, *, connection: Any = None) -> None: ...

    def add_managed_track(self, track_id: int, *, connection: Any = None) -> None: ...

    def has_managed_track(self, track_id: int) -> bool: ...

    def list_managed_tracks(self) -> list[int]: ...

    def remove_managed_track(self, track_id: int) -> None: ...

    def upsert_media_asset(self, asset: MediaAsset, *, connection: Any = None) -> None: ...

    def list_media_assets(self, track_id: int | None = None) -> list[MediaAsset]: ...

    def save_lyrics(self, track_id: int, payload: str, fetched_at: float, *, connection: Any = None) -> None: ...

    def get_lyrics(self, track_id: int) -> str | None: ...

    def transaction(self) -> AbstractContextManager[Any]: ...
```

- [ ] **Step 2: 创建 `src/musicvault/ports/process_state.py`**

```python
"""处理管线状态端口：下载/处理进度与 raw 路径映射。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class ProcessStateRepository(Protocol):
    """处理管线状态（downloaded → processed）的读写能力。"""

    def mark_downloaded(self, path: str, track_id: int) -> None: ...

    def list_downloaded_track_ids(self) -> list[int]: ...

    def find_track_id_by_path(self, path: str) -> int | None: ...

    def mark_processed(self, track_id: int, updated_at: float) -> None: ...

    def is_processed(self, track_id: int, required_specs: set[str]) -> bool: ...

    def transaction(self) -> AbstractContextManager[Any]: ...
```

- [ ] **Step 3: 删除 `src/musicvault/ports/state.py`**

```bash
git rm src/musicvault/ports/state.py
```

- [ ] **Step 4: 修改 `src/musicvault/application/source_state.py`**

替换 import 与类型标注、方法名（3 处 `managed_song`）：

```python
from musicvault.ports.source_state import SourceStateRepository


class SourceStateRecorder:
    """把 fetch/pull/process 阶段产生的源侧状态持久化到 SourceStateRepository（SQLite）。

    结果写入 SQLite，供 distribute 阶段通过 SourceSnapshot 消费，
    而不再只落在旧 JSON 状态文件中。
    """

    def __init__(self, state: SourceStateRepository) -> None:
        self.state = state
```

`record_source_state` 中：

```python
            for song_id in managed_songs:
                self.state.add_managed_track(int(song_id), connection=connection)
```

- [ ] **Step 5: 修改 `src/musicvault/application/playlist_use_case.py`**

替换 import 与构造签名：

```python
from musicvault.ports.source_state import SourceStateRepository


class PlaylistUseCase:
    """歌单与单曲管理的应用用例。"""

    def __init__(self, cfg: Config, state: SourceStateRepository) -> None:
```

方法名替换（4 处）：

```python
    def list_songs(self) -> list[int]:
        return self.state.list_managed_tracks()

    def has_song(self, song_id: int) -> bool:
        return self.state.has_managed_track(song_id)

    def add_song(self, song_id: int) -> None:
        """登记单独管理的单曲；track 不存在时先写占位记录（managed_tracks 外键约束）。"""
        if self.state.has_managed_track(song_id):
            return
        if self.state.get_track(song_id) is None:
            # 占位曲目由 sync 获取真实元数据后覆盖
            self.state.upsert_track(Track(id=song_id, name=str(song_id), artists=[], album="", raw={}))
        self.state.add_managed_track(song_id)

    def remove_song(self, song_id: int) -> None:
        """移除单曲管理登记并删除其 canonical 文件。"""
        self.state.remove_managed_track(song_id)
```

- [ ] **Step 6: 更新 `tests/test_source_state_recorder.py`**

替换 import 与 helper：

```python
from musicvault.adapters.state.sqlite import SQLiteSourceStateRepository, SQLiteState


def _repository(tmp_path: Path) -> SQLiteSourceStateRepository:
    return SQLiteSourceStateRepository(SQLiteState(tmp_path / "state.db"))
```

文件中若断言 `list_managed_songs`（测试名 `test_recorder_persists_tracks_playlists_and_managed_songs` 附近可能有），改为 `repo.list_managed_tracks()`。

- [ ] **Step 7: 更新 `tests/test_playlist_use_case.py` 与 `tests/test_playlist_use_case_more_edges.py`**

替换 import 与 helper（两个文件同样处理）：

```python
from musicvault.adapters.state.sqlite import SQLiteSourceStateRepository, SQLiteState


def _repository(cfg: Config) -> SQLiteSourceStateRepository:
    return SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file))
```

`_use_case` helper 的返回类型标注 `SQLiteStateRepository` → `SQLiteSourceStateRepository`。断言中的 `repo.list_managed_songs()` → `repo.list_managed_tracks()`（例如 `test_playlist_use_case.py:40` 的 `assert repo.list_managed_songs() == [123, 456]`）。

- [ ] **Step 8: 运行验证**

Run: `uv python -m pytest tests/test_source_state_recorder.py tests/test_playlist_use_case.py tests/test_playlist_use_case_more_edges.py tests/test_architecture.py -q`
Expected: 全 PASS。`test_architecture.py` 验证 adapters 不 import application/ports 的规则仍然成立（本任务未改 adapters）。

- [ ] **Step 9: 提交**

```bash
git add src/musicvault/ports/source_state.py src/musicvault/ports/process_state.py src/musicvault/ports/state.py src/musicvault/application/source_state.py src/musicvault/application/playlist_use_case.py tests/test_source_state_recorder.py tests/test_playlist_use_case.py tests/test_playlist_use_case_more_edges.py
git commit -m "refactor: 拆分状态端口并迁移源侧用例（managed_tracks 命名）"
```

---

### Task 3: 同步与处理用例迁移（双端口注入）

**Files:**
- Modify: `src/musicvault/application/sync_use_case.py`
- Modify: `src/musicvault/application/process_use_case.py`
- Modify: `src/musicvault/application/pipeline_use_case.py`
- Modify: `tests/test_sync_use_case.py`、`tests/test_sync_use_case_more_edges.py`
- Modify: `tests/test_process_use_case.py`、`tests/test_process_use_case_more_edges.py`
- Modify: `tests/test_pipeline_use_case.py`、`tests/test_pipeline_to_sqlite.py`
- Modify: `tests/test_dry_run.py`、`tests/test_playlist_reconciliation.py`

**Interfaces:**
- Consumes: Task 2 的两个 Protocol
- Produces:
  - `SyncUseCase(cfg, api, downloader, workers, state: SourceStateRepository, process_state: ProcessStateRepository, dry_run=False)`，属性 `self.process_state`
  - `ProcessUseCase(cfg, api, decryptor, organizer, metadata, workers, state: SourceStateRepository, process_state: ProcessStateRepository, dry_run=False, presets=None)`，属性 `self.process_state`
  - `PipelineUseCase(cfg, api, state: SourceStateRepository, process_state: ProcessStateRepository, dry_run=False, presets=None, registry=None, target=None)`

- [ ] **Step 1: 修改 `src/musicvault/application/sync_use_case.py`**

import 替换：

```python
from musicvault.ports.process_state import ProcessStateRepository
from musicvault.ports.source_state import SourceStateRepository
```

构造签名与属性：

```python
    def __init__(
        self,
        cfg: Config,
        api: SourceClient,
        downloader: Downloader,
        workers: int,
        state: SourceStateRepository,
        process_state: ProcessStateRepository,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.downloader = downloader
        self.workers = max(1, workers)
        self.dry_run = dry_run
        self.process_state = process_state
        # workspace 各生命周期区域路径的唯一来源（cache/media_store/library/logs）
        self.paths = WorkspacePaths(cfg.workspace_path)
        # 把本次 sync 的源侧状态写入 SQLite，供 distribute 阶段消费
        self.recorder = SourceStateRecorder(state)
```

方法名替换（精确清单）：

| 位置 | 旧代码 | 新代码 |
|---|---|---|
| `run_fetch`（约 92 行） | `self.recorder.state.list_managed_songs()` | `self.recorder.state.list_managed_tracks()` |
| `run_pull`（约 122 行） | `self.recorder.state.list_managed_songs()` | `self.recorder.state.list_managed_tracks()` |
| `_fetch_remote`（约 171 行） | `self.recorder.state.list_managed_songs()` | `self.recorder.state.list_managed_tracks()` |
| `_fetch_remote`（约 206 行） | `self.recorder.state.remove_managed_song(mid)` | `self.recorder.state.remove_managed_track(mid)` |
| `_diff_tracks`（约 310 行） | `downloaded_ids.update(self.recorder.state.list_pending_track_ids())` | `downloaded_ids.update(self.process_state.list_downloaded_track_ids())` |
| `_sync_tracks`（约 425 行） | `self.recorder.state.add_pending_file(rel, item.track.id)` | `self.process_state.mark_downloaded(rel, item.track.id)` |
| `_save_partial_downloads`（约 478 行） | `self.recorder.state.add_pending_file(rel, item.track.id)` | `self.process_state.mark_downloaded(rel, item.track.id)` |

注释同步更新：`_cleanup_stale_state` 与 `remove_track` 的 docstring 中「（级联清理 processed_tracks / pending_files / 关系）」改为「（级联清理 processing_state / media_assets / 关系）」；`_diff_tracks` docstring 中「或待处理 raw 文件（pending_files）的曲目视为已下载」改为「或待处理 raw 文件（processing_state.downloaded）的曲目视为已下载」。

- [ ] **Step 2: 修改 `src/musicvault/application/process_use_case.py`**

import 替换：

```python
from musicvault.ports.process_state import ProcessStateRepository
from musicvault.ports.source_state import SourceStateRepository
```

构造签名与属性：

```python
    def __init__(
        self,
        cfg: Config,
        api: SourceClient,
        decryptor: Decryptor,
        organizer: Organizer,
        metadata: MetadataWriter,
        workers: int,
        state: SourceStateRepository,
        process_state: ProcessStateRepository,
        dry_run: bool = False,
        presets: Mapping[str, BasePreset] | None = None,
    ) -> None:
```

在 `self.recorder = SourceStateRecorder(state)` 之后加：

```python
        self.process_state = process_state
```

方法调用替换（精确清单）：

| 位置 | 旧代码 | 新代码 |
|---|---|---|
| `run_process`（约 113 行） | `self.recorder.state.is_processed(track_id, required_specs)` | `self.process_state.is_processed(track_id, required_specs)` |
| `_filter_pending`（约 379 行） | `self.recorder.state.is_processed(track.id, required_specs)` | `self.process_state.is_processed(track.id, required_specs)` |
| `_mark_processed`（约 395 行） | `self.recorder.state.record_processed(track_id, "preset-script", time.time())` | `self.process_state.mark_processed(track_id, time.time())` |
| `_guess_track_id`（约 401 行） | `self.recorder.state.find_track_id_by_path(rel)` | `self.process_state.find_track_id_by_path(rel)` |

注释同步更新：`_mark_processed` 中「固定标记（不再依赖 domain/preset.py 的 compute_preset_hash）」删除（preset_hash 概念已退役）；`_guess_track_id` docstring「从 SQLite pending_files 反查」改为「从 processing_state 反查」。

- [ ] **Step 3: 修改 `src/musicvault/application/pipeline_use_case.py`**

import 替换：

```python
from musicvault.ports.process_state import ProcessStateRepository
from musicvault.ports.source_state import SourceStateRepository
```

构造签名：

```python
    def __init__(
        self,
        cfg: Config,
        api: SourceClient,
        state: SourceStateRepository,
        process_state: ProcessStateRepository,
        dry_run: bool = False,
        presets: Mapping[str, BasePreset] | None = None,
        registry: PresetRegistry | None = None,
        target: TargetOperations | None = None,
    ) -> None:
```

`SyncUseCase` / `ProcessUseCase` 构造调用加 `process_state=process_state`：

```python
        self.sync_service = SyncUseCase(
            cfg=cfg,
            api=api,
            downloader=Downloader(),
            workers=max(1, download_workers),
            dry_run=dry_run,
            state=state,
            process_state=process_state,
        )
        self.process_service = ProcessUseCase(
            ...
            dry_run=dry_run,
            state=state,
            process_state=process_state,
            presets=presets,
        )
```

- [ ] **Step 4: 更新测试 helper（6 个文件统一模式）**

对 `tests/test_sync_use_case.py`、`tests/test_sync_use_case_more_edges.py`、`tests/test_process_use_case.py`、`tests/test_process_use_case_more_edges.py`、`tests/test_pipeline_use_case.py`、`tests/test_pipeline_to_sqlite.py`、`tests/test_dry_run.py`，统一做三件事：

a) import 替换：

```python
from musicvault.adapters.state.sqlite import SQLiteProcessStateRepository, SQLiteSourceStateRepository, SQLiteState
```

b) `_repository` helper 返回类型改为 `SQLiteSourceStateRepository`，并新增：

```python
def _process_repository(cfg: Config) -> SQLiteProcessStateRepository:
    return SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file))
```

（`test_dry_run.py` 中 helper 参数是 `cfg: Config`；若某些文件 helper 参数为 `tmp_path`，用 `tmp_path / "state.db"` 同样处理。）

c) 所有 `SyncUseCase(...)` / `ProcessUseCase(...)` 构造调用处追加 `process_state=_process_repository(cfg)`（或对已有 repo 变量追加 `process_state=SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file))`）。

- [ ] **Step 5: 更新方法名断言与调用（精确清单）**

`tests/test_sync_use_case_more_edges.py`：

```python
# 约 410 / 442 行
assert repo.list_pending_track_ids() == [111]
```
改为：
```python
assert _process_repository(cfg).list_downloaded_track_ids() == [111]
```
（若该测试内已有 process repo 变量，直接复用变量名。）

`tests/test_process_use_case.py`：所有 `repo.record_processed(333, "preset-script", 0.0)` → `process.mark_processed(333, 0.0)`；所有 `repo.is_processed(...)` → `process.is_processed(...)`；`repo.add_pending_file(rel, 333)`（约 484 行）→ `process.mark_downloaded(rel, 333)`。其中 process 为测试内新建的 `SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file))`（与源侧 repo 共享同一 db 文件）。

`tests/test_process_use_case_more_edges.py`：同样替换 `repo.is_processed` → `process.is_processed`（4 处：148/149/170/274 行）；`repo.record_processed` → `process.mark_processed`。

`tests/test_pipeline_to_sqlite.py`：

```python
# 约 192 行
assert repo.list_managed_songs() == [999]          →  assert repo.list_managed_tracks() == [999]
# 约 348 行
repo.record_processed(333, "preset-script", 0.0)   →  process.mark_processed(333, 0.0)
# 约 373 行
repo.add_pending_file(rel, 333)                    →  process.mark_downloaded(rel, 333)
```

`tests/test_dry_run.py`：

```python
# 约 57 行
repo.add_pending_file("cache/111.mp3", 111)        →  process.mark_downloaded("cache/111.mp3", 111)
# 约 142 行
assert repo.list_managed_songs() == [999, 1000]    →  assert repo.list_managed_tracks() == [999, 1000]
# 约 202 / 221 / 234 行
repo.record_processed(333, "preset-script", 0.0)   →  process.mark_processed(333, 0.0)
repo.is_processed(...)                             →  process.is_processed(...)
```

`tests/test_playlist_reconciliation.py`：`SQLiteStateRepository(SQLiteState(cfg.state_db_file))` 全部改为 `SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file))`（import 同步）；三处 `SyncUseCase(cfg, MagicMock(), MagicMock(), workers=1, state=...)` 追加 `process_state=SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file))`。

注意：`test_process_use_case.py` / `test_process_use_case_more_edges.py` / `test_pipeline_to_sqlite.py` / `test_dry_run.py` 中的 `_process_svc` / 直接构造处，`ProcessUseCase(...)` 关键字参数全部加 `process_state=`。

- [ ] **Step 6: 运行验证**

Run: `uv python -m pytest tests/test_sync_use_case.py tests/test_sync_use_case_more_edges.py tests/test_process_use_case.py tests/test_process_use_case_more_edges.py tests/test_pipeline_use_case.py tests/test_pipeline_to_sqlite.py tests/test_dry_run.py tests/test_playlist_reconciliation.py -q`
Expected: 全 PASS（bootstrap 相关测试此刻仍红，Task 4 修复）。

- [ ] **Step 7: 提交**

```bash
git add src/musicvault/application/sync_use_case.py src/musicvault/application/process_use_case.py src/musicvault/application/pipeline_use_case.py tests/test_sync_use_case.py tests/test_sync_use_case_more_edges.py tests/test_process_use_case.py tests/test_process_use_case_more_edges.py tests/test_pipeline_use_case.py tests/test_pipeline_to_sqlite.py tests/test_dry_run.py tests/test_playlist_reconciliation.py
git commit -m "refactor: 同步与处理用例迁移至双状态端口"
```

---

### Task 4: bootstrap 组装与 preset 注册移除

**Files:**
- Modify: `src/musicvault/application/bootstrap.py`
- Modify: `tests/test_bootstrap.py`
- Delete: `tests/test_preset_registry_persistence.py`
- Modify: `tests/test_bootstrap_more.py`（如断言受影响）

**Interfaces:**
- Consumes: Task 3 的 `PipelineUseCase` 新签名、Task 1 的两个 SQLite Repository 类
- Produces:
  - `Runtime(paths, source_state: SQLiteSourceStateRepository, process_state: SQLiteProcessStateRepository, presets: PresetRegistry)`（`state` 字段删除）
  - `build_runtime(config)`、`build_pipeline(config, source=None, *, dry_run=False)`、`build_playlist_use_case(config)`、`build_distribute_pipeline(config, *, dry_run=False)` 全部不再向 SQLite 写注册信息

- [ ] **Step 1: 修改 `src/musicvault/application/bootstrap.py`**

import 替换：

```python
from musicvault.adapters.state.sqlite import SQLiteProcessStateRepository, SQLiteSourceStateRepository, SQLiteState
```

`Runtime` 定义：

```python
@dataclass(frozen=True, slots=True)
class Runtime:
    """composition root 创建的具体运行时依赖。"""

    paths: WorkspacePaths
    source_state: SQLiteSourceStateRepository
    process_state: SQLiteProcessStateRepository
    presets: PresetRegistry
```

`build_runtime`：

```python
def build_runtime(config: Config) -> Runtime:
    paths = WorkspacePaths(config.workspace_path)
    paths.ensure()
    database = SQLiteState(paths.state_db)
    source_state = SQLiteSourceStateRepository(database)
    process_state = SQLiteProcessStateRepository(database)
    presets = PresetRegistry()
    if config.builtin_scripts_enabled:
        register_builtin_presets(presets, config.library_dir, config.default_playlist_name)
    directories = [Path(directory) for directory in config.preset_directories]
    presets.load_directories(directories)
    return Runtime(
        paths=paths,
        source_state=source_state,
        process_state=process_state,
        presets=presets,
    )
```

（删除原「preset 与 sync_target 两类注册分 kind 写入 preset_registry」两个 for 循环与注释。）

`build_pipeline` 尾部：

```python
    database = SQLiteState(config.state_db_file)
    return PipelineUseCase(
        cfg=config,
        api=source,
        state=SQLiteSourceStateRepository(database),
        process_state=SQLiteProcessStateRepository(database),
        dry_run=dry_run,
        presets=presets,
        registry=registry,
        target=FilesystemTarget(WorkspacePaths(config.workspace_path).library),
    )
```

`build_playlist_use_case`：

```python
def build_playlist_use_case(config: Config) -> PlaylistUseCase:
    """组装歌单/单曲管理用例（add/remove/list 命令专用）。"""
    return PlaylistUseCase(
        cfg=config,
        state=SQLiteSourceStateRepository(SQLiteState(config.state_db_file)),
    )
```

`DistributePipeline.run` 中：

```python
        return self.engine.run(
            self.runtime.source_state.create_snapshot(),
            self.runtime.presets.registrations(enabled_only=True),
            selected=selected,
            presets=presets,
        )
```

- [ ] **Step 2: 修改 `tests/test_bootstrap.py`**

`test_build_runtime_builtin_scripts_disabled`（约 115-122 行）末尾断言替换：

```python
    assert runtime.presets.preset_registrations() == ()
    assert runtime.presets.target_registrations() == ()
    # preset 注册只存在于内存注册表（动态发现），不再写入 SQLite
    assert runtime.source_state.create_snapshot().tracks == ()
```

检查文件其余部分是否引用 `runtime.state`（如 `test_build_runtime_builtin_target_root_is_library_dir` 只用 `runtime.presets`，无需改）。

- [ ] **Step 3: 删除 `tests/test_preset_registry_persistence.py`**

```bash
git rm tests/test_preset_registry_persistence.py
```

- [ ] **Step 4: 检查 `tests/test_bootstrap_more.py`**

`test_build_playlist_use_case` 使用 `service.state.upsert_track(...)`，`PlaylistUseCase.state` 字段仍在（源侧端口），无需修改。运行确认即可。

- [ ] **Step 5: 运行验证**

Run: `uv python -m pytest tests/test_bootstrap.py tests/test_bootstrap_more.py tests/test_bootstrap_pipeline.py tests/test_builtin_hardlink.py -q`
Expected: 全 PASS。

- [ ] **Step 6: 提交**

```bash
git add src/musicvault/application/bootstrap.py tests/test_bootstrap.py tests/test_bootstrap_more.py tests/test_preset_registry_persistence.py
git commit -m "refactor: bootstrap 组装双状态端口并移除 preset 注册持久化"
```

---

### Task 5: 全量验证与收尾

**Files:**
- Modify: 任何仍失败的测试文件（按失败输出修复）
- Modify: `AGENTS.md`（「schema 版本化」表述更新）

**Interfaces:**
- Consumes: 前 4 个任务全部产物

- [ ] **Step 1: 全量测试**

Run: `uv python -m pytest tests/ -q`
Expected: 全 PASS（当前 263 项，重构后数量可能因删除 preset 持久化测试而略有变化）。若有个别失败（如 `test_cli_*` 系列间接触发），按失败输出逐项修复；修复原则：只改测试断言与构造参数，不改动生产代码行为。

- [ ] **Step 2: lint 与格式**

Run: `uv python -m ruff check src/ tests/` 与 `uv python -m ruff format --check src/ tests/`
Expected: 无错误；如有格式问题执行 `uv python -m ruff format src/ tests/` 后再跑一次全量测试。

- [ ] **Step 3: 更新 `AGENTS.md`**

`workspace 布局` 小节中「`state.db`（SQLite，schema 版本化，写入走事务）」改为「`state.db`（SQLite，六表职责化 schema：源侧状态 + 处理管线状态；旧格式库检测后拒绝初始化；写入走事务）」。

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "refactor: 数据库表结构职责化重构收尾（全量测试与文档）"
```

- [ ] **Step 5: 全量回归确认**

Run: `uv python -m pytest tests/ -q`
Expected: 全 PASS，重构完成。

---

## Self-Review 记录

- Spec 覆盖：新六表 schema（Task 1）、旧库检测（Task 1）、歌词并入 media_assets（Task 1 的 save_lyrics/get_lyrics + 测试）、管线状态合并（Task 1 的 processing_state + Task 3 调用迁移）、managed_tracks 改名（Task 1 建表 + Task 2 端口与用例）、端口拆分（Task 2）、bootstrap 组装与 preset 注册移除（Task 4）、track_count 冗余列删除（Task 1 的 _upsert_playlist）、测试影响与文档（Task 3/4/5）。
- 类型一致性：`mark_downloaded(path, track_id)`、`mark_processed(track_id, updated_at)`、`list_downloaded_track_ids()`、`is_processed(track_id, required_specs)` 在 Task 1（实现）、Task 2（Protocol）、Task 3（用例调用）、Task 4（组装）中签名一致。

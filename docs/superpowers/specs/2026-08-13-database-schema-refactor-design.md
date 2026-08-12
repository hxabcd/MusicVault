# 设计：SQLite 表结构职责化重构（破坏性重建）

日期：2026-08-13
状态：待审阅

## 背景与目标

当前 `state.db`（SCHEMA_VERSION=2）共 10 张业务表，混有三类职责：源侧状态（tracks/playlists/managed_songs）、媒体资产与管线状态（media_assets/lyrics/processed_tracks/pending_files）、配置注册（preset_registry/export_targets）。存在死表、只写不读的镜像表、职责重叠与退化列。

目标：重写表结构使其职责清晰，每个概念一张表、每张表一个职责。经用户确认，采用**破坏性重建**（方案 A）：废弃迁移链，旧库不兼容，检测到旧格式时提示用户删除 `state.db` 后自动重建。

## 现状问题清单

| 表 | 问题 |
|---|---|
| `lyrics` | 与 `media_assets` 职责重叠（歌词原稿本质是媒体资产） |
| `processed_tracks` | `preset_hash` 列退化（恒写 `"preset-script"`）；`is_processed` 实际依赖 media_assets 的 spec 覆盖 |
| `pending_files` | 命名模糊；与 `processed_tracks` 同属「处理管线状态」却分两张表 |
| `preset_registry` | 只写不读的注册镜像；preset 采用注册策略、每次启动由脚本动态发现，注册信息不应入库 |
| `export_targets` | 死表（`register_target` 只写，无任何读取方） |
| `managed_songs` | 命名不统一（songs vs tracks） |
| `playlists` | `track_count` 冗余列（可由 playlist_tracks 计数） |
| `schema_version` + `_MIGRATIONS` | 迁移链随破坏性重建废弃 |

## 新表结构（单版本，无迁移链）

```sql
tracks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    artists_json TEXT NOT NULL,
    album TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    cover_url TEXT,
    duration_ms INTEGER,
    raw_json TEXT NOT NULL
);  -- 不变

playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);  -- 删除冗余 track_count

playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id),
    UNIQUE (playlist_id, position)
);  -- 不变

managed_tracks (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE
);  -- 改名自 managed_songs；保留「占位曲目先登记」机制

media_assets (
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
);  -- path 可空：歌词行无文件；新增 data_json 存歌词原稿 payload

processing_state (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('downloaded', 'processed')),
    raw_path TEXT,
    updated_at REAL NOT NULL
);  -- 合并 pending_files + processed_tracks
```

删除表：`lyrics`、`processed_tracks`、`pending_files`、`preset_registry`、`export_targets`、`schema_version`。

### 语义说明

- **歌词原稿**：`asset_type='lyrics'`、`spec='unified'`、`source='netease'`、`path=NULL`、`data_json` 存统一歌词格式 JSON（原 `lyrics.payload`）。歌词是 process 阶段专用数据，快照构建（`create_snapshot`）排除 lyrics 行，不进入 `SourceSnapshot`。
- **管线状态**：`downloaded` 行携带 `raw_path`（供 `_guess_track_id` 反查，对应原 `pending_files`）；处理完成后置 `state='processed'`、`raw_path=NULL`（对应原 `processed_tracks`）。
- **`is_processed(track_id, required_specs)`**：`processing_state` 该 track 行为 `processed` 且 media_assets 的 audio spec 覆盖 `required_specs`。行为与现状一致，退化的 `preset_hash` 列消失。

## 端口拆分

`ports/state.py` 拆为两个 Protocol 文件：

- `ports/source_state.py` — `SourceStateRepository`：
  `transaction`、`create_snapshot`、`get_track`/`upsert_track`/`remove_track`、
  `upsert_playlist`/`get_playlist`/`list_playlists`/`remove_playlist`、
  `add_managed_track`/`has_managed_track`/`list_managed_tracks`/`remove_managed_track`（原名 `*_managed_song`）、
  `upsert_media_asset`/`list_media_assets`、`save_lyrics`/`get_lyrics`
- `ports/process_state.py` — `ProcessStateRepository`：
  `transaction`、`mark_downloaded`/`list_downloaded_track_ids`/`find_track_id_by_path`（原名 `add_pending_file` 系列）、
  `mark_processed`（原名 `record_processed`，无 `preset_hash` 参数）、`is_processed`

## 适配器与组装

`adapters/state/sqlite.py` 保留 `SQLiteState`（连接、建库、旧库检测），Repository 拆为两个类共享同一 `SQLiteState`：

- `SQLiteSourceStateRepository` 实现 `SourceStateRepository`
- `SQLiteProcessStateRepository` 实现 `ProcessStateRepository`（`is_processed` 跨表查询 processing_state + media_assets）

组装点（`application/bootstrap.py`）：

- `Runtime`：`state: SQLiteStateRepository` 改为 `source_state` + `process_state` 两个字段；删除 `register_preset` 写入逻辑（连同 `preset_registry` 表一起退役）。
- `build_pipeline`：构造两个 repo 注入 `PipelineUseCase`，由其分别传给 `SyncUseCase` 与 `ProcessUseCase`。
- `build_playlist_use_case`：只注入 `SQLiteSourceStateRepository`。
- `build_distribute_pipeline`：`runtime.source_state.create_snapshot()`。

依赖方向不变：`ports/` 只描述业务能力，`adapters/` 只依赖 domain；application 用例注入两个端口。

## 旧库检测

`SQLiteState.initialize`：建表前查询 `sqlite_master`，若存在任一旧表名（`preset_registry`、`export_targets`、`lyrics`、`processed_tracks`、`pending_files`、`managed_songs`、`schema_version`）则抛 `RuntimeError`：「检测到旧格式数据库 <path>，本版本不再迁移旧数据，请删除后重新运行」。避免新旧表混杂产生半新半旧库。

## 测试影响

- 删除：`tests/test_preset_registry_persistence.py` 整文件；`test_bootstrap.py`/`test_sqlite_more.py` 中 `preset_registry` 与 `export_targets` 相关用例。
- 改写：`test_sqlite_state.py` 的迁移链测试 → 旧库检测测试；`test_state_lyrics.py` → media_assets 歌词行语义；pending/processed 相关用例改新方法名与语义。
- 各测试 `_repository()` helper：返回单 repo 的改为按需构造双 repo（或只取所需侧）。
- `test_architecture.py` 无需改动（依赖方向规则不变）。

## 范围外（YAGNI）

- 不做表前缀/分组命名（删除配置表后仅剩源侧与管线两类，前缀无收益）。
- 不改 `MediaAsset` 域模型（歌词原稿经专用方法读写 media_assets 表，不映射为 `MediaAsset`，避免影响快照哈希）。
- 不引入分库（workspace 保持单 `state.db`）。

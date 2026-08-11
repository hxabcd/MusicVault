# 交接文档：旧流水线迁入 media_store 新布局（规划，未实施）

日期：2026-08-12
状态：规划中（交接文档，等待 A+B+E 修复完成后再启动）

> 本文件由 2026-08-12 spec 落实审计（7 代理并行）产出，作为旧流水线迁移工作的独立规划起点。相关设计见 `docs/superpowers/specs/2026-08-12-spec-deviation-remediation-design.md` 的阶段 0。所有行号基于审计时的代码快照，实施前需重新核对。

## 目标

将旧命令流（sync/pull/process/reindex）的文件布局迁移到与新链路一致的 workspace 布局：

- canonical 文件从 `downloads/` 迁入 `media_store/<track_id>/audio/`
- 下载缓存从 `downloads/cache/` 迁入 `cache/`
- reindex 基于新布局（media_store 参与重建）
- 迁移完成后 `downloads/` 不再产生新文件

## 现状快照（证据）

| 代码点 | 现状 | 证据 |
|---|---|---|
| canonical 文件登记 | `build_audio_asset_from_file` 自述注释：「当前流水线的 canonical 文件仍落在旧 downloads 布局；待流水线迁到 media_store 后再更新路径」 | `src/musicvault/application/source_state.py:49-52` |
| 下载缓存写入 | 下载仍写 `downloads_cache_dir` | `src/musicvault/application/sync_use_case.py:549` |
| reindex | 区分 downloads canonical、downloads/cache、library（旧布局）；media_store 不参与 reindex；方向是从 downloads/library 目录重建 SQLite，而非从 DB 重建 library | `src/musicvault/application/pipeline_use_case.py:88-98, 133-155` |
| 删除 canonical | 删除逻辑位于 downloads（非 media_store） | `src/musicvault/application/sync_use_case.py:444-507` |
| 缓存清理 | 清理 `downloads_cache_dir`（受 `keep_downloads` 控制） | `src/musicvault/application/process_use_case.py:442-448` |
| migrate 承接 | 一次性把旧 downloads/cache 复制到新 cache/，canonical 复制到 media_store 并登记 SQLite；迁移后旧文件保留 | `src/musicvault/adapters/filesystem/workspace.py:146-162` |
| library 重建 | 旧链路可整体 rmtree library 后重建（硬链接视图） | `src/musicvault/application/sync_use_case.py:217-221` |

新链路（target-sync）已完全合规：`FileMediaStore.put` 幂等落盘 `media_store/<track_id>/audio/`（`adapters/filesystem/media_store.py:19-47`），preset 经 `MediaRequest/MediaResolver` 只读解析（`preset_api/_media.py:7-17`），一次运行内共享同一 SQLite 快照（`adapters/state/sqlite.py:352-359`）。迁移方向是让旧流水线对齐这套已就绪的机制。

## 顺带项（按关联度标注）

**建议同批处理（同一批文件，避免两次大改）：**

- **Rich 输出剥离**：`sync_use_case.py:15-16`、`process_use_case.py:22`、`pipeline_use_case.py:20` 直接 import `shared/tui_progress`（console/BatchProgress），pipeline_use_case.py 内 10 余处 `console.print`（:101, 177, 179, 243, 316, 323, 325, 344, 357-359）。新链路模式可参照：用例只返回结构化结果，打印归 CLI。AGENTS.md 已声明此偏离，新链路（sync_engine.py）已合规。
- **adapters 层 Rich 输出**：`adapters/processors/organizer.py:8` 使用 `shared.output.warn`（未在 AGENTS.md 偏离声明内）。
- **单曲登记事务细节**：`SyncUseCase._sync_tracks` 对每首曲目的 `upsert_track` 与 `add_pending_file` 是两次独立事务（`sync_use_case.py:528-531`），跨实体原子性仅靠外层 recorder 单事务兜底。

**另立规划（不随本次文件迁移）：**

- **hash 损坏检测/按需生成**：sha256 在 put/迁移/登记时已计算存库，但解析路径不做比对，「按需生成」不存在（`preset_api/_media.py:8` docstring 自述；wayfinder:67 已标注延期）。
- **歌词进快照**：`SourceSnapshot` 无歌词字段，歌词由 `ProcessUseCase` 实时调 API 写 `.lrc`（`process_use_case.py:221-263`）。
- **缺失资产结构化错误**：`SnapshotMediaResolver` 缺失返回 `None`，内置 preset 静默跳过（`preset_api/_media.py:17`、`preset_api/builtins.py:21-22`），spec 用户故事 9（明确生成失败原因）未满足。

## 测试影响面

以下测试的 fixture 路径与断言基于旧布局，迁移时需同步更新：

- `tests/test_pipeline_to_sqlite.py` — sync→SQLite→target-sync 闭环（:149-187）、JSON 替代断言（:190-252）
- `tests/test_reindex_to_sqlite.py` — reindex 目录扫描（:80-98 只导入可识别文件）
- `tests/test_workspace_migration.py` — migrate 复制逻辑（:14-123）
- `tests/test_playlist_reconciliation.py` — 旧链路硬链接增删（:184-249）
- `tests/test_source_state_recorder.py` — canonical 登记（:72-82 事务原子性）

另：迁移升级测试（旧 schema 版本 → 最新）在 `SCHEMA_VERSION = 1` 时不可测，等 schema v2 出现后补齐。

## 完成定义（DoD）

- [ ] sync/process/reindex 运行后 canonical 文件落在 `media_store/<track_id>/audio/`，`downloads/` 不再产生新文件
- [ ] 下载缓存落在 `cache/`，清理逻辑（`keep_downloads`）作用于新位置
- [ ] reindex 基于新布局（media_store 参与），且方向与语义明确（目录重建 DB 或 DB 重建 library 二选一并文档化）
- [ ] `source_state.py:49-52` 自述注释删除（已不再成立）
- [ ] 全量 pytest（≥204 项）绿 + ruff 通过
- [ ] 若同批完成 Rich 剥离：`shared/tui_progress` 仅被 cli/ 引用，AGENTS.md 已知偏离更新为已修复

## 实施建议

- 单分支顺序实施，按「缓存路径 → canonical 登记路径 → 删除/清理路径 → reindex」的依赖序推进，每步全量回归。
- 迁移逻辑已有先例：`WorkspaceMigration`（workspace.py:72-201）的复制/幂等/冲突保护模式可直接复用。
- 与 A+B+E 修复（2026-08-12 设计）串行：其 B2（JSON 退役）与 B3（路径单一化）会先改动 Config/WorkspacePaths，本迁移应在其后执行以避免路径定义在迁移中被二次修正。

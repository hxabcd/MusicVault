# MusicVault 最小可运行重构路线图

状态：已完成（2026-08-12）
类型：wayfinder:map

## Destination

为 MusicVault 的模块化单体重构确定一条无关键决策遗漏的实施路线，并形成可以交给执行阶段的工程计划。目的地是最小可运行闭环：SQLite 状态、cache/media_store、Python Preset 注册、内置 Playlist Links、最小 Target Synchronizer 和可验证的 CLI 流程。

## Notes

- 领域：本地音乐同步、媒体资产和目标端单向同步。
- 文档语言：中文。
- 初始规划阶段只解决决策，不执行代码重构；最小闭环现已进入实现阶段。
- 每个ticket只解决一个决策问题。
- 使用 codebase-design 术语：Module、Interface、Seam、Adapter、Depth、Leverage、Locality。
- 优先设计深模块：用较小的 Interface 隐藏复杂实现；测试优先穿过最高层 Seam。
- 外部 preset 是可信 Python 脚本，但只能依赖版本化公开 preset API。
- GitHub Issues 是 issue tracker 的事实来源；本目录保存本次规划的 map、ticket和依赖快照，正式跟踪以对应的 GitHub Issue 编号和链接为准。

## Implementation status

已在提交 `11182fd` 中落地最小可运行闭环：

- `domain/`、`application/`、`ports/`、`adapters/` 和 `preset_api/` 形成模块化单体的第一版 Module 边界。
- SQLite v1 状态库已提供 `tracks`、`playlists`、`playlist_tracks`、`managed_songs`、`media_assets`、`preset_registry` 和 `export_targets`，SQL 由 Repository 隔离。
- `cache/`、`media_store/<track_id>/audio/`、`library/`、`logs/` 和 `state.db` 已成为 workspace 的新布局；旧 `downloads/` 和 JSON 状态可通过迁移导入，原文件默认保留。
- `musicvault.preset_api.v1` 已提供 `PresetRegistry`、`PresetRegistration`、`PresetContext`、`TargetSynchronizer` 和统一操作结果模型；外部脚本通过 `register(registry)` 发现。
- 内置 `playlist_links` 已通过最小 `prepare → sync_item → finalize` 生命周期运行。Manifest 尚未实现，因此当前采用安全的 `append` 策略，不执行删除。
- `SourceSnapshot` 在一次 `target-sync` 中由同一 SQLite 连接读取并由所有启用 preset 共享；`MediaResolver` 当前只解析已有资产，按需生成延期。
- 可验证 CLI 流程为：`msv migrate` → `msv presets` → `msv target-sync [--preset NAME] [--dry-run]`。
- 新增契约测试覆盖 SQLite、迁移、Preset API、TargetSynchronizer 生命周期和 dry-run；当前完整测试集为 163 项通过。

后续提交继续打通旧流水线与新状态：`sync`/`process` 通过新增的 `SourceStateRecorder`（`application/source_state.py`）在单事务内把曲目、歌单关系、单独管理单曲和 canonical 媒体资产写入 SQLite，使 `msv sync` → `msv target-sync` 形成真实闭环；dry-run 不写库，陈旧单曲 id 不触发外键失败。

## Decisions so far

- [模块化单体与端口适配器](../specs/2026-08-05-architecture-modular-monolith-spec.md) — 以 domain/application/ports/adapters/presentation 划分职责，不拆微服务。
- [SQLite 状态存储](../specs/2026-08-05-sqlite-state-spec.md) — 使用 SQLite 作为结构化状态的唯一来源，Repository 隔离 SQL。
- [Workspace 与 media_store](../specs/2026-08-05-workspace-media-store-spec.md) — cache 保存临时文件，media_store 按 track_id 聚合长期媒体资产，library 是可重建视图。
- [Python Preset 注册与发现](../specs/2026-08-05-python-preset-system-spec.md) — 内置 Playlist Links 可开关，外部目录通过 register(registry) 加载，同名失败。
- [TargetSynchronizer](../specs/2026-08-05-target-synchronizer-spec.md) — Preset 定义目标策略，当前只实现最小生命周期。
- [Preset 操作 API](../specs/2026-08-05-preset-operation-api-spec.md) — 标准操作统一执行，自定义操作必须通过公开 context 登记。
- [SourceSnapshot 与 MediaResolver](../specs/2026-08-05-source-snapshot-and-media-resolution-spec.md) — 一次 sync 共享源快照，按需媒体生成延期。
- [目标安全策略与 Manifest](../specs/2026-08-05-target-safety-and-manifest-spec.md) — 当前单向同步，外部目标默认不删除，Manifest 延期。
- [Preset 同步历史](../specs/2026-08-05-preset-sync-history-spec.md) — 每个 Preset 的聚合同步历史和全局保留策略延期。

## Not yet specified

- 现有 `sync`、`pull`、`process` 旧流水线迁移到 SQLite/application 用例的具体 Seam；当前旧命令保留兼容，新闭环通过 `target-sync` 运行。
- 外部 preset 的目标配置、媒体需求声明和缺失资产时的生成策略。
- 从旧 `library` 视图重建新目标视图的完整规则，以及迁移失败后的人工恢复指引。
- 最小闭环之外的发布回滚策略和 workspace 版本升级策略。

## Implemented decisions

- SQLite schema 版本从 `1` 开始，使用 `schema_version` 表和顺序 migration；数据库写入通过事务完成。
- `MediaAsset` 使用 `track_id + asset_type + spec` 定位，迁移时保存路径、大小、SHA-256、来源和更新时间。
- 外部 preset API 固定为 `v1`；同名 preset 报错并包含来源，API 版本不兼容时阻止加载。
- 标准操作包括 link、copy、write_text；自定义操作必须经 context 登记，dry-run 不执行副作用，单项失败不阻塞后续曲目。
- 外部 preset 默认使用 `append`；当前没有 Manifest 和删除操作，未知目标对象不会被自动删除。
- workspace 迁移采用复制而非删除：相同内容重复执行时跳过，目标存在不同内容时失败，旧文件保留为恢复依据。

## Remaining implementation work

- 已按《模块化单体与端口适配器 Spec》完成旧流水线迁移接缝：`sync`/`pull`/`process`/`reindex` 已迁为 application 用例（`SyncUseCase`/`ProcessUseCase`/`PipelineUseCase`），源端 SDK 端口化（`ports/source.py`），CLI 组装收敛到 `bootstrap.build_pipeline`，`services/` 目录已删除。
- 后续可选：application 用例的 Rich 输出剥离（spec Completion Notes 已注明）；Manifest 决策完成后的 managed 目标清理；MediaResolver 按需生成与目标元数据/歌词端口。

## Out of scope

- 双向同步。
- 远程设备和播放器适配器。
- 目标端 Manifest 的实现。
- 每个 Preset 的同步历史持久化。
- 完整 observe/plan/reconcile 目标差异引擎。
- 自动回滚所有自定义操作。

## Open tickets

开放ticket快照位于 `tickets/`。其中 01–07 和 09 的核心决策已由实现和测试落地，但ticket文件与 GitHub Issue 状态尚未自动关闭；08 仍对应旧流水线迁移接缝。ticket通过 frontmatter 的 `depends_on` 表达前置决策，正式状态和执行跟踪以对应的 GitHub Issues 为准。

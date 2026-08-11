# 设计：旧流水线迁入 media_store 新布局（C 阶段实施设计）

日期：2026-08-12
状态：已实施（2026-08-12，C 阶段全部完成，含阶段 0-6 与 Task 1-8）

> 本设计承接 `docs/superpowers/specs/2026-08-12-old-pipeline-migration-handoff.md`（交接文档），是 C 阶段实施的正式设计。前置条件（A+B+E 修复）已于 2026-08-12 完成（JSON 退役、WorkspacePaths 单一来源、架构检查测试等均已落地）。

## 背景与目标

旧命令流（sync/pull/process）的文件布局仍停留在旧 `downloads/` 布局，与新链路（target-sync）的 media_store 布局不一致。目标：旧流水线写入路径全部迁入新布局，`downloads/` 路径从代码库彻底消失。

- canonical 文件：`downloads/` → `media_store/<track_id>/audio/`
- 下载缓存：`downloads/cache/` → `cache/`
- 解密临时目录：`workspace/decoded` → `cache/decoded/`
- 同批完成 Rich 输出剥离（用例只返回结构化结果，打印归 CLI）

## 已确认决策（含用户明确指示）

1. **canonical 直写**：`organizer.route_audio` 的 output_dir 直接传 `media_asset_path(track_id, "audio")`，复用 route_audio 现有幂等/force 语义，无额外 I/O。不经 FileMediaStore.put（其 copy 语义会多一次落盘，且冲突报错语义与旧行为不符）。
2. **Rich 剥离同批**：sync/process/pipeline 用例去掉 `shared/tui_progress` 依赖，结构化结果返回，打印归 CLI。
3. **删除迁移机制**：WorkspaceMigration、`msv migrate` 命令、legacy JSON 导入（synced_tracks/playlists/songs.json）、`state_dir` 全部退役。
4. **删除 reindex**：reindex 命令与 `rebuild_index`/`_record_rebuilt_state` 及配套辅助、测试删除；`link`（link_only，DB→library）保留为唯一重建视图方向。
5. **旧工作区完全忽略**：个人项目，无外部用户，不提供任何旧数据升级路径（重新 sync 即完整重建）。downloads/ 旧文件保留于磁盘，不清理不提示。

## 目标布局与数据流

```
sync     downloader → cache/<raw>                    （原 downloads/cache/）
process  decrypt → cache/decoded/                    （原 workspace/decoded）
         route_audio(output_dir=media_store/<tid>/audio/)
              └─ 12345.flac / 12345_192k.mp3  ← 直写，命名不变
         .lrc 写同目录（audio_src.with_name 自动跟随）
link     DB → library（find_canonical_for_spec 改查 media_store/<tid>/audio/）
删除     _prune_stale_tracks / PlaylistUseCase.remove_* → 删 media_store/<tid>/audio/* + library 硬链接
```

路径单一来源：`WorkspacePaths.media_asset_path(track_id, asset_type, filename)`（workspace.py:57-58）。

## 变更清单（依赖序）

### 阶段 0：删除 reindex

- `PipelineUseCase.rebuild_index`（pipeline_use_case.py:79-188）、`_record_rebuilt_state`（:190-227）、`_guess_spec_from_filename`（:423-441）、`_abs_path`（:444-447）
- CLI `reindex` 命令分支（cli/main.py:282-290）
- `tests/test_reindex_to_sqlite.py` 删除
- 文档：AGENTS.md「旧命令流（sync/pull/process/reindex）」、README reindex 条目
- 连带：`build_audio_asset_from_file` 的 `source="pipeline:reindex"` 调用点消失（函数保留，process 仍用）

### 阶段 1：缓存路径

- 下载缓存：`sync_use_case.py:548` `downloader.download_track(..., self.cfg.downloads_cache_dir)` → `self.paths.cache`
- 解密临时目录：`process_use_case.py:214` `self.cfg.workspace_path / "decoded"` → `self.paths.cache / "decoded"`
- 用例内统一 `self.paths = WorkspacePaths(cfg.workspace_path)`，替代全部 `cfg.downloads_dir`/`cfg.downloads_cache_dir` 访问

### 阶段 2：canonical 落位

- `process_use_case.py:192` `is_canonical` 判定：`raw_file.parent.resolve() == downloads_dir` → parent 是 `media_store/<tid>/audio` 且 stem 为数字
- `process_use_case.py:203-217` `route_audio` output_dir → `self.paths.media_asset_path(track_id, "audio")`

### 阶段 3：查找/删除/扫描迁移

- `SyncUseCase.find_canonical_for_spec`（sync_use_case.py:344-369）：查 `media_store/<tid>/audio/`，命名候选不变（`<tid>[_[bitrate]][ext]`）
- `SyncUseCase._prune_stale_tracks`（:443-506）：删除 `media_store/<tid>/audio/` 下全部文件（各 ext、bitrate 变体、.lrc）+ inode 匹配 library 链接
- `ProcessUseCase._iter_downloads`（:444-450）→ `self.paths.cache`；`_scan_canonical_files`（:452-469）→ 扫描 `media_store/*/audio/`
- `PipelineUseCase._cleanup_uncategorized_orphans`（:369-420）：inode 映射改扫 media_store
- `PlaylistUseCase.remove_playlist`（playlist_use_case.py:76-94）、`remove_song`（:143-147）：删除 canonical 改到 media_store；:15 注释删除
- `source_state.py:51` 自述注释删除；资产路径统一绝对路径（`_record_rebuilt_state` 已随 reindex 删除；`build_audio_asset_from_file` 本就存绝对路径，与 FileMediaStore.put 一致）

### 阶段 4：删除迁移机制（migrate 退役）

- `WorkspaceMigration` 类、`MigrationReport`、`_LEGACY_AUDIO_RE`、`_AUDIO_FORMATS`、`_read_json`（workspace.py:16-17, 61-213）
- `WorkspacePaths.legacy_downloads` 属性（workspace.py:50-51）
- `msv migrate` CLI 命令、`build_workspace_migrator`（bootstrap.py:77-81）
- `Config.load` legacy playlist_ids 中间态（config.py:217-226）、`_extract_legacy_playlist_ids`（config.py:353-367）
- `Config.state_dir` 属性（config.py:66）、`ensure_dirs` 不再创建 downloads/state 目录（config.py:88）
- `Config.downloads_dir`/`downloads_cache_dir` 属性（config.py:49-54）退役
- 文档：README.md:54,88 migrate 条目、AGENTS.md:54 命令流描述
- 测试：`tests/test_workspace_migration.py` 整删；test_dry_run.py legacy 中间态用例删

### 阶段 5：Rich 剥离

**原则**：对齐 SyncEngine → SyncRunResult 先例，用例零 Rich 依赖（不 import `shared/tui_progress`），打印归 CLI。

application 层新增结构化结果（frozen dataclass）：

```python
# sync_use_case.py
@dataclass(frozen=True, slots=True)
class SyncResult:
    downloaded: tuple[DownloadedTrack, ...]   # 供 process 阶段消费（原返回 list）
    added: int          # 新增下载成功数
    no_url: int         # 无直链跳过
    pruned: int         # 远端删除清理数
    track_count: int
    playlist_count: int
    dry_run_plan: dict | None = None   # dry-run 时携带完整计划（with_url/no_url/pruned/moves/renames/stale_index）

# process_use_case.py
@dataclass(frozen=True, slots=True)
class ProcessResult:
    processed: int
    skipped: int
    failed: int

# pipeline_use_case.py
@dataclass(frozen=True, slots=True)
class PipelineResult:
    downloaded: int
    processed: int
    pruned: int
    dry_run_plan: dict | None = None
```

- `run_sync` 返回 `SyncResult`（取代 `list[DownloadedTrack]`）；`run_process` 返回 `ProcessResult`；`run_pipeline` 返回 `PipelineResult`（汇总 sync+process）；`link_only` 保持 `(linked_tracks, playlist_count)` 返回，仅去掉内部打印
- **进度条**：用例签名加可选 `progress: ProgressReporter | None = None`；application 层定义 Protocol（begin/advance/end），CLI 用适配器包住现有 `BatchProgress` 传入；无回调时用例静默
- **打印迁移**：`SyncUseCase._print_dry_run_plan` 与各摘要/完成行（"从 N 个歌单同步 M 首"、"重建完成"、"链接完成"、"完成"等）移到 cli/main.py 渲染函数（输入结果对象）；dry_run_plan 计算仍在用例内（涉及 API 查询与状态推导）
- **收尾**：`shared/tui_progress` 仅剩 cli/ 引用；AGENTS.md 已知偏离声明更新为「已修复」；KeyboardInterrupt 与退出码语义不变（用例内中断处理不动）

### 阶段 6：测试与文档收尾

- fixture 路径换新布局：test_pipeline_to_sqlite、test_playlist_reconciliation、test_source_state_recorder、test_dry_run、test_playlist_use_case、test_config_model（:112 ensure_dirs 目录断言翻转：不再断言 downloads/state 存在）
- 断言 `SyncResult` 字段（原 downloaded 返回值的断言改为 `.downloaded`）
- tests/ 已确认无 Rich 输出断言（无 capsys/capfd/BatchProgress 断言），剥离的测试面小
- README/AGENTS.md/wayfinder 同步（migrate、reindex 条目删除；AGENTS.md 新布局章节写清 link 方向 = DB→library）

## 完成定义（DoD）

- [ ] sync/process 运行后 canonical 落在 `media_store/<track_id>/audio/`；`downloads/` 不再产生新文件、不再被创建
- [ ] 下载缓存落在 `cache/`，`keep_downloads` 清理作用于新位置
- [ ] reindex 命令与 `rebuild_index`/`_record_rebuilt_state` 及测试删除
- [ ] WorkspaceMigration / `msv migrate` / legacy JSON 导入 / `state_dir` 删除，`downloads` 路径从代码库消失
- [ ] Rich 剥离完成：`shared/tui_progress` 仅被 cli/ 引用，AGENTS.md 已知偏离更新为已修复
- [ ] `source_state.py:49-52` 自述注释删除
- [ ] 全量 pytest（≥204 项）绿 + `python -m ruff check src/ tests/` 通过

## 风险与注意事项

- 阶段 3 的 `_prune_stale_tracks`/`remove_playlist` 删除逻辑在 media_store 下的路径拼装需逐一核对（bitrate 变体、.lrc 后缀、inode 匹配），避免误删其他曲目
- Rich 剥离改用例签名（run_sync 返回类型变化），下游调用点（pipeline_use_case、cli）同步更新；测试构造用例处的 `dry_run` 参数不变
- `link_only` 与 `_handle_playlist_rename` 共享 `find_canonical_for_spec`，改一处两处生效
- 资产路径统一为绝对路径后，`_cleanup_stale_state` 的 `asset.path.exists()` 检查更健壮（原相对路径依赖 cwd）
- 删除 `state_dir` 前确认 test_dry_run.py 无其他 state/ 断言残留

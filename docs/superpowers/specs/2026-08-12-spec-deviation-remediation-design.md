# 设计：Spec 落实偏差修复（A+B+E）与旧流水线迁移交接文档

日期：2026-08-12
状态：已批准（用户确认：范围 A+B+E、C 另行规划且先产出交接文档；执行方式方案 1 混合式；gh issue 实际关闭；架构检查测试选择收紧顶层重导出）

## 背景与动机

2026-08-12 并行委派 7 个子代理对 `docs/specs/` 下 9 个 spec 做了落实审计（2026-08-05 系列）。结论：9 个 spec 中 6 个落实度 ≥80%，核心决策全部落地（204 项测试佐证），无静默覆盖 spec 的情况。但存在以下待修项，分五类：

- **A 文档/跟踪同步**：spec 头状态「规划中」未更新、wayfinder 测试数口径过期（163 vs 204）、Open tickets 01-09 未关闭、AGENTS.md 偏离声明不全（organizer.py warn、cli 组装未声明）。
- **B 架构合规修复**：`cli/main.py` 3 处直接组装具体依赖；`cli/playlist.py` 绕过用例直连 SQLite 且与 JSON 双写；songs.json/playlists.json 未完全退役；Config/WorkspacePaths 双份路径定义。
- **C 旧流水线迁入新布局**（另行规划）：旧流水线 canonical 仍落 `downloads/`，未迁入 media_store。
- **D 功能实现**（不纳入本次）：manifest、同步历史、删除策略行为、按需媒体生成、歌词进快照、script_hash——均为 spec/wayfinder 声明的延期项。
- **E 测试补全**：SnapshotMediaResolver 零直接单测、越界/冲突/多余文件保留断言缺失、取消场景无测试、无架构检查测试。

## 范围

**纳入**：A + B + E，以及阶段 0 的 C 交接文档。
**排除**：C 的实施（迁移旧流水线）、D 全部功能实现。
**不排除的顺带项**：E 中「幂等重复执行与 CLI 退出码测试」。

## 阶段 0 — C 交接文档（第一交付）

**产物**：`docs/specs/2026-08-12-old-pipeline-migration-handoff.md`（新文件，单独 commit）

**内容**：
- 目标：旧流水线（sync/process/reindex）canonical 文件 → `media_store/<track_id>/audio/`；`downloads/cache` → `cache/`；reindex 基于新布局；`downloads/` 不再产生新文件。
- 现状快照（证据行号，随审计结果写入）：
  - `src/musicvault/application/source_state.py:49-52` 自述「canonical 文件仍落在旧 downloads 布局；待流水线迁到 media_store 后再更新路径」
  - `src/musicvault/application/sync_use_case.py:549` 下载缓存仍写 `downloads_cache_dir`
  - `src/musicvault/application/pipeline_use_case.py:88-98, 133-155` reindex 区分 downloads canonical / downloads/cache / library（旧布局）
  - `src/musicvault/application/sync_use_case.py:444-507` 删除 canonical 逻辑位于 downloads
  - `src/musicvault/application/process_use_case.py:442-448` 缓存清理作用于 downloads_cache_dir
  - `src/musicvault/adapters/filesystem/workspace.py:146-162` migrate 一次性承接旧 downloads/cache
- 顺带项标注：Rich 输出剥离（同批文件）、hash 损坏检测、歌词进快照——标注「可同批」或「另立」。
- 测试影响面：test_pipeline_to_sqlite / test_reindex_to_sqlite / test_workspace_migration 等 fixture 路径更新。
- 完成定义（DoD）：sync/process/reindex 后文件落 media_store；`downloads/` 不再产生新文件；全量测试绿。

## 阶段 A — 文档/跟踪同步（委派 1 个子代理）

- 逐个核对各 spec 文件头「状态」，仅更新与 wayfinder「已完成」一致者（如 workspace-media-store、source-snapshot 等）。
- wayfinder `docs/wayfinder/2026-08-05-musicvault-refactor-map.md`：测试数 163→204；Open tickets 01-07、09 已落地、08（旧流水线接缝）已完成。
- **已确认：用 `gh issue close` 实际关闭已落地 ticket 对应的 GitHub issue**（外部动作，用户已批准）。关闭前先用 `gh issue view` 核对，非本次实现范畴的 ticket 不关。
- AGENTS.md：补 organizer.py warn 输出偏离声明（`src/musicvault/adapters/processors/organizer.py:8` 用 `shared.output.warn`，不在原声明范围内）。此修改只「补声明」；cli 组装偏离的「已修复」标注在 B 完成后由收尾步骤更新（见验证与完成定义）。

## 阶段 B — 架构合规修复（主循环串行；每步全量 pytest + ruff + commit）

依赖顺序：B1 → B2 → B3（B2 独立于 B1，但保持串行以便回归定位）。

### B1 组装收敛

- `cli/main.py` 3 处直接 new 适配器：
  - migrate 路径（main.py:234-239 new WorkspacePaths/SQLiteState/SQLiteStateRepository/WorkspaceMigration）→ 收敛到 bootstrap
  - target-sync 路径（main.py:254-267 new FilesystemTarget + SyncEngine）→ bootstrap 新增 `build_target_sync_pipeline`（组装 FilesystemTarget(library)、SyncEngine、快照读取与 registrations 的现有装配均移入）
  - 登录路径（main.py:391-393 new NeteaseClient）→ 复用 `build_source_client`
- 保持 CLI 的 KeyboardInterrupt 处理与退出码逻辑不变。

### B2 JSON 全部退役 + playlist 改造（强耦合，一起做）

- songs.json：sync 单曲输入改走 SQLite `managed_songs` 表；Config 的 add/remove 逻辑（`core/config.py:99-122, 136-145`）迁移到 application 用例；CLI 补 song 管理命令（`msv song add/remove/list`）。
- playlists.json：`sync_use_case.py:121` 停止写回；歌单索引走 SQLite。
- `cli/playlist.py:150` 直连 `SQLiteStateRepository` → 走 application 用例（新增/扩展用例承载 playlist 与 song 管理）。
- 旧 JSON 读写代码删除；一次性导入已由 `WorkspaceMigration._import_legacy_state`（`adapters/filesystem/workspace.py:164-201`）覆盖，无需兼容层。
- 相关测试更新：删除/改写断言 JSON 不再产生的测试仍应保留（断言行为正确）。

### B3 路径单一化

- `Config.ensure_dirs`（`core/config.py:83-95`）与 `WorkspacePaths.ensure`（`adapters/filesystem/workspace.py:20-58`）合并，WorkspacePaths 为单一来源；Config 侧不再重复定义布局（保留委托或移除重复属性，以最小改动为准）。

## 阶段 E — 测试补全

### E1 与 A 并行（独立子代理）

- 新建 `tests/test_media_resolver.py`：SnapshotMediaResolver / MediaRequest 直接单测（缺失资产返回 None、命中快照资产返回 MediaAsset、跨 track 隔离）。
- FilesystemTarget 安全断言：越界路径抛 ValueError、覆盖冲突抛 FileExistsError、同内容幂等跳过、多余文件保留（append 语义）。
- 取消场景测试：KeyboardInterrupt 处理（CLI 层 main.py:297-299, 346-348 有处理但无测试）。
- 幂等重复执行（同内容不重复写）与 CLI 退出码（失败返回 1）测试。

### E2 B 后（主循环）

- 架构检查测试：
  - 预设脚本只 import `musicvault.preset_api.v1`（用现有测试内联脚本样式断言）。
  - application/ 不 import sqlite3、rich、网易云 SDK；adapters 不 import application/preset_api。
  - **已确认处置：收紧 `preset_api/__init__.py`**——顶层只保留 `v1` 子模块引用，公开面锁定在 v1；实施前先 grep 确认无内部代码依赖顶层重导出的符号（含 tests/、preset 目录样例），再移除顶层 re-export。

## 验证与完成定义

- 每阶段（B 每步、A/E 完成后）：全量 pytest（204 项基线，只增不减）+ `python -m ruff check src/ tests/` + `python -m musicvault --help` 冒烟。
- B2 完成后跑 `python -m pytest tests/ -q` 全量并确认 JSON 不再产生的断言仍绿。
- 收尾：AGENTS.md / wayfinder 状态更新（cli 组装偏离标注「已修复」、Open tickets 关闭），收尾 commit。

## 风险与注意事项

- B2 改变用户工作流（编辑 JSON → CLI 命令）：用户已确认接受。
- B1 动 cli/main.py 多处，注意保持退出码与 KeyboardInterrupt 语义。
- 收紧 `__init__.py` 前必须先 grep 确认无内部依赖，防止破坏现有 preset 脚本。
- gh issue 关闭前逐个 `gh issue view` 核对，非本次实现范畴的 ticket 不关。
- 子代理与主循环并行期间，B 主干不触碰子代理正在修改的文件（A 改 docs/，E1 新建测试文件，均与 B 无交集）。

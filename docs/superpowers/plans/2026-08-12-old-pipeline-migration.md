# 旧流水线迁入 media_store 新布局 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把旧流水线（sync/pull/process）的文件写入迁入 `media_store/<track_id>/audio/` 与 `cache/`，同批完成 Rich 输出剥离，并删除 reindex 与 WorkspaceMigration 一次性迁移机制。

**Architecture:** canonical 文件由 `organizer.route_audio` 直写 `media_store/<track_id>/audio/`（`WorkspacePaths.media_asset_path` 单一来源，命名不变）；下载缓存与解密临时目录落 `cache/`；三个旧用例（SyncUseCase/ProcessUseCase/PipelineUseCase）返回结构化结果、打印归 CLI（对齐新链路 SyncEngine → SyncRunResult 先例）；reindex 命令与 WorkspaceMigration/migrate/legacy JSON 导入整体删除。

**Tech Stack:** Python 3.12+、SQLite（SQLiteStateRepository）、Rich（仅限 cli/ 层）、pytest + unittest.mock、ruff（line-length=120）。

## Global Constraints

- 注释、docstring、commit message 一律中文；领域术语遵循 `CONTEXT.md` 术语表。
- 每任务结束必须全量回归：`python -m pytest tests/ -q`（基线 204 项，只增不减）+ `python -m ruff check src/ tests/`。
- application 层不得 import `shared/tui_progress`（Rich）；`shared/tui_progress` 仅允许被 cli/ 引用。
- 用例内路径统一经 `WorkspacePaths(cfg.workspace_path)` 访问（`media_asset_path`/`cache`）；`Config.downloads_dir`/`downloads_cache_dir`/`state_dir` 退役。
- 所有行号基于 2026-08-12 代码快照；实施前若与当前文件不符，先按符号名定位。

---

### Task 1: 删除 reindex（阶段 0）

**Files:**
- Modify: `src/musicvault/application/pipeline_use_case.py`（删 `rebuild_index`、`_record_rebuilt_state`、`_guess_spec_from_filename`、`_abs_path`）
- Modify: `src/musicvault/cli/main.py`（删 reindex parser 与命令分支）
- Delete: `tests/test_reindex_to_sqlite.py`
- Modify: `AGENTS.md`、`README.md`（reindex 引用）

**Interfaces:**
- Consumes: 无
- Produces: `PipelineUseCase` 仅剩 `link_only`/`run_pipeline`/`_cleanup_uncategorized_orphans` 及 `__init__`；`build_pipeline`（bootstrap）签名不变，供 Task 6/7 使用。

- [ ] **Step 1: 确认引用面**

Run: `grep -rn "rebuild_index\|reindex" src/ tests/ README.md AGENTS.md`
Expected: 引用点仅限 pipeline_use_case.py、cli/main.py（parser :144-145 与分支 :277-290）、test_reindex_to_sqlite.py、README/AGENTS 文档。若出现其他代码引用，先在本步将其一并列入删除。

- [ ] **Step 2: 删除 pipeline_use_case.py 中 reindex 实现**

删除以下方法（整段，含 docstring）：
- `rebuild_index`（原 :79-188）
- `_record_rebuilt_state`（原 :190-227）
- `_guess_spec_from_filename`（原 :423-441）
- `_abs_path`（原 :444-447）

同步清理 import：`build_audio_asset_from_file`（:13）、`time`（:5）若不再使用则删除；`os`（:4）保留（`__init__` 用 `os.cpu_count()`）。

- [ ] **Step 3: 删除 cli/main.py 中 reindex 命令**

删除 parser（原 :144-145）：
```python
    reindex = sub.add_parser("reindex", help="重建索引", description="通过 downloads 目录中的文件重建已下载索引")
    _add_common_args(reindex, include_dry_run=False)
```
删除命令分支（原 :277-290）：
```python
    # reindex 不需要 API，直接重建索引
    if args.command == "reindex":
        workspace = getattr(args, "workspace", None)
        if workspace is not None:
            cfg.workspace = workspace
        from musicvault.application.bootstrap import build_pipeline

        service = build_pipeline(cfg)
        try:
            service.rebuild_index()
        except KeyboardInterrupt:
            output_info("已取消")
            return 130
        return 0
```

- [ ] **Step 4: 删除测试与文档引用**

```bash
git rm tests/test_reindex_to_sqlite.py
```
AGENTS.md「旧命令流（sync/pull/process/reindex）」→「旧命令流（sync/pull/process）」；README 中 reindex 相关行删除。

- [ ] **Step 5: 全量回归 + 提交**

```bash
python -m pytest tests/ -q
python -m ruff check src/ tests/
git add -A
git commit -m "refactor: 删除 reindex 命令与 rebuild_index（DB 恢复路径由重新 sync 替代）"
```
Expected: 全量绿（约 199 项，比基线少 5 项 test_reindex_to_sqlite）+ ruff 通过。

---

### Task 2: 缓存路径迁移（阶段 1）

**Files:**
- Modify: `src/musicvault/application/sync_use_case.py`（`__init__` 加 `self.paths`；下载缓存 → `paths.cache`）
- Modify: `src/musicvault/application/process_use_case.py`（`__init__` 加 `self.paths`；decoded → `paths.cache / "decoded"`）
- Test: `tests/test_pipeline_to_sqlite.py`、`tests/test_dry_run.py`（下载缓存 fixture）

**Interfaces:**
- Consumes: `WorkspacePaths`（`adapters/filesystem/workspace.py`，frozen dataclass，`cache` 属性）
- Produces: `SyncUseCase.__init__` 签名不变；`SyncUseCase.run_sync` 的下载批次把 `downloader.download_track` 的 dest 参数传 `self.paths.cache`。Task 3 依赖 `self.paths` 已就位。

- [ ] **Step 1: 写失败测试（断言下载缓存落 cache/）**

在 `tests/test_dry_run.py` 的 `TestSyncDryRun.test_normal_mode_writes_to_sqlite`（原 :107-134）追加断言（mock downloader 记录调用参数）：

```python
        # 下载缓存落在新 cache/ 目录（不再写 downloads/cache/）
        call_args = downloader.download_track.call_args
        assert call_args.args[2] == cfg.cache_dir
```

Run: `python -m pytest tests/test_dry_run.py -q`
Expected: FAIL（实现仍传 `cfg.downloads_cache_dir`）。

- [ ] **Step 2: 实现——SyncUseCase 使用 WorkspacePaths**

`sync_use_case.py` `__init__`（:40 附近）加入：
```python
        self.paths = WorkspacePaths(cfg.workspace_path)
```
import 增加 `from musicvault.adapters.filesystem.workspace import WorkspacePaths`。

`_run_download_batch`（原 :548）：
```python
                pool.submit(self.downloader.download_track, track, url, self.cfg.downloads_cache_dir): (idx, track)
```
→
```python
                pool.submit(self.downloader.download_track, track, url, self.paths.cache): (idx, track)
```

- [ ] **Step 3: 实现——ProcessUseCase 的 decoded 临时目录**

`process_use_case.py` `__init__`（:47 附近）加入：
```python
        self.paths = WorkspacePaths(cfg.workspace_path)
```
import 增加 `from musicvault.adapters.filesystem.workspace import WorkspacePaths`。

`_process_file`（原 :214）：
```python
            decoded = self.decryptor.decrypt_if_needed(downloaded, self.cfg.workspace_path / "decoded")
```
→
```python
            decoded = self.decryptor.decrypt_if_needed(downloaded, self.paths.cache / "decoded")
```

- [ ] **Step 4: 更新下载缓存 fixture（mock 返回值语义对齐）**

替换规则（tests/ 全目录）：
- `str(cfg.downloads_cache_dir / "<name>")` → `str(cfg.cache_dir / "<name>")`
- `cfg.downloads_cache_dir.mkdir(parents=True)` → `cfg.cache_dir.mkdir(parents=True)`

已知位置：`test_pipeline_to_sqlite.py` :39, :49, :68, :87, :104, :133, :156, :167, :197, :207, :221, :282, :317, :320；`test_dry_run.py` :56, :88, :112, :123, :142, :171。
Run: `grep -rn "downloads_cache_dir" tests/ src/` 确认无残留（Task 4 退役 Config 属性前，src 中 `cfg.downloads_cache_dir` 应已清零——本任务后 `downloads_cache_dir` 仅剩 core/config.py 定义本身）。

- [ ] **Step 5: 跑测试验证**

Run: `python -m pytest tests/ -q`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor: 下载缓存与解密临时目录迁入 cache/（WorkspacePaths 单一来源）"
```

---

### Task 3: canonical 落位 media_store（阶段 2）

**Files:**
- Modify: `src/musicvault/application/process_use_case.py`（`is_canonical` 判定 :192；`route_audio` output_dir :203-217）
- Test: `tests/test_pipeline_to_sqlite.py`、`tests/test_dry_run.py`、`tests/test_playlist_use_case.py`、`tests/test_playlist_reconciliation.py`、`tests/test_source_state_recorder.py`（canonical fixture）

**Interfaces:**
- Consumes: Task 2 的 `self.paths`；`WorkspacePaths.media_asset_path(track_id, asset_type, filename)`（workspace.py:57-58）
- Produces: canonical 文件落 `media_store/<tid>/audio/<tid>[_[bitrate]][ext]`；`ProcessUseCase._process_file` 返回 `dict[spec_key, Path]`（路径为 media_store 下）——Task 4 的 `find_canonical_for_spec` 依赖此布局。

- [ ] **Step 1: 写失败测试（canonical 落 media_store）**

`tests/test_pipeline_to_sqlite.py::test_process_records_media_assets_to_sqlite`（原 :100-123）改 fixture：

```python
    canonical = cfg.media_store_dir / "333" / "audio" / "333.flac"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"fake flac")
```
`asset.path == canonical` 断言（:122）不变，此时应为 FAIL（实现仍写 downloads，登记路径不匹配）。

Run: `python -m pytest tests/test_pipeline_to_sqlite.py::test_process_records_media_assets_to_sqlite -q`
Expected: FAIL（asset.path 不匹配）。

- [ ] **Step 2: 实现——canonical 直写 media_store**

`process_use_case.py::_process_file`（原 :192）：
```python
        is_canonical = raw_file.parent.resolve() == self.cfg.downloads_dir.resolve() and raw_file.stem.isdigit()
```
→
```python
        is_canonical = (
            raw_file.parent.parent.name == "audio"
            and raw_file.parent.parent.parent == self.paths.media_store
            and raw_file.stem.isdigit()
        )
```
（parent 形态为 `<ws>/media_store/<tid>/audio`；`parent.parent.parent == media_store` 校验根，`parent.parent.name.isdigit()` 由 stem.isdigit 与目录结构共同保证，`audio` 段校验防止误判其他资产类型。）

`:203-204`：
```python
                    result = self.organizer.route_audio(
                        raw_file, track_info, self.cfg.downloads_dir, {spec}, force=force
                    )
```
→
```python
                    result = self.organizer.route_audio(
                        raw_file, track_info, self.paths.media_asset_path(track_id, "audio", "").parent, {spec},
                        force=force,
                    )
```
（`media_asset_path(track_id, "audio", "")` 返回 `<ws>/media_store/<tid>/audio/<空名>`，`.parent` 即 `<ws>/media_store/<tid>/audio`；filename 空串仅用于取目录。若嫌取巧，直接 `self.paths.media_store / str(track_id) / "audio"` 亦可，但保持单一来源优先。）

`:216`：
```python
            raw_result = self.organizer.route_audio(
                decoded, track_info, self.cfg.downloads_dir, audio_specs, force=force
            )
```
→
```python
            raw_result = self.organizer.route_audio(
                decoded, track_info, self.paths.media_asset_path(track_id, "audio", "").parent, audio_specs, force=force
            )
```

- [ ] **Step 3: 跑目标测试验证**

Run: `python -m pytest tests/test_pipeline_to_sqlite.py::test_process_records_media_assets_to_sqlite -q`
Expected: PASS。

- [ ] **Step 4: 更新全部 canonical fixture**

替换规则（tests/ 全目录）：canonical 文件路径
`cfg.downloads_dir / "<tid>.flac"` → `cfg.media_store_dir / "<tid>" / "audio" / "<tid>.flac"`（需先 `canonical.parent.mkdir(parents=True, exist_ok=True)`；`<tid>` 同理含 bitrate 变体如 `333_192k.mp3`）。
`cfg.downloads_dir.mkdir(parents=True)` → `cfg.media_store_dir.mkdir(parents=True)`。

已知位置：`test_pipeline_to_sqlite.py` :38, :67, :86, :103, :106, :132, :155, :174, :196, :221, :264, :283, :287-290；`test_dry_run.py` :55, :87, :91-92, :111, :141, :144, :170, :172；`test_playlist_use_case.py` :63-64, :102-103；`test_playlist_reconciliation.py` :188-218；`test_source_state_recorder.py` canonical 相关行（grep `downloads_dir` 定位）。

Run: `python -m pytest tests/ -q`
Expected: 全绿。`grep -rn "downloads_dir" tests/` 仅剩 Task 4 才清理的 `state_dir` 相邻残留与误伤，逐一确认。

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "refactor: canonical 文件直写 media_store/<track_id>/audio/（route_audio output_dir 迁移）"
```

---

### Task 4: canonical 查找/删除/扫描迁移（阶段 3）

**Files:**
- Modify: `src/musicvault/application/sync_use_case.py`（`find_canonical_for_spec` :344-369、`_prune_stale_tracks` :443-506）
- Modify: `src/musicvault/application/process_use_case.py`（`_iter_downloads` :444-450、`_scan_canonical_files` :452-469）
- Modify: `src/musicvault/application/pipeline_use_case.py`（`_cleanup_uncategorized_orphans` :369-420）
- Modify: `src/musicvault/application/playlist_use_case.py`（`remove_playlist` :76-94、`remove_song` :143-147、:15 注释）
- Modify: `src/musicvault/application/source_state.py`（:51 自述注释）
- Test: `tests/test_dry_run.py`（新增 prune 实际删除测试）

**Interfaces:**
- Consumes: Task 3 的 canonical 布局（`media_store/<tid>/audio/`）
- Produces: `SyncUseCase.find_canonical_for_spec(track_id, spec_key) -> Path | None`（查 media_store）——`link_only` 与 `_handle_playlist_rename` 复用。

- [ ] **Step 1: 写失败测试（prune 删除 media_store 中 canonical）**

`tests/test_dry_run.py` 新增（追加到 `TestSyncDryRun` 类内）：

```python
    def test_prune_deletes_canonical_from_media_store(self, tmp_path: Path) -> None:
        """非 dry-run：远端已删除曲目的 canonical 从 media_store 删除，library 链接同步删除。"""
        cfg = _make_cfg(tmp_path)
        cfg.media_store_dir.mkdir(parents=True)
        cfg.cache_dir.mkdir(parents=True)
        _seed_synced(cfg, {111: [10]})
        canonical = cfg.media_store_dir / "111" / "audio" / "111.flac"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake flac")

        api = MagicMock()
        api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
        api.get_playlist_tracks.return_value = []  # 远端已无 111

        svc = SyncUseCase(cfg, api, MagicMock(), workers=2, dry_run=False, state=_repository(cfg))
        svc.run_sync("cookie", playlist_ids=[10])

        assert not canonical.exists()
        assert 111 not in svc.load_synced_state()
```

Run: `python -m pytest tests/test_dry_run.py::TestSyncDryRun::test_prune_deletes_canonical_from_media_store -q`
Expected: FAIL（canonical 仍留在磁盘，因 `_prune_stale_tracks` 删的是 downloads 路径）。

- [ ] **Step 2: 实现——find_canonical_for_spec 查 media_store**

`sync_use_case.py::find_canonical_for_spec`（:344-369）整体替换：

```python
    def find_canonical_for_spec(self, track_id: int, spec_key: str) -> Path | None:
        """查找符合指定 spec_key 的 canonical 文件（media_store/<track_id>/audio/ 中）。"""
        audio_dir = self.paths.media_asset_path(track_id, "audio", "").parent
        if not audio_dir.is_dir():
            return None
        if spec_key == "ORIGINAL":
            for ext in (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"):
                p = audio_dir / f"{track_id}{ext}"
                if p.exists():
                    return p
            return None

        parts = spec_key.split("-", 1)
        fmt = parts[0].lower()
        bitrate = parts[1] if len(parts) > 1 else None
        ext_map = {"flac": ".flac", "mp3": ".mp3", "aac": ".m4a", "ogg": ".ogg", "opus": ".opus"}
        ext = ext_map.get(fmt, f".{fmt}")

        # 先尝试带 bitrate 后缀，再尝试无 bitrate
        candidates: list[str] = []
        if bitrate:
            candidates.append(f"{track_id}_{bitrate}{ext}")
        candidates.append(f"{track_id}{ext}")

        for name in candidates:
            p = audio_dir / name
            if p.exists():
                return p
        return None
```

- [ ] **Step 3: 实现——_prune_stale_tracks 删除 media_store**

`sync_use_case.py::_prune_stale_tracks` 中（:460-478）canonical 收集与删除段替换：

```python
            # 收集 canonical 文件 inode（删除前）
            canonical_inodes: set[tuple[int, int]] = set()
            audio_dir = self.paths.media_asset_path(track_id, "audio", "").parent
            if audio_dir.is_dir():
                for f in list(audio_dir.iterdir()):
                    if not f.is_file():
                        continue
                    try:
                        st = f.stat()
                        canonical_inodes.add((st.st_dev, st.st_ino))
                    except OSError:
                        continue
                # 删除 media_store/<tid>/audio/ 下全部文件（各格式、bitrate 变体、.lrc）
                shutil.rmtree(audio_dir)
```
（`audio_dir` 仅含本曲目资产；删除整目录即覆盖所有后缀与 .lrc。`:473` 后原逐 ext unlink 与 `downloads` 前缀扫描段删除。library inode 匹配段 :481-497 不变。）

- [ ] **Step 4: 实现——扫描/清理路径迁移**

`process_use_case.py::_iter_downloads`（:444-450）：`self.cfg.downloads_cache_dir` 两处 → `self.paths.cache`。

`process_use_case.py::_scan_canonical_files`（:452-469）整体替换：

```python
    def _scan_canonical_files(self) -> list[tuple[Path, int]]:
        media_root = self.paths.media_store
        if not media_root.is_dir():
            return []
        seen: set[int] = set()
        result: list[tuple[Path, int]] = []
        for track_dir in sorted(media_root.iterdir()):
            if not track_dir.is_dir() or not track_dir.name.isdigit():
                continue
            track_id = int(track_dir.name)
            audio_dir = track_dir / "audio"
            if not audio_dir.is_dir():
                continue
            for file_path in sorted(audio_dir.iterdir()):
                if not file_path.is_file() or file_path.suffix.lower() not in (".flac", ".mp3"):
                    continue
                stem = file_path.stem.split("_")[0]
                if stem != track_dir.name:
                    continue
                if track_id in seen:
                    continue
                result.append((file_path, track_id))
                seen.add(track_id)
        return result
```
（原逻辑：每个 track_id 只取第一个 canonical 文件。新实现保持：同 track 去重、只认 stem 前缀。）

`pipeline_use_case.py::_cleanup_uncategorized_orphans`（:376-390）inode 映射段替换：

```python
        # 构建 canonical 文件的 inode → track_id 映射
        inode_to_tid: dict[tuple[int, int], int] = {}
        media_root = self.cfg.workspace_path / "media_store"
        if media_root.is_dir():
            for track_dir in media_root.iterdir():
                if not track_dir.is_dir() or not track_dir.name.isdigit():
                    continue
                audio_dir = track_dir / "audio"
                if not audio_dir.is_dir():
                    continue
                for f in audio_dir.iterdir():
                    if not f.is_file():
                        continue
                    try:
                        st = f.stat()
                        inode_to_tid[(st.st_dev, st.st_ino)] = int(track_dir.name)
                    except OSError:
                        continue
```
（此处不用 `self.paths`——PipelineUseCase 未在 Task 2 加该字段。与 Task 2 一致起见补 `self.paths` 亦可，二选一；本计划采用在 `__init__` 补 `self.paths = WorkspacePaths(cfg.workspace_path)` 并全用 `self.paths.media_store`，与 SyncUseCase/ProcessUseCase 对齐。）

`playlist_use_case.py::remove_playlist`（:76-94）：canonical inode 收集与删除段改为遍历 `media_store/<tid>/audio/`（模式同 Task 4 Step 3：遍历收集 inode → `shutil.rmtree(audio_dir)`）；`remove_song`（:146-147）改为：

```python
    def remove_song(self, song_id: int) -> None:
        """移除单曲管理登记并删除其 canonical 文件。"""
        self.state.remove_managed_song(song_id)
        audio_dir = self.paths.media_asset_path(song_id, "audio", "").parent
        if audio_dir.is_dir():
            shutil.rmtree(audio_dir)
```
（`PlaylistUseCase.__init__` 补 `self.paths = WorkspacePaths(cfg.workspace_path)` 与 import；`_SONG_REMOVE_EXTS` 与 `_CANONICAL_EXTS` 常量删除。:15 注释「旧 downloads 布局，C 阶段迁移前保持不变」删除。）

`source_state.py`（:51）：删除自述注释行：
```python
    当前流水线的 canonical 文件仍落在旧 downloads 布局；待流水线迁到 media_store 后再更新路径。
```

- [ ] **Step 5: 跑测试验证**

Run: `python -m pytest tests/ -q`
Expected: 全绿（含新 prune 测试）。`grep -rn "downloads_dir" src/ tests/` 应仅剩 core/config.py 定义与 Task 5 待删的引用。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "refactor: canonical 查找/删除/扫描迁移到 media_store（find_canonical_for_spec、prune、process 本地扫描、PlaylistUseCase 删除）"
```

---

### Task 5: 删除迁移机制（阶段 4）

**Files:**
- Modify: `src/musicvault/adapters/filesystem/workspace.py`（删 `WorkspaceMigration`/`MigrationReport`/`_LEGACY_AUDIO_RE`/`_AUDIO_FORMATS`/`_read_json`、`legacy_downloads` 属性）
- Modify: `src/musicvault/cli/main.py`（删 migrate parser :154-159 与分支 :236-251）
- Modify: `src/musicvault/application/bootstrap.py`（删 `build_workspace_migrator` :77-81）
- Modify: `src/musicvault/core/config.py`（删 `downloads_dir`/`downloads_cache_dir`/`state_dir` 属性、legacy 中间态 :217-226、`_extract_legacy_playlist_ids` :353-367、ensure_dirs :88 中 downloads/state 创建）
- Delete: `tests/test_workspace_migration.py`
- Test: `tests/test_dry_run.py`、`tests/test_pipeline_to_sqlite.py`、`tests/test_config_model.py`（state_dir/JSON 残留）

**Interfaces:**
- Consumes: Task 1-4 完成（`downloads_dir` 无代码引用）
- Produces: `Config` 路径属性只剩新布局（`workspace_path`/`cache_dir`/`media_store_dir`/`library_dir`/`logs_dir`/`state_db_file`/`preset_dir`）；`WorkspacePaths` 仅五区域 + 新方法。

- [ ] **Step 1: 确认引用面**

Run: `grep -rn "WorkspaceMigration\|build_workspace_migrator\|legacy_downloads\|state_dir\|downloads_dir\|downloads_cache_dir\|migrate" src/ tests/ README.md AGENTS.md`
Expected: 引用点如下（逐一核对后删除或保留标注）：
- workspace.py：`WorkspaceMigration` 类/`MigrationReport`/`legacy_downloads`/`_LEGACY_AUDIO_RE`/`_AUDIO_FORMATS`/`_read_json` → 删
- cli/main.py migrate parser 与分支 → 删；bootstrap `build_workspace_migrator` → 删
- config.py `downloads_dir`/`downloads_cache_dir`/`state_dir` 属性、legacy 中间态、`_extract_legacy_playlist_ids`、ensure_dirs → 删
- tests：test_workspace_migration.py → 删；test_dry_run/test_pipeline_to_sqlite/test_config_model 的 state_dir 相关 → 本任务处理
- README/AGENTS.md migrate 条目 → Task 8

- [ ] **Step 2: 删除 workspace.py 迁移代码**

删除：`_LEGACY_AUDIO_RE`（:16）、`_AUDIO_FORMATS`（:17）、`WorkspacePaths.legacy_downloads`（:50-51）、`MigrationReport`（:61-69）、`WorkspaceMigration`（:72-201）、`_read_json`（:204-213）。同步清理 import：`re`（:3）、`shutil`（:4）若不再使用则删；`Track`/`Playlist`/`MediaAsset`（:9-10）若仅迁移代码使用则删；`same_file_content`/`sha256_file`（:11）仅迁移用则删（`WorkspacePaths.media_asset_path` 等保留）。`WorkspacePaths` 仅保留 `root/cache/media_store/library/state_db/logs/ensure/media_asset_path`。

- [ ] **Step 3: 删除 cli 与 bootstrap 的 migrate**

cli/main.py：删 parser（:154-159）：
```python
    migrate = sub.add_parser("migrate", help="迁移 workspace", description="将旧 downloads 音频安全复制到 media_store")
    migrate.add_argument(
        "--config", default=_DEFAULT_CONFIG, help="配置文件路径（可被 MUSIC_VAULT_CONFIG 环境变量覆盖）"
    )
    migrate.add_argument("--workspace", default=None, help="工作目录")
    migrate.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")
```
删分支（:236-251）：
```python
    if args.command == "migrate":
        if getattr(args, "workspace", None) is not None:
            cfg.workspace = args.workspace
        try:
            from musicvault.application.bootstrap import build_workspace_migrator

            report = build_workspace_migrator(cfg).migrate()
        except Exception as error:  # noqa: BLE001 - CLI 将迁移失败转换为非零退出码
            output_error(f"workspace 迁移失败：{error}")
            return 2
        output_success(
            f"迁移完成：复制 {report.copied_assets} 个媒体资产，"
            f"跳过 {report.skipped_assets} 个，缓存复制 {report.copied_cache_files} 个，"
            f"忽略 {report.ignored_files} 个文件"
        )
        return 0
```
bootstrap.py：删 `build_workspace_migrator`（:77-81）及不再使用的 import（`WorkspaceMigration`）。

- [ ] **Step 4: 删除 Config 退役属性与 legacy 中间态**

config.py：
- 删 `downloads_dir`（:48-50）、`downloads_cache_dir`（:52-54）、`state_dir`（:65-67）三个属性
- `ensure_dirs`（:84-91）改为：
```python
    def ensure_dirs(self) -> None:
        # 新布局五区域（cache/media_store/library/logs/state.db）由 WorkspacePaths 单一定义；
        # 此处仅补充 preset 目录。
        WorkspacePaths(self.workspace_path).ensure()
        for preset in self.presets:
            self.preset_dir(preset.name).mkdir(parents=True, exist_ok=True)
```
- 删 `load` 中 legacy 中间态（:217-226）：
```python
            if "playlist_ids" in raw or "playlist_id" in raw:
                # 一次性迁移中间态：写入 state/playlists.json，由 msv migrate 导入 SQLite
                legacy_ids = _extract_legacy_playlist_ids(raw)
                if legacy_ids:
                    cfg.ensure_dirs()
                    index_path = cfg.state_dir / "playlists.json"
                    index = load_json(index_path, {})
                    for pid in legacy_ids:
                        index.setdefault(str(pid), {"name": "", "track_count": 0})
                    save_json(index_path, index)
```
- 删 `_extract_legacy_playlist_ids`（:353-367）；若 `save_json`/`load_json`（:10）不再被 config.py 使用则清理 import
- 删 `cache_dir`/`media_store_dir` 属性上的注释（:58「downloads/cache 作为旧布局保留」改为无注释）

- [ ] **Step 5: 删除/更新测试**

```bash
git rm tests/test_workspace_migration.py
```
替换规则（tests/ 全目录）：
- `cfg.state_dir.mkdir(parents=True)` → 整行删除
- `cfg.state_dir / "synced_tracks.json"` / `"playlists.json"` / `"processed_files.json"` 相关 save_json/断言 → 删除或改写：JSON 不再产生的断言（test_pipeline_to_sqlite.py:214, :274；test_dry_run.py:134）改为 `assert not (cfg.workspace_path / "state").exists()`；test_dry_run.py:59/:145 的 save_json 预置与其断言（:77-78）整段删除（状态已由 `_seed_synced` 经 SQLite 预置）
- `test_config_model.py`（:112-113）：`assert (Path(tmp) / "downloads").is_dir()`、`assert (Path(tmp) / "state").is_dir()` 两行删除

- [ ] **Step 6: 全量回归 + 提交**

```bash
python -m pytest tests/ -q
python -m ruff check src/ tests/
git add -A
git commit -m "refactor: 删除 WorkspaceMigration 与 migrate 命令（legacy JSON 导入、state_dir、downloads 路径退役）"
```
Expected: 全绿（项数较 Task 4 再减 test_workspace_migration 的用例数）+ ruff 通过。

---

### Task 6: Rich 剥离——结构化结果与用例（阶段 5a）

**Files:**
- Create: `src/musicvault/application/progress.py`（`ProgressReporter` Protocol）
- Modify: `src/musicvault/application/sync_use_case.py`（`SyncResult`、run_sync 返回类型、移除 console/BatchProgress/_print_dry_run_plan）
- Modify: `src/musicvault/application/process_use_case.py`（`ProcessResult`、run_process 返回类型、移除 BatchProgress/_print_dry_run_plan）
- Modify: `src/musicvault/application/pipeline_use_case.py`（`PipelineResult`、run_pipeline 返回类型、移除 console/ok）
- Test: `tests/test_dry_run.py`、`tests/test_pipeline_to_sqlite.py`

**Interfaces:**
- Consumes: 无新依赖；`ProgressReporter` 由本任务创建
- Produces（Task 7 消费）:
  - `SyncUseCase.run_sync(cookie: str, playlist_ids: list[int], *, progress: ProgressReporter | None = None) -> SyncResult`
  - `SyncResult`: `downloaded: tuple[DownloadedTrack, ...]`、`added: int`、`no_url: int`、`pruned: int`、`track_count: int`、`playlist_count: int`、`dry_run_plan: dict | None`
  - `ProcessUseCase.run_process(downloaded, force, playlist_index=None, *, progress=None) -> ProcessResult`
  - `ProcessResult`: `processed: int`、`skipped: int`、`failed: int`
  - `PipelineUseCase.run_pipeline(cookie: str, command: str, *, progress: ProgressReporter | None = None) -> PipelineResult`
  - `PipelineResult`: `downloaded: int`、`processed: int`、`pruned: int`、`dry_run_plan: dict | None`
  - `link_only` 返回 `tuple[int, int]` 不变
  - `ProgressReporter`（`application/progress.py`）:
    ```python
    class ProgressReporter(Protocol):
        def begin(self, total: int, phase: str) -> None: ...
        def advance(self, *, success: bool, idx: int, item_name: str) -> None: ...
        def end(self) -> None: ...
    ```

- [ ] **Step 1: 新建 ProgressReporter Protocol**

`src/musicvault/application/progress.py`：
```python
"""进度展示端口：用例只报告进度事件，展示由 CLI（Rich）负责。"""

from __future__ import annotations

from typing import Protocol


class ProgressReporter(Protocol):
    """批量任务的进度报告接口；无展示需求时传 None。"""

    def begin(self, total: int, phase: str) -> None: ...
    def advance(self, *, success: bool, idx: int, item_name: str) -> None: ...
    def end(self) -> None: ...
```

- [ ] **Step 2: 写失败测试（返回类型变化）**

`tests/test_dry_run.py` 更新：
- `TestSyncDryRun.test_new_track_reported_no_writes`（:69-82）：
```python
        result = svc.run_sync("cookie", playlist_ids=[10])

        # 不下载、不写状态
        assert result.downloaded == ()
        downloader.download_track.assert_not_called()
        state_map = svc.load_synced_state()
        assert 222 not in state_map
        # 计划包含新曲目与歌单信息变化
        assert [t.id for t in result.dry_run_plan["with_url"]] == [222]
        assert result.dry_run_plan["pruned"] == []
```
（原 :72 `downloaded == []`、:81-82 `svc.plan[...]` 替换；playlists.json 断言段已在 Task 5 删除）
- `test_normal_mode_writes_to_sqlite`（:129-131）：
```python
        result = svc.run_sync("cookie", playlist_ids=[10])

        assert len(result.downloaded) == 1
```
- `TestProcessDryRun` 用例的 `run_process` 返回值无需断言（调用不变量）。

Run: `python -m pytest tests/test_dry_run.py -q`
Expected: FAIL（`result.downloaded` → AttributeError：list 无 downloaded；`result.dry_run_plan` 同理）。

- [ ] **Step 3: 实现——SyncUseCase**

`sync_use_case.py`：
- import 清理：删 `from musicvault.shared.tui_progress import BatchProgress, console`（:16）；新增 `from musicvault.application.progress import ProgressReporter` 与 `from collections.abc import Sequence`（如需）
- 新增结果类型（模块级）：
```python
@dataclass(frozen=True, slots=True)
class SyncResult:
    """sync 运行的结构化结果：CLI 据此渲染，process 阶段消费 downloaded。"""

    downloaded: tuple[DownloadedTrack, ...]
    added: int = 0
    no_url: int = 0
    pruned: int = 0
    track_count: int = 0
    playlist_count: int = 0
    dry_run_plan: dict | None = None
```
（import `dataclass`——sync_use_case.py 目前无 dataclass import，需新增 `from dataclasses import dataclass`）
- `run_sync(self, cookie: str, playlist_ids: list[int], *, progress: ProgressReporter | None = None) -> SyncResult`：
  - dry_run 分支：`self.plan = {...}` 改局部 `plan = {...}`（:134-141），删除 `self._print_dry_run_plan(...)` 调用（:142），返回 `SyncResult(downloaded=(), dry_run_plan=plan, track_count=len(unique), playlist_count=len(playlist_ids) + (1 if song_ids else 0))`
  - 正常分支：`downloaded = self._sync_tracks(new_tracks, track_playlists, progress)`；删除摘要打印（:149-157）；`added = len(downloaded)`；`no_url` 计数：`_sync_tracks` 内部 skipped 计数——在 `_sync_tracks` 返回 tuple 或在 SyncResult 计算（`_sync_tracks` 中 `skipped` 局部变量改为返回 `(downloaded, skipped)`，或经 `_diff` 推导；本计划采用：`_sync_tracks` 返回 `tuple[list[DownloadedTrack], int]`（downloaded, no_url））
  - 返回 `SyncResult(downloaded=tuple(downloaded), added=added, no_url=no_url, pruned=pruned_count, track_count=len(unique), playlist_count=n_playlists)`
- `_sync_tracks`（:508-531）：加 `progress` 参数透传 `_run_download_batch`；返回 `(downloaded, skipped)`
- `_run_download_batch`（:533-568）：`progress: ProgressReporter | None` 参数；BatchProgress 替换：
```python
        if progress is not None:
            progress.begin(total=total, phase=phase)
        try:
            ...
            for future in as_completed(future_map):
                ...
                    progress.advance(success=True, idx=idx, item_name=track.name)
                except Exception as exc:
                    progress.advance(success=False, idx=idx, item_name=track.name)
                    ...
        finally:
            if progress is not None:
                progress.end()
```
（`with ThreadPoolExecutor(...) as pool, BatchProgress(...)` 改为 `with ThreadPoolExecutor(max_workers=workers) as pool:`；`phase` 参数名改为 `_phase` 或保留——`_run_download_batch` 现签名 `(tasks, track_playlists)`，phase 固定"下载中"，直接内联 `phase = "下载中"`）
- 删除 `_print_dry_run_plan`（:401-441）整段

- [ ] **Step 4: 实现——ProcessUseCase**

`process_use_case.py`：
- import 清理：删 `from musicvault.shared.tui_progress import BatchProgress, console`（:22）
- 新增：
```python
@dataclass(frozen=True, slots=True)
class ProcessResult:
    """process 运行的结构化结果。"""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
```
（import `dataclass`）
- `run_process(self, downloaded, force, playlist_index=None, *, progress: ProgressReporter | None = None) -> ProcessResult`：`_run_process_batch(..., progress)`；`_process_local(force, progress)`；返回 `ProcessResult(processed=..., skipped=..., failed=...)`——计数来源：`_run_process_batch` 内 `skipped`（:90 `pending, skipped`）与失败数（`results` 计数）；为最小改动：`_run_process_batch` 返回 `ProcessResult`，`run_process`/`_process_local` 透传返回；无任务时返回 `ProcessResult()`
- `_run_process_batch`（:81-136）：`progress` 参数；BatchProgress 替换（模式同 Task 6 Step 3）；删除 `self._print_dry_run_plan(pending)`（:96）改直接返回 `ProcessResult(processed=len(pending), skipped=skipped)`（dry-run 不执行）；正常路径 `_mark_processed`/`_link_track`/`_record_processed_results` 后返回 `ProcessResult(processed=len(results), skipped=skipped, failed=失败数)`
- `_print_dry_run_plan`（:154-158）删除

- [ ] **Step 5: 实现——PipelineUseCase**

`pipeline_use_case.py`：
- import 清理：删 `from musicvault.shared.tui_progress import console, ok`（:20）
- 新增：
```python
@dataclass(frozen=True, slots=True)
class PipelineResult:
    """pipeline 运行的结构化结果。"""

    downloaded: int = 0
    processed: int = 0
    pruned: int = 0
    dry_run_plan: dict | None = None
```
（import `dataclass`）
- `run_pipeline(self, cookie: str, command: str, *, progress: ProgressReporter | None = None) -> PipelineResult`：
```python
    def run_pipeline(
        self,
        cookie: str,
        command: str,
        *,
        progress: ProgressReporter | None = None,
    ) -> PipelineResult:
        if not self.dry_run:
            self.cfg.ensure_dirs()

        only_pull = command == "pull"
        only_process = command == "process"

        playlist_index: dict[str, dict[str, object]] = {}
        downloaded: tuple = ()
        pruned = 0
        dry_run_plan: dict | None = None
        if not only_process:
            sync_result = self.sync_service.run_sync(
                cookie=cookie,
                playlist_ids=[pl.id for pl in self.recorder.state.list_playlists()],
                progress=progress,
            )
            downloaded = sync_result.downloaded
            pruned = sync_result.pruned
            dry_run_plan = sync_result.dry_run_plan
            playlist_index = self.sync_service.playlist_index

        processed = 0
        if not only_pull and (not self.dry_run or only_process):
            process_result = self.process_service.run_process(
                downloaded=downloaded,
                force=self.cfg.force,
                playlist_index=playlist_index,
                progress=progress,
            )
            processed = process_result.processed

        if not self.dry_run:
            self._cleanup_uncategorized_orphans()

        return PipelineResult(
            downloaded=len(downloaded),
            processed=processed,
            pruned=pruned,
            dry_run_plan=dry_run_plan,
        )
```
（原 :335-367 中的 console.print 与 ok("完成") 全部删除；`downloaded: list = []` → `tuple`）
- `link_only`（:229-333）：删除全部 `console.print`（:245, :321-323, :328-330）；dry-run 分支直接 `return linked_tracks, playlist_count`；正常分支同。签名与返回不变。

- [ ] **Step 6: 跑测试验证**

Run: `python -m pytest tests/ -q`
Expected: 全绿。`grep -rn "tui_progress" src/musicvault/application/` 应为空。

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "refactor: 旧流水线用例返回结构化结果（SyncResult/ProcessResult/PipelineResult），剥离 Rich 进度展示"
```

---

### Task 7: Rich 剥离——CLI 渲染（阶段 5b）

**Files:**
- Create: `src/musicvault/cli/render.py`（渲染函数）
- Modify: `src/musicvault/cli/main.py`（pipeline 分支接入渲染与进度适配器）
- Modify: `AGENTS.md`（已知偏离更新为已修复）

**Interfaces:**
- Consumes: Task 6 的全部结果类型与 `ProgressReporter`
- Produces: `cli/render.py` 导出 `render_pipeline_result(result: PipelineResult, *, dry_run: bool, command: str) -> None`、`render_link_result(counts: tuple[int, int], *, dry_run: bool) -> None`、`BatchProgressAdapter`（实现 `ProgressReporter`）

- [ ] **Step 1: 新建 cli/render.py**

```python
"""CLI 渲染层：把用例结构化结果渲染为终端输出（Rich 仅存在于本层与 cli 其余部分）。"""

from __future__ import annotations

from musicvault.application.pipeline_use_case import PipelineResult
from musicvault.application.progress import ProgressReporter
from musicvault.application.sync_use_case import SyncResult
from musicvault.shared.tui_progress import BatchProgress, console, ok


class BatchProgressAdapter:
    """把 BatchProgress 适配为 ProgressReporter；每次 begin 重建进度条。"""

    def __init__(self) -> None:
        self._batch: BatchProgress | None = None

    def begin(self, total: int, phase: str) -> None:
        self._batch = BatchProgress(total=total, phase=phase)
        self._batch.__enter__()

    def advance(self, *, success: bool, idx: int, item_name: str) -> None:
        assert self._batch is not None
        self._batch.advance(success, idx, item_name)

    def end(self) -> None:
        assert self._batch is not None
        self._batch.__exit__(None, None, None)
        self._batch = None


def render_sync_summary(result: SyncResult) -> None:
    """「从 N 个歌单同步 M 首」摘要（原 SyncUseCase.run_sync 内部打印）。"""
    stats: list[str] = []
    if result.added:
        stats.append(f"[green]+{result.added} 首[/green]")
    if result.pruned:
        stats.append(f"[red]-{result.pruned} 首[/red]")
    console.print(f"  从 [cyan]{result.playlist_count}[/cyan] 个歌单同步 [cyan]{result.track_count}[/cyan] 首")
    console.print("    " + " | ".join(stats) if stats else "    [dim]无变化[/dim]")


def render_dry_run_plan(plan: dict) -> None:
    """dry-run 计划预览（原 SyncUseCase._print_dry_run_plan）。"""
    with_url: list = plan.get("with_url") or []
    no_url: list = plan.get("no_url") or []
    pruned: list = plan.get("pruned") or []
    moves: list = plan.get("moves") or []
    renames: list = plan.get("renames") or []
    stale_index: int = plan.get("stale_index") or 0

    if with_url:
        console.print(f"  [green]将下载[/green] [cyan]{len(with_url)}[/cyan] 首：")
        for i, t in enumerate(with_url, 1):
            console.print(f"    [dim]{i:>3}.[/dim] {t.artist_text} - {t.name}")
    else:
        console.print("  [dim]将下载 0 首（无新增曲目）[/dim]")

    if no_url:
        console.print(f"  [yellow]无可用直链将跳过[/yellow] [cyan]{len(no_url)}[/cyan] 首：")
        for i, t in enumerate(no_url, 1):
            console.print(f"    [dim]{i:>3}.[/dim] {t.artist_text} - {t.name}")

    if pruned:
        console.print(
            f"  [red]将清理远端已删除曲目[/red] [cyan]{len(pruned)}[/cyan] 首：{', '.join(map(str, pruned))}"
        )

    if renames:
        console.print("  [cyan]歌单目录将重命名：[/cyan]")
        for _pid, old, new in renames:
            console.print(f"    [dim]-[/dim] {old} → {new}")

    if moves:
        console.print(f"  [cyan]歌单归属调整：[/cyan][cyan]{len(moves)}[/cyan] 首曲目的 library 链接将移动")

    if stale_index:
        console.print(f"  [yellow]将清理 {stale_index} 条本地文件缺失的过期索引[/yellow]")


def render_pipeline_result(result: PipelineResult, *, dry_run: bool, command: str) -> None:
    """pipeline 运行结束后的汇总输出。"""
    if dry_run and command != "process":
        if result.dry_run_plan:
            console.print(
                f"  从 [cyan]{result.dry_run_plan.get('playlist_count', '?')}[/cyan] 个歌单同步 "
                f"[cyan]{result.dry_run_plan.get('track_count', '?')}[/cyan] 首（[bold yellow]dry-run 预览[/bold yellow]）"
            )
            render_dry_run_plan(result.dry_run_plan)
        n_new = len((result.dry_run_plan or {}).get("with_url") or [])
        if n_new and command != "pull":
            console.print(f"  [dim]随后将进入后处理：新下载的 {n_new} 首曲目（转码/元数据/歌词/硬链接）[/dim]")
        console.print("  [bold yellow]dry-run 结束：未下载、未修改任何文件[/bold yellow]")
        return
    render_sync_summary(SyncResult(downloaded=(), added=result.downloaded, pruned=result.pruned))
    if command != "pull":
        if result.processed:
            console.print(f"  [green]处理完成 {result.processed} 首[/green]")
    ok("完成")


def render_link_result(counts: tuple[int, int], *, dry_run: bool) -> None:
    """link（--only-link）结果输出。"""
    linked, playlist_count = counts
    if dry_run:
        console.print(
            f"  [bold yellow]dry-run 预览[/bold yellow]：将创建 [cyan]{linked}[/cyan] 个硬链接"
            f"（涉及 [cyan]{playlist_count}[/cyan] 个歌单）"
        )
        return
    if linked:
        console.print(f"  链接完成：[cyan]{linked}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单")
    else:
        console.print("[dim]所有 library 链接均已就绪[/dim]")
```

（说明：`render_pipeline_result` 中 dry-run 的「从 N 个歌单同步 M 首」行取 `dry_run_plan` 内的计数——`SyncResult.dry_run_plan` 在原实现不含 track/playlist 计数，为保持预览行完整，Task 6 Step 3 的 dry_run 分支在组装 plan 时额外写入 `plan["track_count"] = len(unique)`、`plan["playlist_count"] = n_playlists`。）

- [ ] **Step 2: 修改 cli/main.py pipeline 分支**

main.py（:330-341）替换：
```python
    from musicvault.application.bootstrap import build_pipeline
    from musicvault.cli.render import BatchProgressAdapter, render_link_result, render_pipeline_result

    service = build_pipeline(cfg, dry_run=getattr(args, "dry_run", False))
    progress = BatchProgressAdapter()
    try:
        if args.command == "process" and getattr(args, "only_link", False):
            result = service.link_only(cookie=cookie)
            render_link_result(result, dry_run=args.dry_run)
        else:
            result = service.run_pipeline(cookie=cookie, command=pipeline_cmd, progress=progress)
            render_pipeline_result(result, dry_run=args.dry_run, command=pipeline_cmd)
    except KeyboardInterrupt:
        output_info("已取消")
        return 130
    return 0
```
（`args.dry_run` 对 process/link 分支存在——`_add_common_args(process)` 含 dry_run。）

- [ ] **Step 3: 冒烟验证**

```bash
python -m musicvault --help
python -m pytest tests/ -q
```
Expected: help 无 reindex/migrate 子命令；全量绿。

- [ ] **Step 4: 更新 AGENTS.md 已知偏离**

「已知偏离：旧流水线用例（`PipelineUseCase` 链）内部仍直接使用 `shared/tui_progress` 的 Rich 进度展示，输出剥离留待后续；新链路（target-sync）已合规。」→「已修复：旧流水线用例已返回结构化结果，Rich 展示仅存在于 cli/。」

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "refactor: 打印归 CLI（cli/render.py 渲染结构化结果），AGENTS.md 偏离声明更新为已修复"
```

---

### Task 8: 文档收尾与全量验证（阶段 6）

**Files:**
- Modify: `README.md`（migrate 条目 :54, :88 删除；reindex 引用删除；命令清单更新）
- Modify: `AGENTS.md`（两条流水线描述：删除 migrate/reindex；workspace 布局注释；link 方向「DB→library」文档化；「常用命令」注释若含旧命令则更新）
- Modify: `docs/superpowers/specs/2026-08-12-old-pipeline-migration-handoff.md`（状态更新为已实施）
- Modify: `docs/superpowers/specs/2026-08-12-old-pipeline-migration-design.md`（状态更新为已实施）

**Interfaces:**
- Consumes: Task 1-7 全部完成

- [ ] **Step 1: 更新 README.md**

删除 `msv migrate` 表格行（:54）与示例（:88）；删除 reindex 相关行；命令清单补注：canonical 落 `media_store/<track_id>/audio/`，缓存落 `cache/`。

- [ ] **Step 2: 更新 AGENTS.md**

- 两条流水线小节：旧命令流改为（sync/pull/process）；删除「migrate → presets → target-sync」中的 migrate；workspace 布局描述改为「`cache/`（临时文件，含下载缓存与解密中间）、`media_store/<track_id>/audio/`（长期媒体资产，canonical 文件）、`library/`（可重建的目标视图，由 link/target-sync 从 DB 重建）、`logs/`、`state.db`」。
- 若「Issue tracker」或命令示例含 migrate/reindex 一并更新。

- [ ] **Step 3: 更新 handoff 与 design 文档状态**

两文件头部「状态」改为「已实施（2026-08-12）」；handoff 的 DoD 逐条勾选核对。

- [ ] **Step 4: 全量验证**

```bash
python -m pytest tests/ -q
python -m ruff check src/ tests/
python -m ruff format --check src/ tests/
python -m musicvault --help
```
Expected: 全绿 + ruff 格式通过 + help 正常。

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "docs: C 阶段迁移收尾（README/AGENTS.md 更新，handoff 与设计文档标记已实施）"
```

---

## Self-Review 记录

**Spec 覆盖核对：**
- 阶段 0 删 reindex → Task 1 ✓
- 阶段 1 缓存路径 → Task 2 ✓
- 阶段 2 canonical 落位 → Task 3 ✓
- 阶段 3 查找/删除/扫描 → Task 4 ✓
- 阶段 4 删迁移机制 → Task 5 ✓
- 阶段 5 Rich 剥离 → Task 6（用例侧）+ Task 7（CLI 侧）✓
- 阶段 6 测试/文档收尾 → Task 2-5 的 fixture 步骤 + Task 8 ✓
- DoD「全量 pytest ≥204 项绿 + ruff」→ 每任务回归 + Task 8 全量 ✓

**占位符扫描：** 无 TBD/TODO；fixture 替换给到精确行号与替换规则。

**类型一致性：** `SyncResult.downloaded` 为 tuple（Task 6 定义，Task 7 `render_sync_summary` 只用计数）；`run_pipeline` 的 `progress` 关键字参数（Task 6）与 main.py 调用（Task 7）一致；`link_only` 返回 `tuple[int, int]` 全链一致；`PipelineResult.dry_run_plan` 含 `track_count`/`playlist_count` 键（Task 6 Step 3 与 Task 7 渲染约定一致）。

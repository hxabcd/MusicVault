# preset 与 sync_target 脚本 API 分离实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将混合在 `preset_api` 中的两套公开脚本 API 分离为平行的 `preset_api`（preset 专用）与 `target_api`（sync_target 专用）两个包，注册表拆分、脚本入口保持单参数组合对象、配置统一为 `script_directories`。

**Architecture:** `target_api` 是新的公开包（`v1.py` + `_executor.py` + `_media.py` + `builtins.py`），`PresetRegistry`/`TargetRegistry` 拆为两个类；外部脚本目录统一（`script_directories`），由新加载器 `application/script_loader.py` 以单参数组合对象 `ScriptRegistries`（`.presets`/`.targets`）分发注册；`PresetContext` 改名 `TargetContext`；破坏性迁移，无垫片。

**Tech Stack:** Python 3.12+，pytest，ruff（line-length=120），uv。

**Spec:** [2026-08-13-preset-target-api-separation-design.md](../specs/2026-08-13-preset-target-api-separation-design.md)

## Global Constraints

- 一律使用中文回答用户、书写注释、docstring 与 commit message（AGENTS.md）。
- 领域术语遵循 CONTEXT.md（「中文（English）」形式），如 目标同步器（Target Synchronizer）、预设（Preset）。
- 依赖方向固定：application → domain/ports/公开 API 包；`target_api` **不得** import `preset_api`；两个公开包只依赖 domain/ports/shared。
- 破坏性迁移：不保留任何 re-export 垫片；`preset_api.v1` 中 target 符号一律移除。
- `PresetContext` 全局改名 `TargetContext`（含注释、测试、文档）。
- 测试命令：`uv python -m pytest tests/ -q`；单文件：`uv python -m pytest tests/test_xxx.py -v`。
- lint/格式：`uv python -m ruff check src/ tests/`、`uv python -m ruff format --check src/ tests/`。
- 当前测试基线 263 项通过；每个任务结束时全量测试须通过。

## 文件结构

**新建（src）**
- `src/musicvault/target_api/__init__.py` — 同构「仅暴露 v1」约定
- `src/musicvault/target_api/v1.py` — `TargetRegistration`、`TargetSynchronizer`、`TargetContext`、`Operation`、`TargetRegistry`、`PresetLoadError`、`API_VERSION`
- `src/musicvault/target_api/_executor.py` — 自 `preset_api/_executor.py` 原样迁移
- `src/musicvault/target_api/_media.py` — 自 `preset_api/_media.py` 原样迁移
- `src/musicvault/target_api/builtins.py` — `HardlinkDistributor` + `register_builtin_targets`
- `src/musicvault/application/script_loader.py` — `ScriptRegistries` + `load_script_directories`

**新建（tests）**
- `tests/test_target_api.py` — TargetRegistry/TargetRegistration/TargetContext 单测（自 test_preset_api_more.py 迁移 + 新增）
- `tests/test_script_loader.py` — 加载器单测（自 test_preset_api.py 加载类用例迁移 + 双注册表分发）

**修改（src）**
- `src/musicvault/preset_api/v1.py` — 移除全部 target 符号与加载逻辑；移除 `PresetRegistration.target` 字段
- `src/musicvault/preset_api/builtins.py` — 仅 `ArchivePreset` + `register_builtin_presets(registry)`（去 target_root 参数）
- `src/musicvault/application/bootstrap.py` — Runtime 增 `targets`；双注册表 + 双内置注册 + loader
- `src/musicvault/application/pipeline_use_case.py` — 注入 `TargetRegistry`，`_run_distribute` 用 `target_registrations()`
- `src/musicvault/application/sync_engine.py` — import 改 `target_api.v1`
- `src/musicvault/cli/main.py` — `runtime.targets.target_registrations()`；DistributePipeline 校验与执行用 targets
- `src/musicvault/cli/render.py` — `PresetRegistration`/`TargetRegistration` 分源导入
- `src/musicvault/core/config.py` — `script_directories` 字段 + `script_system` 键 + 旧键兼容读取

**修改（tests）**：`test_preset_api.py`、`test_preset_api_more.py`、`test_preset_registry.py`、`test_preset_builtins_more.py`、`test_builtin_hardlink.py`、`test_media_resolver.py`、`test_sync_engine.py`、`test_sync_engine_more_edges.py`、`test_cli_semantics.py`、`test_pipeline_use_case.py`、`test_bootstrap*.py`（三个）、`test_config_model.py`、`test_config_more.py`、`test_architecture.py`、`test_dry_run.py`（视失败）

**修改（docs）**：`AGENTS.md`、`README.md`

---

### Task 1: 创建 target_api 公开包（纯新增）

**Files:**
- Create: `src/musicvault/target_api/__init__.py`
- Create: `src/musicvault/target_api/v1.py`
- Create: `src/musicvault/target_api/_executor.py`
- Create: `src/musicvault/target_api/_media.py`
- Create: `src/musicvault/target_api/builtins.py`
- Test: `tests/test_target_api.py`

**Interfaces:**
- Consumes: `musicvault.domain.models.TargetDescriptor`、`musicvault.domain.operations.Operation`（重导出）、`musicvault.ports.target.TargetOperations`、`musicvault.ports.media.MediaRequest`、`musicvault.shared.utils.format_track_name/safe_filename`
- Produces:
  - `target_api.v1.TargetRegistry.register_target(registration) / target_registrations(enabled_only=False) / create_target(name, presets)`
  - `target_api.v1.TargetContext(snapshot, target, dry_run=False, target_descriptor=None, media_store_root=None)`
  - `target_api.builtins.register_builtin_targets(registry, target_root, default_playlist_name="未分类")`

- [ ] **Step 1: 写失败测试** `tests/test_target_api.py`

```python
from musicvault.target_api.v1 import TargetRegistry, TargetRegistration, TargetContext, PresetLoadError

def test_target_registry_dependency_injection():
    registry = TargetRegistry()
    captured: dict = {}
    def factory(presets):
        captured["presets"] = presets
        return object()
    registry.register_target(TargetRegistration(name="t", factory=factory, depends_on=("a",)))
    with pytest.raises(PresetLoadError, match="a"):
        registry.create_target("t", presets={})  # 依赖缺失 → 报错
```

覆盖：`register_target`/`target_registrations`/`create_target` 依赖注入与缺失报错、同名拒绝、API 版本校验、`TargetContext` 构造与 `lyrics_file` 边界、`register_builtin_targets` 注册 hardlink。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv python -m pytest tests/test_target_api.py -v`
Expected: FAIL — `ModuleNotFoundError: musicvault.target_api`

- [ ] **Step 3: 实现 target_api 包**

- `_executor.py`、`_media.py`：从 `preset_api/` 原样复制（内容一字不改，仅文件位置变化）。
- `v1.py`：复制现 `preset_api/v1.py` 中 target 侧代码并改名：
  - `PresetContext` → `TargetContext`（类名、docstring、`__post_init__` 注释内全部替换）
  - `TargetRegistry`：仅 `register_target` / `target_registrations` / `create_target`（依赖注入与缺失校验保留，错误消息保留）；移除 `register` 兼容入口、`get`、`load_directories`、`_load_script`、`preset_registrations`、`create_preset`
  - `__all__ = ["API_VERSION", "Operation", "PresetLoadError", "TargetContext", "TargetRegistration", "TargetRegistry", "TargetSynchronizer"]`
- `builtins.py`：`HardlinkDistributor` 原样迁移 + `register_builtin_targets(registry, target_root, default_playlist_name="未分类")` 注册 `hardlink`（`depends_on=("archive",)`，factory 注入 `presets["archive"]`），import 改 `from musicvault.target_api.v1 import ...`
- `__init__.py`：同构 preset_api——`from musicvault.target_api import v1`，`__all__ = ["v1"]`。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv python -m pytest tests/test_target_api.py -v`
Expected: PASS；`uv python -m pytest tests/ -q` 全量仍 263 项通过（preset_api 未动）

- [ ] **Step 5: 提交**

`git add -A && git commit -m "feat: 新增 target_api 公开包（target 脚本专用 API 分离第一步）"`

---

### Task 2: 新增 script_loader（单参数组合对象分发）

**Files:**
- Create: `src/musicvault/application/script_loader.py`
- Create: `tests/test_script_loader.py`

**Interfaces:**
- Consumes: `preset_api.v1.PresetRegistry`、`target_api.v1.TargetRegistry`、`preset_api.v1.PresetLoadError`（加载错误统一抛此类型，保持历史 catch 兼容）
- Produces: `ScriptRegistries(presets: PresetRegistry, targets: TargetRegistry)`、`load_script_directories(directories, presets, targets) -> None`

- [ ] **Step 1: 写失败测试** `tests/test_script_loader.py`

脚本样例（新 register 签名）：

```python
script.write_text(
    "from musicvault.target_api.v1 import API_VERSION\n"
    "def register(registry):\n"
    "    registry.targets.register_target(\n"
    "        TargetRegistration(name='t', factory=lambda p: object(), api_version=API_VERSION))\n",
    encoding="utf-8",
)
```

覆盖（自 `tests/test_preset_api.py` 加载类用例迁移改写）：
- preset 脚本经 `registry.presets.register_preset` 注册、target 脚本经 `registry.targets.register_target` 注册，同一目录两类脚本共存
- source 元数据保留（`_loading_source` 语义）
- 同名拒绝（含跨目录）、缺少 `register` 报路径、脚本抛错包装、ImportError 依赖缺失包装
- 目录排序确定性、`_` 前缀脚本跳过

- [ ] **Step 2: 运行测试确认失败**

Run: `uv python -m pytest tests/test_script_loader.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 `application/script_loader.py`**

```python
@dataclass(frozen=True, slots=True)
class ScriptRegistries:
    presets: PresetRegistry
    targets: TargetRegistry

def load_script_directories(
    directories: Iterable[str | Path],
    presets: PresetRegistry,
    targets: TargetRegistry,
) -> None:
    """遍历外部脚本目录，以 ScriptRegistries 分发两类注册。"""
```

迁移 `PresetRegistry._load_script` 全部逻辑（`_NAME_RE` 校验留在各注册表内；`PresetLoadError` 从 `preset_api.v1` 导入）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv python -m pytest tests/test_script_loader.py -v`；全量回归
Expected: PASS

- [ ] **Step 5: 提交**

`git commit -am "feat: 新增 script_loader 统一加载 preset 与 sync_target 外部脚本"`

---

### Task 3: preset_api 瘦身 + 内部消费方双注册表化

**Files:**
- Modify: `src/musicvault/preset_api/v1.py`、`src/musicvault/preset_api/builtins.py`
- Modify: `src/musicvault/application/bootstrap.py`、`application/pipeline_use_case.py`、`application/sync_engine.py`、`cli/main.py`、`cli/render.py`
- Modify: `tests/test_preset_api.py`、`test_preset_api_more.py`、`test_preset_registry.py`、`test_preset_builtins_more.py`、`test_builtin_hardlink.py`、`test_media_resolver.py`、`test_sync_engine.py`、`test_sync_engine_more_edges.py`、`test_cli_semantics.py`、`test_pipeline_use_case.py`、`test_bootstrap.py`、`test_bootstrap_more.py`、`test_bootstrap_pipeline.py`

**Interfaces:**
- Consumes: Task 1 的 `target_api` 全符号、Task 2 的 `load_script_directories`
- Produces: `preset_api.v1.__all__ = ["API_VERSION", "AudioFormat", "BasePreset", "LyricEncoding", "MetadataSpec", "PresetLoadError", "PresetRegistration", "PresetRegistry", "Quality", "audio_spec_key"]`；`Runtime` 增字段 `targets: TargetRegistry`

- [ ] **Step 1: preset_api 瘦身**

`v1.py` 移除：`TargetRegistration`、`TargetSynchronizer`、`PresetContext`、`Operation` 重导出、`TargetDescriptor` import、`_executor`/`_media` import、`PresetRegistration.target` 字段及其 `__post_init__` 逻辑、`register_target`/`create_target`/`target_registrations`/`register`/`get`/`load_directories`/`_load_script`、`importlib`/`sys`/`Callable`/`Iterable`/`replace`/`Path`/`Protocol`/`Any` 中不再使用的导入。

`builtins.py`：移除 `HardlinkDistributor` 与 `shutil`、`Mapping`、`format_track_name`、`safe_filename`、`TargetRegistration`、`PresetRegistry`（若不再用）等导入；`register_builtin_presets(registry)` 仅注册 archive（签名去掉 `target_root`、`default_playlist_name`）。

- [ ] **Step 2: 更新内部消费方**

- `sync_engine.py`：`from musicvault.target_api.v1 import TargetContext, TargetRegistration`
- `pipeline_use_case.py`：构造参数 `registry: PresetRegistry | None` 改为 `registry` + `targets: TargetRegistry | None`；`_run_distribute` 用 `self.targets.target_registrations(enabled_only=True)`，`self.registry is None or self.target is None` 判断加入 `self.targets is None`
- `bootstrap.py`：`Runtime` 增加 `targets: TargetRegistry`；`build_runtime` 与 `build_pipeline` 各建 `PresetRegistry()` + `TargetRegistry()`，内置注册拆两次调用（`register_builtin_presets(presets)`、`register_builtin_targets(targets, config.library_dir, config.default_playlist_name)`），外部脚本统一 `load_script_directories(directories, presets, targets)`
- `cli/main.py`：`render_targets(runtime.targets.target_registrations())`；`DistributePipeline.run` 中 selected 缺失校验与执行列表改用 `self.runtime.targets.target_registrations(enabled_only=True)`（错误消息改「未找到指定 sync_target」）
- `cli/render.py`：`PresetRegistration` 从 `preset_api.v1`、`TargetRegistration` 从 `target_api.v1` 分源导入

- [ ] **Step 3: 更新测试**

- `test_preset_api.py`：删除已迁移到 `test_script_loader.py` 的加载类用例；`test_registry_rejects_incompatible_api_version` 改为 `register_preset` 路径；移除 `TargetRegistration` 导入
- `test_preset_api_more.py`：PresetContext 用例 → `test_target_api.py`（改名 TargetContext）；`TargetRegistration.create` 用例 → `test_target_api.py`；`PresetRegistration.create` 分支与 `_NAME_RE` 校验保留；`PresetRegistry` 用例收窄
- `test_preset_registry.py`：只测 preset 侧（register_preset/preset_registrations/create_preset/同名/API 版本）；target 侧用例已在 `test_target_api.py`，删除 `test_duplicate_names_rejected_across_kinds` 与 `test_legacy_register_maps_to_target`
- `test_preset_builtins_more.py`、`test_builtin_hardlink.py`：`HardlinkDistributor`/`register_builtin_targets` → `target_api.builtins`；`TargetContext` → `target_api.v1`；`ArchivePreset`/`register_builtin_presets` 留在 `preset_api.builtins`（调用处更新为新签名）
- `test_media_resolver.py`：`from musicvault.target_api._media import SnapshotMediaResolver`
- `test_sync_engine.py`、`test_sync_engine_more_edges.py`、`test_cli_semantics.py`：`TargetRegistration` → `target_api.v1`
- `test_pipeline_use_case.py`：`PresetContext` → `TargetContext`（target_api.v1）、`TargetRegistration` → `target_api.v1`
- `test_bootstrap*.py`：`Runtime` 断言增加 `targets`；`register_builtin_presets` 调用新签名；`preset_directories` 字段名在 Task 4 处理

- [ ] **Step 4: 全量回归 + lint**

Run: `uv python -m pytest tests/ -q`；`uv python -m ruff check src/ tests/`；`uv python -m ruff format --check src/ tests/`
Expected: 全绿；对失败项逐个按上述清单修正（本任务允许通过测试失败驱动剩余导入修正）

- [ ] **Step 5: 提交**

`git commit -am "refactor: preset_api 瘦身并拆分为双注册表（破坏性迁移）"`

---

### Task 4: 配置改名 script_directories / script_system

**Files:**
- Modify: `src/musicvault/core/config.py`
- Modify: `tests/test_config_model.py`、`tests/test_config_more.py`、`src/musicvault/application/bootstrap.py`

**Interfaces:**
- Consumes: —
- Produces: `Config.script_directories: tuple[str, ...]`；`to_dict()["script_system"] = {"directories": [...], "builtin": ...}`；`from_dict` 兼容读取 `script_directories` / `preset_directories` / `preset_system.directories` / `preset.directories`

- [ ] **Step 1: 改实现**

`config.py`：字段改名；`from_dict` 读取优先级 `raw["script_directories"]` → `raw["preset_directories"]` → `script_system.get("directories")` → `preset_system.get("directories")`；`to_dict` 输出 `script_system`；`builtin_scripts_enabled` 读取兼容链保留（`script_system.builtin` → `preset_system.builtin` → `playlist_links`）。

- [ ] **Step 2: 更新测试**

`test_config_model.py` / `test_config_more.py`：`preset_directories` 断言改为 `script_directories`；to_dict 键断言改 `script_system`；新增旧键（`preset_directories`、`preset_system.directories`、`preset_system.builtin`）兼容读取用例。

- [ ] **Step 3: 更新 bootstrap 引用**

`bootstrap.py`：`config.preset_directories` → `config.script_directories`（两处）。

- [ ] **Step 4: 全量回归**

Run: `uv python -m pytest tests/ -q`；ruff check/format
Expected: 全绿

- [ ] **Step 5: 提交**

`git commit -am "refactor: 外部脚本目录配置改名 script_directories（兼容旧键读取）"`

---

### Task 5: 架构约束测试 + 文档更新

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `AGENTS.md`、`README.md`

- [ ] **Step 1: 扩展架构测试**

`tests/test_architecture.py`：把 `test_preset_api_top_level_exposes_only_v1` 泛化为对两个包的参数化检查（`preset_api` 顶层仅 v1；`target_api` 顶层仅 v1，断言 `not hasattr(target_api, "TargetRegistry")` 等）；新增 `target_api` 不 import `preset_api` 的依赖方向断言。

- [ ] **Step 2: 更新 AGENTS.md**

- `preset_api/` 段改为只描述 preset 脚本 API（BasePreset/PresetRegistration/Quality/AudioFormat/LyricEncoding/MetadataSpec/render/builtins）
- 新增 `target_api/` 段：TargetRegistration/TargetSynchronizer/TargetContext/TargetRegistry/builtins（hardlink），「外部脚本唯一可依赖的版本化公开 API（当前 v1）」表述覆盖两个包
- 依赖方向补充：`target_api` 不依赖 `preset_api`
- 命令与流水线段：`build_runtime` 描述更新为双注册表 + `load_script_directories`；配置名 `preset_system.builtin` → `script_system.builtin`

- [ ] **Step 3: 更新 README.md**

- 第 90 行：`preset_system.directories` → `script_system.directories`；`musicvault.preset_api.v1` → 分别引用 `musicvault.preset_api.v1`（preset 脚本）与 `musicvault.target_api.v1`（sync_target 脚本）
- 第 165、230-231 行：配置表 `preset_system` → `script_system`
- 脚本编写指南（若有导入示例）更新 register 新入口说明

- [ ] **Step 4: 全量回归 + 冒烟**

Run: `uv python -m pytest tests/ -q`；`uv python -m ruff check src/ tests/`；`uv python -m ruff format --check src/ tests/`；`uv python -m musicvault --help`
Expected: 全绿；263 项以上（新增用例）

- [ ] **Step 5: 提交**

`git commit -am "docs: 更新 AGENTS.md 与 README 的脚本体系与配置说明"`

---

### Task 6: 最终回归与收尾

**Files:**
- 视失败调整

- [ ] **Step 1: 全量测试**

Run: `uv python -m pytest tests/ -q`
Expected: 全绿（含新增 target_api/script_loader 用例）

- [ ] **Step 2: 检查残留引用**

Run: `uv python -m ruff check src/ tests/`；grep 确认 `preset_api` 下无 `PresetContext|TargetRegistration|register_target` 残留、`src` 下无 `PresetContext` 残留

- [ ] **Step 3: 提交收尾**

`git commit -am "chore: 分离收尾回归"`（无变更则跳过）

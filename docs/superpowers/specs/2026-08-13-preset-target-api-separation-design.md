# MusicVault 重构：preset 脚本与 sync_target 脚本 API 分离

日期：2026-08-13

## 背景与动机

`preset_api/` 目前混合承载两套公开脚本 API：

1. **preset 脚本 API**（声明处理规格）：`BasePreset`、`PresetRegistration`、`Quality`、`AudioFormat`、`LyricEncoding`、`MetadataSpec`、`audio_spec_key`、`render.py` 歌词渲染工具库。
2. **sync_target 脚本 API**（定义分发）：`TargetRegistration`、`TargetSynchronizer`、`PresetContext`、`Operation`、`PresetRegistry`（双注册 + `load_directories`）、`_executor.py`、`_media.py`。

`PresetRegistry` 一个类同时维护两个索引（`register_preset` / `register_target`），`builtins.py` 同时包含 `ArchivePreset`（preset）与 `HardlinkDistributor`（target）。外部脚本体系只有一个版本化命名空间，两类脚本的公开 API 面无法独立演进。

工作区已存在空目录 `src/musicvault/target_api/`（无源文件，仅 pyc 残留），此前有一次未完成的分离尝试。

## 目标

- 将两类脚本的公开 API 分离为两个平行包：`preset_api`（preset 专用）+ `target_api`（sync_target 专用）。
- 注册表拆分为 `PresetRegistry` + `TargetRegistry` 两个类，各自职责单一。
- 外部脚本目录**统一**为 `script_directories`（不按脚本类型分目录），一个目录同时放置两类脚本。
- 破坏性迁移：不保留 re-export 垫片；`PresetContext` 改名 `TargetContext`（语义一致化）。

## 目标架构

### 包结构

```
preset_api/                     # preset 脚本专用公开 API
  __init__.py                   # 保持「仅暴露 v1」约定
  v1.py                         # BasePreset、PresetRegistration、Quality、AudioFormat、
                                # LyricEncoding、MetadataSpec、audio_spec_key、API_VERSION、
                                # PresetRegistry、PresetLoadError
  render.py                     # 不变（standard_lrc / enhanced_lrc / plain_text）
  builtins.py                   # ArchivePreset + register_builtin_presets（移除 HardlinkDistributor）

target_api/                     # sync_target 脚本专用公开 API（新公开包）
  __init__.py                   # 同构：「仅暴露 v1」约定
  v1.py                         # TargetRegistration、TargetSynchronizer、TargetContext、
                                # Operation、TargetRegistry、PresetLoadError、API_VERSION
  _executor.py                  # OperationExecutor（自 preset_api 迁入）
  _media.py                     # SnapshotMediaResolver（自 preset_api 迁入）
  builtins.py                   # HardlinkDistributor + register_builtin_targets
```

### 注册表拆分

| 能力 | `PresetRegistry`（preset_api.v1） | `TargetRegistry`（target_api.v1） |
|---|---|---|
| 注册 | `register_preset` | `register_target` |
| 枚举 | `preset_registrations` | `target_registrations` |
| 创建 | `create_preset` | `create_target`（含 depends_on 依赖注入校验） |
| 移除 | `get()` 双索引查询、`register()` 兼容入口、`load_directories()` | 同左，全部移除 |

依赖注入逻辑（`create_target` 校验 `depends_on` 并注入 preset 实例）迁移到 `TargetRegistry`，错误消息保留。

### 脚本入口：单参数组合对象

外部脚本保持 `register(registry)` 单参数入口，参数为轻量组合对象 `ScriptRegistries`（定义于 `application/script_loader.py`）：

```python
@dataclass(frozen=True, slots=True)
class ScriptRegistries:
    presets: PresetRegistry
    targets: TargetRegistry
```

preset 脚本：

```python
def register(registry):
    registry.presets.register_preset(PresetRegistration(name="my_preset", factory=MyPreset))
```

sync_target 脚本：

```python
def register(registry):
    registry.targets.register_target(TargetRegistration(name="my_target", factory=MyFactory, depends_on=("archive",)))
```

加载器：新文件 `application/script_loader.py`，提供 `load_script_directories(directories, presets, targets)`——遍历一次目录，对每个 `*.py` 执行 `register(ScriptRegistries(presets, targets))`；`_loading_source` 上下文、错误包装、缺少 `register` 检测等逻辑自 `PresetRegistry` 迁移至此。

### 内置脚本拆分

- `preset_api/builtins.py`：`ArchivePreset` + `register_builtin_presets(registry)`（只注册 archive；不再需要 `target_root` 参数）。
- `target_api/builtins.py`：`HardlinkDistributor` + `register_builtin_targets(registry, target_root, default_playlist_name="未分类")`（注册 hardlink）。

### 配置改名

- `Config.preset_directories` → `Config.script_directories`。
- `to_dict()` 输出键 `preset_system` → `script_system`（`directories` / `builtin` 子键不变）。
- `from_dict()` 兼容读取旧键：`raw["preset_directories"]`、`preset_system.directories`（含旧 `preset` 别名）、`preset_system.builtin` / `playlist_links` 迁移逻辑保留。
- `builtin_scripts_enabled` 字段名保留。

### 改名与各包独立定义

- `PresetContext` → `TargetContext`（sync_target 专用上下文，改放 `target_api.v1`）。
- `API_VERSION`、`PresetLoadError` 在两个包各自定义（平行公开包，互不依赖）。
- `Operation` 继续从 `domain.operations` 导入后由 `target_api.v1` 重导出（target 脚本专用）。

### 依赖方向

- `target_api` 只依赖 `domain` / `ports` / `shared.utils`（`builtins.py` 的 `format_track_name`、`safe_filename`、`audio_spec_key`），**不依赖 `preset_api`**。
- `preset_api` 依赖方向不变（domain / ports / shared）。
- `application`（bootstrap、script_loader、sync_engine、pipeline_use_case）可依赖两个公开包。
- `adapters` 消费枚举现状不变（`metadata_writer` → MetadataSpec、`organizer` → AudioFormat、`netease_client` → Quality，均属 preset 侧）。

## 内部消费方改动

| 文件 | 改动 |
|---|---|
| `application/bootstrap.py` | `Runtime` 增加 `targets: TargetRegistry`；`build_runtime` 创建两个注册表并分别加载内置与外部脚本；`build_pipeline` 仅需 PresetRegistry（distribute 相关注册改由 distribute 链路承担） |
| `application/script_loader.py` | 新文件（见上） |
| `application/sync_engine.py` | `TargetRegistration`、`TargetContext` 改从 `target_api.v1` 导入 |
| `application/pipeline_use_case.py` | preset 侧符号不变（若引用 `PresetRegistry` 仅注册表语义收窄） |
| `application/process_use_case.py` | preset 侧符号不变 |
| `cli/render.py` | `PresetRegistration` 从 `preset_api.v1`、`TargetRegistration` 从 `target_api.v1` 分源导入 |
| `adapters/processors/*`、`providers/netease_client.py` | 零改动 |

## 测试改动

- 约 15 个测试文件的导入路径按符号所属分源更新（preset 侧留 `preset_api.v1`，target 侧改 `target_api.v1`）。
- `test_architecture.py`：新增 `target_api` 顶层「仅暴露 v1」约束（镜像现有 `preset_api` 测试）。
- 新增/调整：`script_loader` 双注册表加载、`TargetRegistry` 依赖注入、`TargetContext` 改名后的用例。
- 现有「外部脚本经 `preset_api.v1` 导入 target 符号」的断言用例改为新导入路径。

## 文档改动

- `AGENTS.md`：`preset_api/` 描述拆分，新增 `target_api/` 段落，更新配置项名称。
- `README.md`：preset 与 sync_target 脚本编写指南的导入示例更新。
- 术语不变（`CONTEXT.md` 无需改动）。

## 非目标

- 不改变两套脚本的注册语义与生命周期（prepare → sync_item → finalize）。
- 不改变歌词渲染工具库、内置 hardlink 分发行为、配置其余部分。
- 不为旧导入路径保留任何垫片（破坏性迁移）。

# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 语言要求

- 一律使用中文回答用户、书写注释、docstring 与 commit message。
- 领域术语遵循 `CONTEXT.md` 术语表（「中文（English）」形式，如 曲目（Track）、目标端（Target）、源快照（Source Snapshot）），不要自创同义词。

## 常用命令

```bash
uv pip install -e .                          # 可编辑安装（Python 3.12+，依赖见 pyproject.toml）
uv python -m pytest tests/ -q                   # 全量测试（当前 263 项通过）
uv python -m pytest tests/test_source_port.py -v                # 单文件测试
uv python -m pytest tests/test_source_port.py::test_name -v     # 单个用例
uv python -m ruff check src/ tests/             # lint（line-length=120）
uv python -m ruff format --check src/ tests/    # 格式检查
uv python -m musicvault --help                  # CLI 冒烟
```

注意：

- 依赖中的 `pymusiclibrary` 是本地构建 wheel（`D:\MyPC\Dev\Projects\ncm-python-sdk`，分支 `fix/playlist-large-response-crash`），不是 PyPI 版本。
- 转码依赖系统 ffmpeg，但测试通过 fake/mock 端口隔离，不需要真实网易云账号或 ffmpeg。

## 架构：模块化单体 + 端口适配器

依赖方向固定：

```text
presentation → application → domain
adapters ───────────────────┘
```

- `domain/` — 纯领域模型与规则：`models.py`（Track/Playlist/MediaAsset/SourceSnapshot/TargetDescriptor）、`lyrics.py`（LyricLine/LyricWord 统一歌词模型）、`operations.py`。只允许标准库，不得依赖 CLI、Rich、SQLite、网易云 SDK 或 ffmpeg。
- `application/` — 应用用例与编排：`SyncUseCase`（fetch/pull 两阶段）、`ProcessUseCase`（后处理）、`PipelineUseCase`（sync 四阶段编排）、`SyncEngine`（distribute 引擎）、`SourceStateRecorder`（源侧状态登记）；`bootstrap.py` 是 composition root。
- `ports/` — 抽象接口（Protocol）：`source.SourceClient`（网易云源端）、`state.StateRepository`（SQLite 状态）、`media.MediaResolver`、`target.TargetOperations`。端口只描述业务需要的能力，不暴露第三方类型。
- `adapters/` — 具体实现：`providers/netease_client.py`（SourceClient 的实现）、`state/sqlite.py`、`filesystem/`（workspace、media_store）、`processors/`（decryptor/downloader/lyrics/metadata_writer/organizer）、`targets/`。
- `preset_api/` — 外部脚本唯一可依赖的版本化公开 API（当前 `v1`），两套脚本体系：preset 脚本（声明音频规格、歌词函数、元数据粒度）+ sync_target 脚本（引用 preset 并定义分发）；内部重构不得破坏其签名，脚本不得 import 内部模块。依赖方向放宽：`adapters/` 允许消费 `preset_api` 的枚举（Quality/AudioFormat 等，见 tests/test_architecture.py）。
- `cli/`（presentation）— 参数解析、交互登录、Rich 输出与退出码；不得自行组装具体依赖。
- `core/` — 仅剩 `config.py`（声明式 presets 已退役）；`shared/` — Rich 输出、进度展示、工具函数。

关键规则：

- 所有具体依赖在 composition root（`application/bootstrap.py` 的 `build_runtime` / `build_source_client` / `build_pipeline` / `build_distribute_pipeline`）组装；业务用例不自行创建数据库连接、SDK 客户端或 Rich 控制台。
- 测试接缝是 application 用例：注入 fake 端口（如鸭子类型 fake SourceClient）即可测整条流水线，这是测试大部分用例的方式。
- 已修复：旧流水线用例已返回结构化结果，Rich 展示仅存在于 cli/。
- 已知偏离补充：`adapters/processors/organizer.py` 使用 `shared.output.warn` 输出告警，不在原「旧流水线用例使用 Rich 进度」声明范围内。

## 命令与流水线

命令已收敛：`init` / `sync` / `distribute` / `presets` / `add` / `remove`（别名 `rm`）/ `list`（别名 `ls`）/ `help`；`pull`/`process` 并入 sync 内部阶段，`target-sync` 改名 `distribute`。

1. **sync 四阶段链路**：`cli` → `build_pipeline(config)` → `PipelineUseCase` 编排 `SyncUseCase`（fetch 拉取元数据 → pull 下载与歌词统一格式入库）→ `ProcessUseCase`（离线歌词、按 preset 声明转码/写元数据/写歌词文件）→ distribute 阶段（`SyncEngine` 驱动 sync_target 重建目标端）；`--no-distribute` / `--only-distribute` 控制分发，状态经 `SourceStateRecorder` 写入 SQLite。
2. **distribute 独立命令**：`build_runtime(config)` 组装 → `PresetRegistry` 加载内置（archive preset + hardlink target，受 `preset_system.builtin` 开关控制）与外部脚本目录 → `build_distribute_pipeline` 的 `DistributePipeline.run` 按 `prepare → sync_item → finalize` 生命周期执行目标操作；`--dry-run` 不产生副作用；一次运行内所有 sync_target 共享同一 SQLite `SourceSnapshot`。

workspace 布局：`cache/`（临时文件，含下载缓存与解密中间产物）、`media_store/<track_id>/`（长期媒体资产，canonical 文件与 `<tid>.<preset>.lrc` 扁平共存）、`library/`（可重建的目标视图，由 hardlink distribute 从 DB 重建）、`logs/`、`state.db`（SQLite，schema 版本化，写入走事务）。

## 文档约定

- `CONTEXT.md` — 领域术语表，改动前先读。

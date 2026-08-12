# 设计：代码覆盖率优化至 90%+（并行加速）

日期：2026-08-13
状态：已批准

## 背景与目标

当前总体行覆盖率 74%（3655 语句，942 未覆盖），273 项测试通过。目标：

1. 排除人工测试的交互代码后，**总体行覆盖率 ≥ 90%**。
2. 在 `pyproject.toml` 设置 `fail_under = 90` 防回归。
3. 交互代码（`input()` 交互、二维码登录轮询等）标注 `# pragma: no cover`，**保留人工手动测试**，不强制自动化覆盖。

已确认的约束：

- 允许小幅修改生产代码（依赖注入、提取纯函数，行为不变）以提高可测性。
- 覆盖率统计排除交互代码（`# pragma: no cover`）。
- 采用方案 B：并行 subagent 加速，批次文件集互不重叠。

## 当前缺口分布

| 模块 | 覆盖率 | 未覆盖语句 |
|---|---|---|
| `cli/playlist.py` | 9% | 250 |
| `cli/main.py` | 61% | 117 |
| `shared/tui_progress.py` | 30% | 81 |
| `processors/metadata_writer.py` | 66% | 83 |
| `cli/render.py` | 22% | 56 |
| `sync_use_case.py` | 82% | 56 |
| `netease_client.py` | 83% | 35 |
| `processors/downloader.py` | 35% | 36 |
| `shared/utils.py` | 71% | 27 |
| `processors/organizer.py` | 64% | 25 |
| CLI 层合计（playlist+main+render） | — | ~423（占全部缺口 45%） |

## 批次划分

文件集互不重叠，可安全并行：

| 批次 | 范围 | 主要工作 |
|---|---|---|
| 批 1 | `cli/playlist.py`（非交互路径）、`cli/main.py`、`cli/render.py`、`__main__.py` | 参数驱动非交互路径测试（fake PlaylistUseCase / fake pipeline，参照 `test_cli_semantics.py`）；`__main__.py` 用 runpy 测 |
| 批 2 | `shared/tui_progress.py`、`shared/utils.py`、`shared/output.py` | 纯函数测试 + BatchProgress 行为测试 |
| 批 3 | `processors/metadata_writer.py`、`organizer.py`、`downloader.py`、`decryptor.py`、`filesystem/media_store.py` | fake 依赖测各处理器边界与错误路径 |
| 批 4 | `sync_use_case.py`、`playlist_use_case.py`、`process_use_case.py` 边界路径 | 异常路径、幂等、边界条件（参照现有 fake 端口模式） |
| 批 5 | `netease_client.py`、`targets/filesystem.py`、`bootstrap.py`、`core/config.py`、`preset_api/*`、`state/sqlite.py`、`domain/models.py`、`domain/lyrics.py`、`filesystem/workspace.py`、`shared/output.py` 的零星缺口 | 补齐余量，确保总体 90%+ 有余度 |

## 执行协议（subagent 约束）

- **只读**：现有测试文件、`pyproject.toml`、`tests/__init__.py`。
- **只写**：本批次源文件（仅依赖注入/提取纯函数，行为不变）+ 新增测试文件。
- 不标 `# pragma: no cover`（收尾统一做）。
- 新测试命名 `test_<批次主题>_*.py`，中文 docstring，ruff line-length 120。
- 完成时运行 `python -m pytest tests/ -q` 全量验证（不带 `--cov`，避免 `.coverage` 文件竞争），汇报各自模块的语句覆盖率估算。
- 交互函数（`input()`、二维码轮询）可以跳过不测。

## 收尾（主流程）

1. 复核全部 diff：确认行为未变、无 pragma 滥用。
2. 为交互函数标注 `# pragma: no cover`：`_add_playlist_interactive`、`_remove_playlist_interactive`、`_ensure_cookie` 登录流程等。
3. `pyproject.toml` 配置 `[tool.coverage.run]` + `[tool.coverage.report] fail_under = 90`。
4. 全量验证：`pytest --cov`（≥90%）、`ruff check`、`ruff format --check`。
5. 删除覆盖率 annotate 中间产物（`*.py,cover`）。
6. 分批次 commit：每批一个 commit，收尾一个 commit。

## 验收标准

- `python -m pytest tests/ -q` 全量通过（现有 273 项 + 新增）。
- `python -m pytest --cov=musicvault` 报告 ≥ 90%，低于 90% 时退出码非零。
- `ruff check` / `ruff format --check` 通过。
- 交互路径人工可用（本次不动其逻辑）。

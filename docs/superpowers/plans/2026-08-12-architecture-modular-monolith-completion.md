# 模块化单体架构 spec 收尾实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 `docs/specs/2026-08-05-architecture-modular-monolith-spec.md` 的剩余实现——旧 `sync`/`pull`/`process`/`reindex` 流水线迁移为 application 用例、源端 SDK 端口化、`core.models`/`core.preset` 双轨依赖收敛到 `domain`，使 spec 的 user stories 1/2/6 全部成立。

**Architecture:** 遵循 spec 的模块化单体分层。把 `core/models.py` 与 `core/preset.py` 的纯领域模型移入 `domain/`（消除 domain 反向依赖 core 的双轨）；新建 `ports/source.py` 定义网易云源端能力的端口协议；把 `services/` 三个编排类原样移入 `application/` 并改为用例命名；组装逻辑收敛到 `application/bootstrap.py` 的 composition root。不改变任何运行时行为，全部为结构性迁移 + 类型标注端口化。

**Tech Stack:** Python 3.12+、SQLite、ruff、pytest。无第三方 API 变化。

## Global Constraints

- 依赖方向固定：`presentation → application → domain`，`adapters ────┘`（domain 不得被 adapters 反向依赖；application 不得被 adapters 依赖）。
- `domain` 不得依赖 CLI、Rich、SQLite、网易云 SDK 或 ffmpeg——只能使用 Python 标准库。
- 端口只描述业务需要的能力，不暴露具体第三方类型。
- 业务用例不自行创建数据库连接、SDK 客户端或 Rich 控制台（具体实现由 composition root 组装）。
- 保留 CLI 工具形态；不引入远程目标、双向同步（spec Out of Scope 不变）。
- 项目注释、docstring、commit message 使用中文（既有惯例）。
- 本计划只做结构迁移与类型标注，**不改变任何运行时行为**；迁移完成后全量测试必须保持通过。

---

### Task 1: `Track`/`DownloadedTrack` 移入 `domain/models.py`，删除 `core/models.py`

**Files:**
- Modify: `src/musicvault/domain/models.py`（追加 Track、DownloadedTrack、ALIAS_SPLIT_RE）
- Delete: `src/musicvault/core/models.py`
- Modify（import 改向，共 14 处 src + 9 处 tests）:
  - src: `ports/state.py`、`preset_api/v1.py`、`application/source_state.py`、`adapters/processors/decryptor.py`、`adapters/processors/downloader.py`、`adapters/processors/metadata_writer.py`、`adapters/processors/organizer.py`、`adapters/providers/netease_client.py`、`adapters/state/sqlite.py`、`adapters/filesystem/workspace.py`、`services/sync_service.py`、`services/process_service.py`、`services/run_service.py`
  - tests: `test_models.py`、`test_dry_run.py`、`test_pipeline_to_sqlite.py`、`test_playlist_reconciliation.py`、`test_preset_organizer.py`、`test_reindex_to_sqlite.py`、`test_source_state_recorder.py`、`test_sqlite_state.py`、`test_sync_engine.py`

**Interfaces:**
- Consumes: `core/models.py` 中的 `Track`、`DownloadedTrack`、`ALIAS_SPLIT_RE`（`from __future__ import annotations`、`re`、`unicodedata`、`dataclass`、`typing.Any`）
- Produces: `domain.models` 导出 `Track`、`DownloadedTrack`、`ALIAS_SPLIT_RE` + 已有 `Playlist`、`MediaAsset`、`TargetDescriptor`、`SourceSnapshot`。`Track.from_ncm_payload`、`Track.artist_text`、`Track.alias` 等签名不变。

- [ ] **Step 1: 用 sed 把 src 与 tests 中所有 `from musicvault.core.models` 改为 `from musicvault.domain.models`**

```bash
grep -rl "from musicvault.core.models" src tests --include="*.py" | xargs sed -i 's/from musicvault.core.models/from musicvault.domain.models/g'
grep -rn "musicvault.core.models" src tests --include="*.py" || echo "CLEAN"
```

Expected: 输出 `CLEAN`（无残留）。

- [ ] **Step 2: 把 `core/models.py` 的完整内容追加到 `domain/models.py` 顶部（位于现有 import 之后、`Playlist` 定义之前），并合并 import**

```bash
# 查看 core/models.py 头部 import，与 domain/models.py 合并去重
head -10 src/musicvault/core/models.py
```

用编辑器打开两个文件，把 `core/models.py` 中 `ALIAS_SPLIT_RE`、`Track`、`DownloadedTrack` 三个定义（约 98 行）复制到 `domain/models.py` 中 `Playlist` 定义之前。`domain/models.py` 现有的 `from musicvault.core.models import Track` 行删除（模型已同文件内聚）。合并后 `domain/models.py` 的 import 应为：

```python
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
```

- [ ] **Step 3: 删除 `core/models.py`，更新 `core/config.py` 等对 core 的剩余引用（如有）**

```bash
rm src/musicvault/core/models.py
grep -rn "core.models\|from musicvault.core" src tests --include="*.py" | grep -v "core.config" || echo "CLEAN"
```

Expected: 只剩 `core.config` 引用。

- [ ] **Step 4: 运行全量测试确认迁移无行为变化**

Run: `python -m pytest tests/ -q`
Expected: 全绿（迁移前基线为 163+ 项通过，数量不变）。

- [ ] **Step 5: 运行 ruff 检查**

Run: `python -m ruff check src tests && python -m ruff format --check src tests`
Expected: 无错误（如 ruff 未安装，跳过并记录）。

- [ ] **Step 6: 提交**

```bash
git add -A src tests
git commit -m "refactor: Track/DownloadedTrack 移入 domain.models，删除 core.models"
```

---

### Task 2: `Preset` 模型移入 `domain/preset.py`，删除 `core/preset.py`

**Files:**
- Create: `src/musicvault/domain/preset.py`（内容为 `core/preset.py` 原样复制，import 改向 domain.models）
- Delete: `src/musicvault/core/preset.py`
- Modify（import 改向）: `src/musicvault/core/config.py`、`src/musicvault/services/sync_service.py`、`src/musicvault/services/process_service.py`、`src/musicvault/services/run_service.py`、tests（`test_config_model.py`、`test_dry_run.py`、`test_pipeline_to_sqlite.py`、`test_playlist_reconciliation.py`、`test_preset_model.py`）

**Interfaces:**
- Consumes: `core/preset.py` 全部内容（`Preset`、`audio_spec_key`、`build_audio_specs`、`validate_presets`、`default_presets`、`compute_preset_hash`），无外部依赖（仅 stdlib）。
- Produces: `domain.preset` 导出与 core 同名同签名的全部符号。`domain.preset` 不 import `domain.models`（无需）。

- [ ] **Step 1: 复制并改向**

```bash
cp src/musicvault/core/preset.py src/musicvault/domain/preset.py
grep -rl "from musicvault.core.preset" src tests --include="*.py" | xargs sed -i 's/from musicvault.core.preset/from musicvault.domain.preset/g'
grep -rn "musicvault.core.preset" src tests --include="*.py" || echo "CLEAN"
```

Expected: 输出 `CLEAN`。

- [ ] **Step 2: 删除 `core/preset.py` 并确认 core 目录仅剩 config**

```bash
rm src/musicvault/core/preset.py
ls src/musicvault/core/
```

Expected: `config.py`（`__init__.py`、`__pycache__` 除外）。

- [ ] **Step 3: 运行全量测试**

Run: `python -m pytest tests/ -q`
Expected: 全绿。

- [ ] **Step 4: 运行 ruff 检查**

Run: `python -m ruff check src tests && python -m ruff format --check src tests`
Expected: 无错误。

- [ ] **Step 5: 提交**

```bash
git add -A src tests
git commit -m "refactor: Preset 模型移入 domain.preset，core 收敛为纯配置"
```

---

### Task 3: 新建 `ports/source.py` 源端端口

**Files:**
- Create: `src/musicvault/ports/source.py`
- Modify: `src/musicvault/adapters/providers/netease_client.py`（文件头加一行注释声明实现该端口，不改变代码）

**Interfaces:**
- Consumes: `domain.models.Track`、`domain.preset`（无）；`NeteaseClient` 既有公开方法（已确认签名存在）
- Produces:
```python
from __future__ import annotations

from typing import Any, Protocol

from musicvault.domain.models import Track


class SourceClient(Protocol):
    """网易云源端能力端口：用例只依赖此协议，不依赖具体 SDK 客户端。"""

    def login_with_cookie(self, cookie: str) -> Any: ...

    def get_playlist_info(self, playlist_id: int) -> dict[str, Any]: ...

    def get_playlist_tracks(self, playlist_id: int) -> list[Track]: ...

    def get_tracks_detail(self, track_ids: list[int]) -> dict[int, Track]: ...

    def get_track_detail(self, track_id: int) -> Track | None: ...

    def get_tracks_download_urls(self, track_ids: list[int]) -> dict[int, str | None]: ...

    def get_album_info(self, album_id: int) -> dict[str, Any]: ...

    def get_track_lyrics(self, track_id: int) -> dict[str, str]: ...
```

- [ ] **Step 1: 写入端口文件**

用上述代码创建 `src/musicvault/ports/source.py`（`from __future__ import annotations` 开头，类 docstring 中文）。

- [ ] **Step 2: 在 `netease_client.py` 头部声明端口实现**

在 `src/musicvault/adapters/providers/netease_client.py` 的 `class NeteaseClient:` 定义上方加注释：

```python
# Implements ports.source.SourceClient（protocol，静态鸭子类型）
```

- [ ] **Step 3: 验证端口可被真实实现与 fake 满足（新增协议冒烟测试）**

创建 `tests/test_source_port.py`：

```python
from __future__ import annotations

from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.ports.source import SourceClient


def test_netease_client_satisfies_source_client_port() -> None:
    """真实 SDK 适配器必须满足 SourceClient 协议的全部方法签名。"""
    required = {
        "login_with_cookie",
        "get_playlist_info",
        "get_playlist_tracks",
        "get_tracks_detail",
        "get_track_detail",
        "get_tracks_download_urls",
        "get_album_info",
        "get_track_lyrics",
    }
    assert required <= set(dir(NeteaseClient))


def test_fake_satisfies_source_client_port() -> None:
    """用例的测试接缝：鸭子类型 fake 也可满足端口（运行时无强制校验）。"""

    class FakeSource:
        def login_with_cookie(self, cookie: str) -> None:
            return None

        def get_playlist_info(self, playlist_id: int) -> dict:
            return {"name": "x", "track_count": 0}

        def get_playlist_tracks(self, playlist_id: int) -> list:
            return []

        def get_tracks_detail(self, track_ids: list[int]) -> dict:
            return {}

        def get_track_detail(self, track_id: int) -> None:
            return None

        def get_tracks_download_urls(self, track_ids: list[int]) -> dict:
            return {}

        def get_album_info(self, album_id: int) -> dict:
            return {}

        def get_track_lyrics(self, track_id: int) -> dict:
            return {}

    _: SourceClient = FakeSource()
```

Run: `python -m pytest tests/test_source_port.py -v`
Expected: 2 passed。

- [ ] **Step 4: 提交**

```bash
git add src/musicvault/ports/source.py src/musicvault/adapters/providers/netease_client.py tests/test_source_port.py
git commit -m "feat: 新增 SourceClient 源端端口，NeteaseClient 声明实现"
```

---

### Task 4: `SyncService` → `application/sync_use_case.py`（`SyncUseCase`）

**Files:**
- Move: `src/musicvault/services/sync_service.py` → `src/musicvault/application/sync_use_case.py`
- Modify（类改名 + api 类型端口化）: 移动后的文件
- Modify（import 改向）: `tests/test_dry_run.py`、`tests/test_pipeline_to_sqlite.py`、`tests/test_playlist_reconciliation.py`

**Interfaces:**
- Consumes: Task 1/2/3 产物（`domain.models`、`domain.preset`、`ports.source.SourceClient`）
- Produces: `application.sync_use_case.SyncUseCase`，构造签名不变：
```python
class SyncUseCase:
    def __init__(
        self,
        cfg: Config,
        api: SourceClient,          # 原 NeteaseClient
        downloader: Downloader,
        workers: int,
        state: StateRepository,
        dry_run: bool = False,
    ) -> None:
```
公开方法 `run_sync(cookie: str, playlist_ids: list[int]) -> list[DownloadedTrack]` 与内部方法 `_load_synced_state`、`_find_canonical_for_spec`、`playlist_index` 属性等全部保留（Task 6 的 `PipelineUseCase` 依赖其中几个私有成员）。

- [ ] **Step 1: 移动文件并改名**

```bash
git mv src/musicvault/services/sync_service.py src/musicvault/application/sync_use_case.py
```

- [ ] **Step 2: 更新文件内 import（合并 domain import）**

打开移动后的文件，import 块改为（其余不变）：

```python
from musicvault.application.source_state import SourceStateRecorder
from musicvault.core.config import Config
from musicvault.domain.models import DownloadedTrack, Playlist, Track
from musicvault.domain.preset import Preset, audio_spec_key
from musicvault.ports.source import SourceClient
from musicvault.ports.state import StateRepository
```

（原 `from musicvault.core.models import DownloadedTrack, Track`、`from musicvault.domain.models import Playlist`、`from musicvault.core.preset import Preset, audio_spec_key` 三条合并为上述 domain 两条。）

- [ ] **Step 3: 类改名与类型标注**

- `class SyncService:` → `class SyncUseCase:`（docstring 更新为「同步应用用例：拉取源端曲目、下载与源侧状态登记」）
- `def __init__(..., api: NeteaseClient, ...)` → `api: SourceClient`
- 删除不再需要的 `from musicvault.adapters.providers.netease_client import NeteaseClient` import

- [ ] **Step 4: 更新测试引用**

```bash
sed -i 's/from musicvault.services.sync_service import SyncService/from musicvault.application.sync_use_case import SyncUseCase/g' tests/test_dry_run.py tests/test_pipeline_to_sqlite.py tests/test_playlist_reconciliation.py
sed -i 's/\bSyncService(/SyncUseCase(/g' tests/test_dry_run.py tests/test_pipeline_to_sqlite.py tests/test_playlist_reconciliation.py
grep -rn "SyncService" src tests --include="*.py" || echo "CLEAN"
```

Expected: `CLEAN`。

- [ ] **Step 5: 运行相关测试**

Run: `python -m pytest tests/test_sync_engine.py tests/test_dry_run.py tests/test_pipeline_to_sqlite.py tests/test_playlist_reconciliation.py -q`
Expected: 全绿。

- [ ] **Step 6: 运行全量测试与 ruff**

Run: `python -m pytest tests/ -q && python -m ruff check src tests && python -m ruff format --check src tests`
Expected: 全绿、无 lint 错误。

- [ ] **Step 7: 提交**

```bash
git add -A src tests
git commit -m "refactor: SyncService 迁为 application 用例 SyncUseCase，api 类型端口化"
```

---

### Task 5: `ProcessService` → `application/process_use_case.py`（`ProcessUseCase`）

**Files:**
- Move: `src/musicvault/services/process_service.py` → `src/musicvault/application/process_use_case.py`
- Modify（类改名 + api 类型端口化）: 移动后的文件
- Modify（import 改向）: `tests/test_dry_run.py`、`tests/test_pipeline_to_sqlite.py`

**Interfaces:**
- Consumes: Task 1/2/3 产物
- Produces: `application.process_use_case.ProcessUseCase`，构造签名不变：
```python
class ProcessUseCase:
    def __init__(
        self,
        cfg: Config,
        api: SourceClient,          # 原 NeteaseClient
        decryptor: Decryptor,
        organizer: Organizer,
        metadata: MetadataWriter,
        workers: int,
        state: StateRepository,
        dry_run: bool = False,
    ) -> None:
```
公开方法 `run_process(downloaded, force, playlist_index=None)` 不变。

- [ ] **Step 1: 移动文件**

```bash
git mv src/musicvault/services/process_service.py src/musicvault/application/process_use_case.py
```

- [ ] **Step 2: 更新文件内 import（合并 domain import）**

```python
from musicvault.application.source_state import SourceStateRecorder, build_audio_asset_from_file
from musicvault.core.config import Config
from musicvault.domain.models import DownloadedTrack, MediaAsset, Track
from musicvault.domain.preset import Preset, audio_spec_key, build_audio_specs, compute_preset_hash
from musicvault.ports.source import SourceClient
from musicvault.ports.state import StateRepository
```

- [ ] **Step 3: 类改名与类型标注**

- `class ProcessService:` → `class ProcessUseCase:`（docstring 更新为「处理应用用例：解码、转码、元数据、歌词与 library 硬链接」）
- `def __init__(..., api: NeteaseClient, ...)` → `api: SourceClient`
- 删除 `from musicvault.adapters.providers.netease_client import NeteaseClient` import

- [ ] **Step 4: 更新测试引用**

```bash
sed -i 's/from musicvault.services.process_service import ProcessService/from musicvault.application.process_use_case import ProcessUseCase/g' tests/test_dry_run.py tests/test_pipeline_to_sqlite.py
sed -i 's/\bProcessService(/ProcessUseCase(/g' tests/test_dry_run.py tests/test_pipeline_to_sqlite.py
grep -rn "ProcessService" src tests --include="*.py" || echo "CLEAN"
```

Expected: `CLEAN`。

- [ ] **Step 5: 运行相关测试**

Run: `python -m pytest tests/test_dry_run.py tests/test_pipeline_to_sqlite.py -q`
Expected: 全绿。

- [ ] **Step 6: 运行全量测试与 ruff**

Run: `python -m pytest tests/ -q && python -m ruff check src tests && python -m ruff format --check src tests`
Expected: 全绿、无 lint 错误。

- [ ] **Step 7: 提交**

```bash
git add -A src tests
git commit -m "refactor: ProcessService 迁为 application 用例 ProcessUseCase，api 类型端口化"
```

---

### Task 6: `RunService` → `application/pipeline_use_case.py`（`PipelineUseCase`）

**Files:**
- Move: `src/musicvault/services/run_service.py` → `src/musicvault/application/pipeline_use_case.py`
- Modify（类改名 + api 类型端口化）: 移动后的文件
- Modify（import 改向）: `tests/test_reindex_to_sqlite.py`

**Interfaces:**
- Consumes: Task 4/5 产物（`SyncUseCase`、`ProcessUseCase`）
- Produces: `application.pipeline_use_case.PipelineUseCase`，构造签名不变：
```python
class PipelineUseCase:
    def __init__(
        self,
        cfg: Config,
        api: SourceClient,          # 原 NeteaseClient
        state: StateRepository,
        dry_run: bool = False,
    ) -> None:
```
公开方法 `rebuild_index() -> tuple[int, int]`、`run_pipeline(cookie: str, command: str) -> None`、`link_only(cookie: str) -> tuple[int, int]` 不变。

- [ ] **Step 1: 移动文件**

```bash
git mv src/musicvault/services/run_service.py src/musicvault/application/pipeline_use_case.py
```

- [ ] **Step 2: 更新文件内 import**

```python
from musicvault.application.process_use_case import ProcessUseCase
from musicvault.application.source_state import SourceStateRecorder, build_audio_asset_from_file
from musicvault.application.sync_use_case import SyncUseCase
from musicvault.core.config import Config
from musicvault.domain.models import Playlist, Track
from musicvault.domain.preset import audio_spec_key, compute_preset_hash
from musicvault.ports.source import SourceClient
from musicvault.ports.state import StateRepository
```

（原 `from musicvault.services.process_service import ProcessService` → `application.process_use_case`；`from musicvault.services.sync_service import SyncService` → `application.sync_use_case`；`from musicvault.core.models import Track` → `domain.models`；`from musicvault.domain.models import Playlist` 合并；`from musicvault.core.preset import ...` → `domain.preset`。）

- [ ] **Step 3: 类改名与类型标注**

- `class RunService:` → `class PipelineUseCase:`（docstring 更新为「流水线用例：sync/pull/process/reindex 的编排与源侧状态登记」）
- `def __init__(..., api: NeteaseClient, ...)` → `api: SourceClient`
- 删除 `from musicvault.adapters.providers.netease_client import NeteaseClient` import
- 内部 `self.sync_service` / `self.process_service` 属性名保留（不重命名，避免无关 diff）

- [ ] **Step 4: 更新测试引用与 `cli/main.py` 的懒加载 import**

`cli/main.py` 在 reindex 与 sync/pull/process 路径有函数体 lazy import（约 294、353 行），必须同步改向，否则这两个命令在运行时 `ModuleNotFoundError`：

```bash
sed -i 's/from musicvault.services.run_service import RunService/from musicvault.application.pipeline_use_case import PipelineUseCase/g' tests/test_reindex_to_sqlite.py src/musicvault/cli/main.py
sed -i 's/\bRunService(/PipelineUseCase(/g' tests/test_reindex_to_sqlite.py src/musicvault/cli/main.py
grep -rn "RunService" src tests --include="*.py" || echo "CLEAN"
grep -rn "musicvault.services" src tests --include="*.py" || echo "CLEAN"
```

Expected: 两个 `CLEAN`（services 包已无引用；main.py 中的 `service = PipelineUseCase(cfg=..., api=NeteaseClient(...), state=...)` 实例化保留，Task 7 再收敛为 `build_pipeline`）。

- [ ] **Step 5: 运行相关测试**

Run: `python -m pytest tests/test_reindex_to_sqlite.py -q`
Expected: 全绿。

- [ ] **Step 6: 运行全量测试与 ruff**

Run: `python -m pytest tests/ -q && python -m ruff check src tests && python -m ruff format --check src tests`
Expected: 全绿、无 lint 错误。

- [ ] **Step 7: 提交**

```bash
git add -A src tests
git commit -m "refactor: RunService 迁为 application 用例 PipelineUseCase，api 类型端口化"
```

---

### Task 7: composition root 收敛——`bootstrap.build_pipeline` + `cli/main.py` 去组装

**Files:**
- Modify: `src/musicvault/application/bootstrap.py`
- Modify: `src/musicvault/cli/main.py`（sync/pull/process/reindex 四条路径改用 bootstrap）
- Create: `tests/test_bootstrap_pipeline.py`

**Interfaces:**
- Consumes: Task 4/5/6 产物、`Config`、`NeteaseClient`
- Produces:
```python
# application/bootstrap.py 新增
def build_source_client(config: Config) -> NeteaseClient:
    """创建网易云源端 SDK 适配器（composition root 专属）。"""
    return NeteaseClient(
        text_cleaning_enabled=config.text_cleaning_enabled,
        download_quality=config.download_quality,
        api_download_url_chunk_size=config.api_download_url_chunk_size,
        api_track_detail_chunk_size=config.api_track_detail_chunk_size,
        alias_split_separators=config.alias_split_separators,
    )


def build_pipeline(
    config: Config,
    source: SourceClient | None = None,
    *,
    dry_run: bool = False,
) -> PipelineUseCase:
    """组装旧流水线用例的具体依赖；测试可注入 fake source。"""
    if source is None:
        source = build_source_client(config)
    return PipelineUseCase(
        cfg=config,
        api=source,
        state=SQLiteStateRepository(SQLiteState(config.state_db_file)),
        dry_run=dry_run,
    )
```

- [ ] **Step 1: 扩展 `bootstrap.py`**

在 `bootstrap.py` 中加入 `build_source_client` 与 `build_pipeline`（import 新增：`from musicvault.adapters.providers.netease_client import NeteaseClient`、`from musicvault.application.pipeline_use_case import PipelineUseCase`、`from musicvault.ports.source import SourceClient`）。

- [ ] **Step 2: 重写 `cli/main.py` 的 reindex 路径**

`src/musicvault/cli/main.py` 的 `reindex` 分支（约 290-309 行）改为：

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

- [ ] **Step 3: 重写 `cli/main.py` 的 sync/pull/process 路径**

`src/musicvault/cli/main.py` 末尾（约 352-365 行）的组装代码改为：

```python
    from musicvault.application.bootstrap import build_pipeline

    service = build_pipeline(cfg, dry_run=getattr(args, "dry_run", False))
```

删除原来的 `from musicvault.adapters.providers.netease_client import NeteaseClient`、`from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository`、`from musicvault.services.run_service import RunService` 与 `service = RunService(cfg=cfg, api=NeteaseClient(...), state=SQLiteStateRepository(...))` 组装块。

- [ ] **Step 4: 确认 main.py 中无残留的 `NeteaseClient`/`SQLiteState` 组装**

Run: `grep -n "NeteaseClient\|SQLiteState\|build_pipeline" src/musicvault/cli/main.py`
Expected: 组装只剩 `build_pipeline` 调用（`NeteaseClient` 不再出现在 main.py；`_ensure_cookie` 等登录路径若仍直接使用 `NeteaseClient` 属于「交互式登录」presentation 职责，保留）。

- [ ] **Step 5: 新增 bootstrap 冒烟测试**

创建 `tests/test_bootstrap_pipeline.py`：

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.application.bootstrap import build_pipeline
from musicvault.core.config import Config


def test_build_pipeline_with_fake_source(tmp_path: Path) -> None:
    """composition root 用注入的 fake source 即可组装完整流水线。"""
    cfg = Config(workspace=str(tmp_path / "ws"))
    service = build_pipeline(cfg, source=MagicMock(), dry_run=True)
    assert service.cfg is cfg
    assert service.dry_run is True
    # 用例持有的状态仓储已指向 workspace 下的 SQLite
    assert service.recorder.state is not None
```

Run: `python -m pytest tests/test_bootstrap_pipeline.py -v`
Expected: 1 passed。

- [ ] **Step 6: 运行全量测试与 ruff**

Run: `python -m pytest tests/ -q && python -m ruff check src tests && python -m ruff format --check src tests`
Expected: 全绿、无 lint 错误。

- [ ] **Step 7: 提交**

```bash
git add src tests
git commit -m "refactor: CLI 组装收敛到 bootstrap.build_pipeline，main.py 仅保留输入输出"
```

---

### Task 8: 删除 `services/` 目录、全量验证、更新文档

**Files:**
- Delete: `src/musicvault/services/`（`__init__.py` 及三个已迁移文件）
- Modify: `docs/specs/2026-08-05-architecture-modular-monolith-spec.md`（状态行）
- Modify: `docs/wayfinder/2026-08-05-musicvault-refactor-map.md`（状态与 remaining）

**Interfaces:** 无（纯收尾）。

- [ ] **Step 1: 删除 services 目录**

```bash
rm -rf src/musicvault/services
grep -rn "musicvault.services" src tests --include="*.py" || echo "CLEAN"
```

Expected: `CLEAN`。

- [ ] **Step 2: 更新 spec 状态**

`docs/specs/2026-08-05-architecture-modular-monolith-spec.md` 顶部两行改为：

```markdown
# 模块化单体与端口适配器 Spec

状态：已完成（2026-08-12）
```

并在文件末尾「Further Notes」后追加一节：

```markdown
## Completion Notes

2026-08-12 完成剩余迁移：

- `core/models.py` 与 `core/preset.py` 已收敛为 `domain/models.py` 与 `domain/preset.py`，domain 自包含、不再依赖 core。
- 源端网易云能力由 `ports/source.py` 的 `SourceClient` 协议描述，`NeteaseClient` 为实现。
- 旧 `sync`/`pull`/`process`/`reindex` 流水线已从 `services/` 迁为 `application/sync_use_case.py`、`application/process_use_case.py`、`application/pipeline_use_case.py` 应用用例，API 类型全部端口化，可用 fake source 测试。
- 所有具体依赖组装收敛到 `application/bootstrap.py`（`build_runtime`/`build_pipeline`），CLI 只保留输入输出与登录交互。
- 已知偏离：`application` 用例内部仍直接使用 `shared/tui_progress` 的 Rich 进度展示（spec 实现决策中「Rich 输出由 presentation 层决定」对 target-sync 新链路已成立，旧流水线用例的输出剥离留待后续）。
```

- [ ] **Step 3: 更新 refactor map**

`docs/wayfinder/2026-08-05-musicvault-refactor-map.md`：
- 状态行 `状态：部分完成（最小闭环已实现）` → `状态：已完成（2026-08-12）`
- `## Remaining implementation work` 一节改写为：

```markdown
## Remaining implementation work

- 已按《模块化单体与端口适配器 Spec》完成旧流水线迁移接缝：`sync`/`pull`/`process`/`reindex` 已迁为 application 用例（`SyncUseCase`/`ProcessUseCase`/`PipelineUseCase`），源端 SDK 端口化（`ports/source.py`），CLI 组装收敛到 `bootstrap.build_pipeline`，`services/` 目录已删除。
- 后续可选：application 用例的 Rich 输出剥离（spec Completion Notes 已注明）；Manifest 决策完成后的 managed 目标清理；MediaResolver 按需生成与目标元数据/歌词端口。
```

- [ ] **Step 4: 全量验证**

Run: `python -m pytest tests/ -q && python -m ruff check src tests && python -m ruff format --check src tests`
Expected: 全绿、无 lint 错误。统计测试数量并记录（基线 163+）。

- [ ] **Step 5: 冒烟验证 CLI 装配（无副作用：不加载 config、不创建 workspace）**

Run: `python -m musicvault --help && python -c "import musicvault.cli.main; import musicvault.application.bootstrap; import musicvault.application.pipeline_use_case; print('IMPORTS OK')"`
Expected: 帮助正常输出；`IMPORTS OK`（`services` 缺失不影响任何导入链）。

- [ ] **Step 6: 提交**

```bash
git add -A src tests docs
git commit -m "chore: 完成模块化单体 spec 收尾，删除 services 层，更新文档状态"
```

---

## Self-Review

**Spec 覆盖核对：**
- user story 1（核心业务不依赖外部 SDK）：✅ Task 3/4/5/6——`SourceClient` 端口 + 用例类型标注。
- user story 2（文件系统副作用通过端口测试）：✅ 既有 `StateRepository`/`TargetOperations`/`MediaResolver` 端口 + Task 3 源端端口；Task 7 的 build_pipeline 支持注入 fake。
- user story 3（CLI 只负责输入和展示）：✅ Task 7——组装收敛 bootstrap，CLI 仅留参数解析、登录、输出与退出码。
- user story 4（preset 只依赖公开 API）：✅ 既有 `preset_api.v1`；本次不动 preset_api。
- user story 5（仍为本地单进程程序）：✅ 无新部署形态。
- user story 6（不访问网易云/不跑 ffmpeg 测试主要用例）：✅ 既有 MagicMock 测试 + Task 3 端口 + Task 7 fake source 注入。
- user story 7（单一变化原因）：✅ Task 1/2 消除 core 双轨；Task 4/5/6 每文件单职责。
- user story 8（基础设施异常转稳定应用错误）：✅ 既有（CLI 捕获转退出码）；本次不回归。
- user story 9（composition root 集中创建）：✅ Task 7——bootstrap.build_pipeline/build_source_client。
- user story 10（保留 CLI 形态允许内部破坏性重构）：✅ 命令形态不变。
- 实现决策「Rich 输出由 presentation 层决定」：部分——target-sync 已合规；旧用例输出剥离记为已知偏离（Task 8 文档注明）。

**占位符扫描：** 无 TBD/TODO；所有代码块为最终内容。

**类型一致性：** `SourceClient` 协议方法签名与 `NeteaseClient` 既有公开方法一致（login_with_cookie/get_playlist_info/get_playlist_tracks/get_tracks_detail/get_track_detail/get_tracks_download_urls/get_album_info/get_track_lyrics 均已确认存在于 `adapters/providers/netease_client.py`）。用例构造签名在各任务间保持一致（cfg/api/state/dry_run 顺序不变）。

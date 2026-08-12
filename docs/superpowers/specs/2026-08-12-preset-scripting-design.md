# MusicVault 重构：基于 Python 脚本的 preset 体系与四阶段链路

日期：2026-08-12

## 背景与动机

当前项目存在两条职责重叠的流水线：

1. **旧命令流**（`sync`/`pull`/`process`）：配置驱动（`Config.presets` 声明式 dataclass），`ProcessUseCase` 硬编码全部行为——按规格集合转码、文本级歌词合并、`_link_track` 建 library 硬链接。
2. **目标同步新链路**（`presets` → `target-sync`）：Python 脚本 preset（`TargetSynchronizer`），从 SQLite 快照重建目标端。

重叠点：旧链路的 `_link_track` 与新链路的 `playlist_links` 都在 library 中建硬链接；旧链路的物化职责（下载/转码/歌词）尚未脚本化。

重构目标：

- **preset 脚本化**：preset 声明音频规格（音质）、歌词格式（代码函数）、歌词编码、元数据粒度；sync_target 脚本按名称引用 preset 并定义保存逻辑。
- **四阶段链路**：`fetch`（拉歌单元数据）→ `pull`（增量下载最高音质 + 存储统一歌词格式）→ `process`（按 preset 声明压缩到 media_store）→ `distribute`（分发到目标端）。pull 获取最完整信息，process 按需压缩。
- **统一歌词格式**：结构化按行模型（逐行/逐字/翻译/罗马音），SQLite 存储，process 离线消费，canonical 不再内嵌歌词。
- **命令收敛**：只保留 `sync [--no-distribute]` 与 `distribute`。

## 目标架构

```
sync [--no-distribute]
│
fetch ──► pull ──► process ──► distribute（默认启用）
│          │          │           │
│ 拉歌单元   │ 增量下载    │ 按 preset    │ 按 sync_target
│ 数据/改名  │ (最高音质)  │ 声明转码     │ 分发到目标端
│ 删除检测   │ 详情+歌词   │ 生成歌词文件  │
│          │ →SQLite    │ 写元数据     │
└──────────┴────────────┴─────────────┴────────────────┘
                          │                                │
                    media_store/                    目标端（library/ 等）
                    <tid>/{tid}_{bitrate}.ext
                    <tid>/{tid}.{preset}.lrc
```

**组件职责**：

| 组件 | 职责 |
|---|---|
| `domain/lyrics.py`（新） | 统一歌词结构化模型 `LyricLine`/`LyricWord` + JSON 序列化（纯模型，无依赖） |
| `preset_api/` | 两套公开 API：preset 脚本（声明规格/歌词函数/元数据）+ sync_target 脚本（引用 preset、定义分发）；歌词渲染工具库 |
| `adapters/processors/lyrics.py`（改造） | 原始 payload → 结构化行列表转换器（复用现有 YRC/LRC 解析） |
| `application/` | SyncUseCase 拆 fetch/pull 阶段；ProcessUseCase 离线消费；SyncEngine 作为 distribute 引擎 |
| `cli/` | 命令收敛为 `sync [--only-process] [--no-distribute]` + `distribute` |

## 命令层

| 命令 | 变化 |
|---|---|
| `sync` | 保留，四阶段默认全跑；新增 `--only-distribute` `--no-distribute` 选项 |
| `distribute` | `target-sync` 改名；单独执行分发（复用 SyncEngine） |
| `presets` | 保留，列出 preset 与 sync_target 两类脚本 |
| `add/remove/list/init/help` | 保留 |
| `pull` / `process` | **移除**（并入 sync 内部阶段） |

## 统一歌词格式

### 领域模型（`domain/lyrics.py`）

```python
@dataclass(frozen=True, slots=True)
class LyricWord:
    start_ms: int
    text: str

@dataclass(frozen=True, slots=True)
class LyricLine:
    start_ms: int
    duration_ms: int                   # 0 = 未知（标准 LRC 无行时长）
    text: str                          # 逐行原文（去时间戳）
    words: tuple[LyricWord, ...] = ()  # 逐字（YRC 有；标准 LRC 为空）
    translation: str = ""              # 时间戳对齐的翻译（无则空串）
    romaji: str = ""
```

### 转换器（改造 `adapters/processors/lyrics.py`）

- 输入：原始 payload dict（`lrc`/`tlyric`/`romalrc`/`yrc`/`ytlrc`/`yromalrc` 六个 key）
- 清洗（去 JSON 元信息行）保留，只做存储前一次性处理
- **YRC 优先**：逐行 `_parse_yrc_line` → `start_ms/duration_ms/words/text`；翻译/罗马音从 ytlrc/yromalrc 按起始时间对齐（复用 `_find_translation_fuzzy`，200ms 容差）
- **标准 LRC 回退**：逐行解析 → `start_ms/text`，无逐字；重复时间戳行**拆成多行**；翻译/罗马音按时间戳 map 精确匹配 + fuzzy 回退
- 输出：`tuple[LyricLine, ...]`
- 现有文本级合并渲染（`merge_translation` 等）退役，渲染逻辑迁移到 preset_api 基于行模型重写

### 存储（`adapters/state/sqlite.py`）

```sql
CREATE TABLE lyrics (
    track_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,   -- JSON 序列化行列表
    fetched_at REAL NOT NULL
);
```

- pull 阶段写入（与 track 详情同批）；process 离线读取（不再调歌词 API）
- 无歌词/获取失败 → 存空列表，不阻塞曲目处理
- 歌词只对**新增曲目**拉取（已有曲目不重拉）

### 歌词文件产物（process 阶段）

- 对每个 preset：调 `build_lyrics(lines) -> str` → 按 preset 声明编码序列写入 `media_store/<tid>/{tid}.{preset}.lrc`
- 返回空串则跳过写文件
- 歌词文件不登记为资产（路径按约定可推导）；distribute 原样分发

## 两套脚本 API

### 枚举与元数据（`preset_api/v1.py`）

```python
class Quality(Enum):
    STANDARD = "standard"; HIGHER = "higher"; EXHIGH = "exhigh"
    HIRES = "hires"; LOSSLESS = "lossless"

class AudioFormat(Enum):
    FLAC = "flac"; MP3 = "mp3"; AAC = "aac"; OGG = "ogg"; OPUS = "opus"

class LyricEncoding(Enum):
    UTF_8 = "utf-8"; GB18030 = "gb18030"
    # 编码保留「序列回退」语义：tuple[LyricEncoding, ...] 按顺序尝试写入

@dataclass(frozen=True, slots=True)
class MetadataSpec:
    embed_cover: bool = True
    cover_max_size: int = 0
    fields: tuple[str, ...] = _FULL_FIELDS     # 白名单内的字段名

    @classmethod
    def full(cls) -> "MetadataSpec":   # 封面 + 全部字段
    @classmethod
    def basic(cls) -> "MetadataSpec":   # 封面 + fields=()（空集=不限制，写入器按全部可用字段写入）
    @classmethod
    def none(cls) -> "MetadataSpec":    # 无封面 + 无字段（同 basic，仅 embed_cover=False）
    # 构造函数可覆盖任意项：MetadataSpec.basic(embed_cover=False)
```

### preset 脚本

```python
class BasePreset:
    """preset 脚本的基类：声明式字段 + 歌词函数（可覆盖）"""
    quality: Quality = Quality.HIRES
    format: AudioFormat | None = None
    bitrate: str | None = None                  # 码率字符串（如 "192k"）
    lyrics_encodings: tuple[LyricEncoding, ...] = (LyricEncoding.UTF_8,)
    metadata: MetadataSpec = MetadataSpec.basic()

    def build_lyrics(self, lines: tuple[LyricLine, ...]) -> str:
        """歌词转换：接收统一结构化行，返回目标文本（默认标准 LRC）。"""
        return standard_lrc(lines)

@dataclass(frozen=True, slots=True)
class PresetRegistration:
    name: str
    factory: Any                    # 预设类/工厂，create() 后得到 Preset 对象
    api_version: str = API_VERSION
    enabled: bool = True
    source: str = "<runtime>"
```

外部脚本示例：

```python
from musicvault.preset_api.v1 import (
    BasePreset, PresetRegistration,
    enhanced_lrc,  # 渲染工具库
)

class MyPreset(BasePreset):
    quality = Quality.HIRES
    format = AudioFormat.FLAC
    lyrics_encodings = (LyricEncoding.UTF_8,)
    metadata = MetadataSpec.full()

    def build_lyrics(self, lines):
        return enhanced_lrc(lines, include_translation=True, include_romaji=True)

def register(registry):
    registry.register_preset(PresetRegistration(name="my_preset", factory=MyPreset))
```

要点：

- 声明式字段（音频规格/编码/元数据粒度）+ 代码函数（歌词转换）混合
- `BasePreset` 是普通类（类属性声明，无需 dataclass），用户只覆盖需要的部分
- 渲染工具库（`standard_lrc`/`enhanced_lrc`/`plain_text`）基于行模型，预设函数组合使用或完全自定义
- 移除声明式歌词选项（`use_karaoke`/`include_translation`/`translation_format`/`include_romaji`/`write_lrc_file`）——被 `build_lyrics` 函数取代；`filename_template` 移除（保存逻辑归 sync_target）
- `domain/preset.py` 的 `Preset` dataclass 退役；`audio_spec_key`/`build_audio_specs` 等辅助函数迁移到 preset_api

### sync_target 脚本

```python
@dataclass(frozen=True, slots=True)
class TargetRegistration:
    name: str
    factory: Any                       # factory(presets: Mapping[str, Preset]) -> TargetSynchronizer
    depends_on: tuple[str, ...] = ()   # 引用的 preset 名称
    api_version: str = API_VERSION
    enabled: bool = True
    source: str = "<runtime>"
    target: TargetDescriptor | None = None
```

- `PresetRegistry` 拆双注册：`register_preset()` / `register_target()`，内部两个索引
- `create()` 时框架解析 `depends_on`：把依赖的 preset **实例对象**注入 factory；依赖缺失 → 清晰报错（"sync_target 'x' 依赖的 preset 'y' 未注册"）
- preset 脚本与 sync_target 脚本共用 `register(registry)` 入口（`load_directories` 加载两种）
- `TargetSynchronizer` 生命周期（prepare → sync_item → finalize）与失败隔离语义保留

### PresetContext 扩展

- 新增 `lyrics_file(track_id, preset_name) -> Path | None`：按 `media_store/<tid>/{tid}.{preset}.lrc` 约定查歌词文件

## 内置脚本

`preset_api/builtins.py`：

```python
class ArchivePreset(BasePreset):
    quality = Quality.HIRES
    format = AudioFormat.FLAC
    metadata = MetadataSpec.full()
    def build_lyrics(self, lines):
        return enhanced_lrc(lines, include_translation=True, include_romaji=True)

class HardlinkDistributor:
    """按歌单目录硬链接分发指定 preset 的音频与歌词（按曲目幂等）。"""
    def __init__(self, presets: Mapping[str, Preset]):
        self.preset = presets["archive"]        # 引用内置 archive
    def sync_item(self, track, context):
        asset = context.media_asset(track.id, spec=audio_spec_key(self.preset.format, self.preset.bitrate))
        if asset is None: return
        owned = {safe_filename(pl.name) for pl in context.playlists if track.id in pl.track_ids} or {未分类}
        # 1) 删除本曲目在 library 其他歌单目录下的旧链接（仅 inode 与 canonical 一致的自建链接）
        # 2) 在所属歌单目录建立缺失链接（音频 + 歌词）
    def finalize(self, context):
        # 删除快照中不存在的歌单目录（内容全为硬链接，rmtree 不伤 canonical）
```

- 默认 preset 仅 archive（portable 移除）；默认 sync_target 为 hardlink
- **library/ = hardlink sync_target 的专属导出根**（bootstrap 注入 `target_root=library/`），不会有别的 target 使用该目录
- 布局：`library/<歌单名>/<文件>`、`library/未分类/`（default_playlist_name）
- 链接文件名模板归 sync_target 自己定义（`format_track_name` 工具保留）
- **幂等语义**（取代 append）：sync_item 对每个曲目——先删除本曲目在非所属歌单目录下的旧链接（**仅限 inode 与 canonical 一致的自建硬链接**，`st_dev+st_ino` 匹配，绝不误删用户文件），再在所属歌单目录建缺失链接；finalize 删除快照中不存在的歌单目录（目录内容全为硬链接，rmtree 安全）；未分类目录中 inode 属于快照 canonical 但该曲目已有歌单归属的链接同样清理（曲目无歌单归属时留在未分类）；快照之外的孤儿文件一律保留
- 歌单改名/分配变化/远端删除的 library 清理全部由 distribute 幂等重建覆盖；fetch 阶段只更新 SQLite 关系，不碰 library

## 处理链路重构

### fetch（拉取歌单元数据，不下载）

- 登录 → 拉取歌单详情/曲目列表 → 检测改名/删除/分配变化 → 更新 SQLite 歌单/曲目关系
- 现状拆分：`SyncUseCase.run_sync` 的元数据前半段独立为 fetch 阶段
- **不碰 library**：链接的移动/删除/改名后重建全部由 distribute 的 hardlink 幂等重建覆盖（`_handle_playlist_rename`/`_reconcile_playlist_assignments` 的链接操作移除）

### pull（增量下载/移除，最完整信息入库）

- 对比新曲目（`_diff_tracks`）→ 下载（Quality 枚举最大值 → `level=.value`）→ `get_tracks_detail` 详情 + `get_track_lyrics` 歌词同批获取 → 歌词转统一格式写入 SQLite `lyrics` 表 → 移除远端已删曲目（`_prune_stale_tracks`）、清理过期状态

### process（按 preset 声明压缩，完全离线歌词消费）

```
解密（如需）→ route_audio 按全部 preset 的 spec 集合转码
→ 读 SQLite 歌词 → 反序列化行列表
→ 写元数据（标签+封面，按各 preset 的 MetadataSpec；共享 spec 求并集）
→ 对每个 preset：build_lyrics(lines) → 文本 → 按 LyricEncoding 序列写 {tid}.{preset}.lrc
```

- 不再调用歌词 API；不再 `_link_track`（保存归 distribute）
- 不再需要歌单映射（歌单关系由 distribute 消费）
- process 仍持有 api（年份回退等增强需要），但歌词不依赖网络

### distribute（分发）

- 复用 `SyncEngine`（prepare → sync_item → finalize、preset 失败隔离、dry-run 无副作用），输入为 target 注册表 + preset 实例索引
- `distribute` 命令与 sync 内部 distribute 阶段共用同一引擎

### 移除项

- `_link_track`、`_process_local` 本地独立模式、`_filter_pending` 的 spec 覆盖跳过保留（force 重处理）
- `Config.presets`、`Config.metadata_fields`（归 MetadataSpec）、`filename_template`（归 sync_target）

### 并发与状态

- 下载/处理 ThreadPoolExecutor 保留；SourceStateRecorder 保留（tracks/playlists/assets），新增 lyrics 表写入

## 配置变化（`core/config.py`）

- **移除**：`presets`（脚本化）、`metadata_fields`（归 MetadataSpec）、`builtin_playlist_links_enabled`（改为 `preset_system.builtin: bool = True` 单开关控制内置 archive+hardlink）
- **保留**：`preset_directories`（外部脚本目录）、`default_playlist_name`（hardlink 的未分类目录）、网络/worker/ffmpeg 各项
- `Config.preset_dir(name)` 方法删除；`library_dir` 语义 = hardlink target 根
- **兼容读取**：旧配置中的 `presets` 数组字段忽略（宽容读取不报错），文档说明已脚本化——单用户项目，不写迁移机制
- `download_quality` 不再落配置：bootstrap 从 preset 注册表取全部 preset 的 `Quality` 取最大 → 传 `NeteaseClient`

## bootstrap（`application/bootstrap.py`）

- `build_runtime`：注册内置（archive preset + hardlink target）→ 加载外部目录（两类脚本）→ 校验 target 依赖 → 推导 download_quality
- 状态库：`lyrics` 新表；现有 `presets` 表加 `kind` 列（preset/target）区分两类注册

## 文件布局

**新增**：

- `domain/lyrics.py` — LyricLine/LyricWord + JSON 序列化（纯模型）
- `adapters/state/sqlite.py` 扩展 — `lyrics` 表、`presets` 表加 `kind` 列

**改造**：

- `preset_api/v1.py` — 枚举、MetadataSpec、BasePreset、PresetRegistration、TargetRegistration（depends_on 注入）、渲染工具、`PresetContext.lyrics_file`
- `preset_api/builtins.py` — ArchivePreset + HardlinkDistributor
- `adapters/processors/lyrics.py` — "payload → 行列表"转换器（原文本级渲染迁移至 preset_api）
- `adapters/processors/organizer.py` / `metadata_writer.py` — AudioFormat 枚举化；MetadataWriter 去掉嵌入歌词、MetadataSpec 驱动
- `application/sync_use_case.py` — 拆 fetch/pull 两阶段
- `application/process_use_case.py` — 离线歌词消费、调 preset 函数、移除 _link_track/_process_local
- `application/pipeline_use_case.py` — 四阶段编排 + sync --no-distribute
- `application/sync_engine.py` — 接收 target 注册表 + preset 实例索引
- `application/bootstrap.py` — 双类注册、依赖校验、quality 推导
- `core/config.py` / `cli/main.py` / `adapters/providers/netease_client.py` — 命令与配置变化
- `adapters/filesystem/workspace.py` — media_store 扁平化（`<tid>/audio/` → `<tid>/`）

**删除**：`domain/preset.py`（辅助函数迁 preset_api）、lyrics.py 文本级渲染、PlaylistUseCase 的 preset 目录遍历清理、SyncUseCase 的 library 链接相关（`_create_track_links`/`_remove_track_links`/`_handle_playlist_rename` 的链接操作/`_reconcile_playlist_assignments`——语义由 hardlink 幂等重建覆盖）、PipelineUseCase 的 `_cleanup_uncategorized_orphans`（由 hardlink 幂等清理覆盖）

## 错误处理与事务

- pull 歌词获取失败：降级存空列表，不阻塞下载
- process 歌词生成失败（preset 函数异常）：该 preset 歌词文件跳过，不阻塞整曲（按 preset 隔离）
- 依赖缺失（sync_target 引用未注册 preset）：加载时报错终止
- SQLite 写入走事务（现有模式）
- dry-run：fetch/pull/process 只计算不执行；distribute 沿用 OperationExecutor 的 PLANNED/SKIPPED 语义

## 测试策略

现有 234 项测试大改/重写，重点新增：

1. 转换器：原始 payload → LyricLine（YRC 逐字、标准 LRC、重复时间戳拆行、翻译/罗马音对齐）
2. LyricLine JSON 序列化往返
3. 渲染工具：standard/enhanced/plain 输出
4. process 离线消费：fake state 供歌词 → 调 preset 函数 → 歌词文件/元数据断言
5. fetch/pull 拆分：fake source client 断言调用序列（下载质量 = 最大值）
6. distribute：SyncEngine + 依赖注入、依赖缺失报错、dry-run
7. 配置：旧 presets 字段宽容忽略

## 文档更新

- `CONTEXT.md` 术语表：预设（Preset）新定义（声明音频规格/歌词/元数据的脚本）、统一歌词格式（Unified Lyrics Format）、fetch/pull/process/distribute 术语
- `AGENTS.md` 架构段更新（两套脚本体系、四阶段链路、命令收敛）

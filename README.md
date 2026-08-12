# MusicVault (msv)

网易云音乐本地同步与整理工具 — 将你的歌单自动同步到本地，按 preset（内置无损 `archive`，可自由增删改）分流处理，并写入完整元数据与歌词。

> [!IMPORTANT]
> 此项目使用 Vibe Coding，绝大部分代码由 AI 编写

> [!NOTE]
> 此项目为本人自用

## 功能

- **交互式登录** — 支持二维码、密码、验证码三种登录方式（`msv init`）
- **交互式歌单管理** — 可从账号歌单中浏览选择，也支持手动输入 ID 或链接
- **自动增量同步** — 拉取远端新增曲目，清理远端已删除曲目（以远端为准）
- **多线程下载** — 自动根据 CPU 核心数调整并发数
- **Preset 脚本化处理** — preset 是 Python 脚本声明的处理规格（音质、输出格式/码率、封面嵌入、歌词输出函数、元数据粒度），内置 `archive`（无损：Hi-Res 音质、逐字歌词 / 标准歌词含翻译、完整元数据 + 封面），可通过外部脚本目录自由增删改
- **四阶段流水线** — `sync` 按 fetch（拉取元数据）→ pull（下载与歌词入库）→ process（按 preset 处理）→ distribute（分发到目标端）顺序执行
- **歌词翻译合并** — 支持独立行带时间戳（separate）、同行前置（inline）、独立行无时间戳（notimestamp）三种翻译格式，可选附带罗马音
- **多歌单共享曲目** — 同一曲目出现在多个歌单时使用硬链接，节省磁盘空间
- **NCM 解密** — 自动解密网易云 `.ncm` 加密文件
- **断点续传安全** — 状态文件采用原子写入，防止中断损坏

## 安装

```bash
pip install -e .
```

依赖项（由 `pyproject.toml` 声明）：

- `pymusiclibrary` — 网易云 API 封装（NeteaseCloudMusicApi Python 绑定）；**注意：此依赖是本地构建 wheel**（`D:\MyPC\Dev\Projects\ncm-python-sdk`，分支 `fix/playlist-large-response-crash`），不是 PyPI 版本
- `mutagen` — 音频元数据写入
- `ncmdump-py` — NCM 文件解密
- `rich` — 终端 UI 美化
- `qrcode` — 终端二维码生成
- `ffmpeg` — 音频转码（系统需安装）

## 用法

### 命令总览

| 命令 | 说明 |
|------|------|
| `msv init` | 登录并初始化配置文件 |
| `msv sync` | 完整流水线：拉取 + 下载 + 处理 + 分发（四阶段） |
| `msv distribute` | 仅执行分发：从 SQLite 源快照重建目标端 |
| `msv add` | 添加要同步的歌单或单曲 |
| `msv remove` | 移除已添加的歌单或单曲 |
| `msv rm` | `remove` 的别名 |
| `msv list` | 查看已添加的歌单或单曲 |
| `msv ls` | `list` 的别名 |
| `msv presets` | 列出内置和外部 Python preset 与 sync_target |
| `msv help` | 显示帮助信息 |

文件落位：canonical 文件写入 `media_store/<track_id>/`（扁平布局，含 `<track_id>.flac/.mp3` 及 `<track_id>.<preset>.lrc`），下载缓存与解密中间产物落 `cache/`（临时目录，可清理）。

### msv init — 登录并初始化配置

首次使用先运行 `msv init`：交互式登录（二维码 / 密码 / 验证码）并在当前目录生成 `config.json`。也可直接用 Cookie 跳过交互：

```bash
msv init                          # 交互式登录
msv init --cookie "MUSIC_U=..."   # 直接写入 Cookie
```

### msv sync — 同步音乐（四阶段）

完整流水线：fetch 拉取歌单元数据 → pull 下载新曲目与歌词（统一歌词格式入库）→ process 处理（解密、转码、写元数据、写歌词文件）→ distribute 分发（重建 `library/` 目标视图）：

```bash
msv sync [--config CONFIG] [--cookie COOKIE] [--workspace WORKSPACE] [--force] [--dry-run]
         [--no-distribute | --only-distribute] [-v]
```

- `--no-distribute`：同步完成后跳过分发阶段
- `--only-distribute`：仅执行分发（library 重建），跳过拉取/下载/后处理

### msv distribute — 仅执行分发

从 SQLite 源快照按 sync_target 声明重建目标端（`--dry-run` 只展示操作计划，不产生副作用）：

```bash
msv distribute [--config CONFIG] [--workspace WORKSPACE] [--preset NAME]... [--dry-run] [-v]
```

### 目标分发闭环（preset 脚本）

处理规格由 **preset 脚本**声明（音频规格、歌词输出函数、元数据粒度），**sync_target 脚本**引用 preset 并定义目标端写入逻辑。内置脚本为 `archive` preset + `hardlink` sync_target（按歌单目录硬链接重建 `library/`，幂等清理陈旧链接）；外部脚本通过配置中的 `preset_system.directories` 发现，并依赖版本化的 `musicvault.preset_api.v1`。`distribute` 命令与 `sync` 的 distribute 阶段在一次运行中为所有启用 sync_target 共享同一个 SQLite `SourceSnapshot`：

```bash
msv presets --workspace ./workspace
msv distribute --workspace ./workspace [--preset hardlink] [--dry-run]
```

`library/` 是可重建的目标视图：由 hardlink distribute 从 SQLite 状态（DB → library）重建，不直接从下载目录扫描。

### msv add — 添加歌单

```bash
# 交互式：从账号歌单列表中浏览选择
msv add

# 指定歌单 ID
msv add 123456789

# 指定歌单链接
msv add https://music.163.com/playlist?id=123456789

# 添加单曲（可多个，单独管理，不入歌单）
msv add --song 2068041065 2068041066
```

### msv remove — 移除歌单

```bash
# 交互式：从已添加歌单中选择移除
msv remove

# 指定歌单 ID
msv remove 123456789

# 移除单曲
msv remove --song 2068041065
```

移除歌单时会同时清理对应的本地音乐文件和处理索引。

### msv list — 查看歌单

```bash
msv list
msv ls    # 等效

# 查看单独管理的单曲列表
msv list --song
```

### 通用选项

| 选项 | 说明 |
|------|------|
| `--config PATH` | 配置文件路径（默认 `./config.json`，可通过 `MUSIC_VAULT_CONFIG` 环境变量覆盖） |
| `--cookie STRING` | 网易云 Cookie 字符串 |
| `--workspace PATH` | 工作目录（默认 `./workspace`） |
| `--force` | 强制重处理已处理文件 |
| `--dry-run` | 预览模式：执行全部查询，但不下载、不写入任何文件（`sync`/`distribute` 可用） |
| `-v, --verbose` | 启用详细日志输出 |

## 首次使用流程

```
msv init          # 登录并生成 config.json
msv add           # 选择要同步的歌单（或 msv add <歌单ID/链接>）
msv sync          # 开始同步
```

如果未先登录就运行 `msv sync`，会自动进入交互式登录，登录完成后退出并提示下一步操作（添加歌单）。

## 配置文件

首次运行后自动在项目目录生成 `config.json`。所有配置项均有默认值，可按需修改。

处理行为不再由配置数组定义：**preset 已脚本化**（内置 `archive` + `hardlink`，开关为 `preset_system.builtin`；外部脚本目录为 `preset_system.directories`）。旧配置中的 `presets` 数组字段会被宽容忽略（不解析不报错），可手动删除。

```json
{
  "cookie": "",
  "workspace": "./workspace",
  "text_cleaning": {
    "enabled": true,
    "allowlist": ""
  },
  "workers": {
    "download": null,
    "process": null,
    "ffmpeg_threads": null
  },
  "network": {
    "download_timeout": 30,
    "api_timeout": 15,
    "cover_timeout": 15,
    "max_retries": 3
  },
  "process": {
    "keep_downloads": false
  },
  "playlist": {
    "default_name": "未分类"
  },
  "ffmpeg": {
    "path": ""
  },
  "api": {
    "download_url_chunk_size": 200,
    "track_detail_chunk_size": 500
  },
  "alias": {
    "split_separators": "/、;；"
  },
  "preset_system": {
    "directories": [],
    "builtin": true
  }
}
```

### 配置项说明

| 分组 | 字段 | 默认值 | 说明 |
|------|------|--------|------|
| 顶层 | `cookie` | `""` | 网易云 Cookie（登录后自动填入） |
| 顶层 | `workspace` | `"./workspace"` | 工作目录路径 |
| `text_cleaning` | `enabled` | `true` | 是否清理 API 返回文本中的不可见字符 |
| `text_cleaning` | `allowlist` | `""` | Unicode 类别白名单（空=内置规则） |
| `workers` | `download` | `null` | 下载并发数（null=自动，上限6） |
| `workers` | `process` | `null` | 处理并发数（null=自动，上限4） |
| `workers` | `ffmpeg_threads` | `null` | ffmpeg 编码线程数（null=自动） |
| `network` | `download_timeout` | `30` | 下载 HTTP 超时（秒） |
| `network` | `api_timeout` | `15` | API 调用超时（秒） |
| `network` | `cover_timeout` | `15` | 封面下载超时（秒） |
| `network` | `max_retries` | `3` | 最大重试次数 |
| `process` | `keep_downloads` | `false` | 是否保留 `cache/` 中的原始下载文件（默认清理） |
| `playlist` | `default_name` | `"未分类"` | 无歌单关联曲目的默认分类名 |
| `ffmpeg` | `path` | `""` | ffmpeg 手动路径（空=自动从 PATH 检测） |
| `api` | `download_url_chunk_size` | `200` | 下载 URL 批量请求大小 |
| `api` | `track_detail_chunk_size` | `500` | 曲目详情批量请求大小 |
| `alias` | `split_separators` | `"/、;；"` | 别名拆分分隔符字符集 |
| `preset_system` | `directories` | `[]` | 外部 preset / sync_target 脚本目录列表 |
| `preset_system` | `builtin` | `true` | 是否启用内置 `archive` preset + `hardlink` sync_target（旧键名 `playlist_links` 自动迁移） |

环境变量：

- `MUSIC_VAULT_CONFIG` — 指定配置文件路径（优先级高于 `--config` 选项的默认值）

## 目录结构

运行后默认在 `workspace/` 下生成：

```
workspace/
├── cache/                临时文件（下载缓存、解密中间产物，可随时清理）
├── media_store/
│   └── <track_id>/       长期媒体资产（canonical 文件：{track_id}.flac/.mp3 及 {track_id}.{preset}.lrc）
├── library/              可重建的目标视图（由 hardlink distribute 从 DB 重建，按歌单名分目录）
│   └── <歌单名>/         如：<歌单名>/{artist} - {name}.flac
├── logs/                 运行日志
└── state.db              SQLite 状态库（schema 版本化）
```

## 开发

```bash
# 安装可编辑模式
uv pip install -e .

# 运行测试
python -m pytest tests/ -v

# 代码检查
ruff check src/ tests/
ruff format --check src/ tests/
```

# MusicVault (msv)

网易云音乐本地同步与整理工具 — 将你的歌单自动同步到本地，按无损/有损分流整理，并写入完整元数据与歌词。

> [!IMPORTANT]
> 此项目使用 Vibe Coding，绝大部分代码由 AI 编写

> [!NOTE]
> 此项目为本人自用

## 功能

- **交互式登录** — 支持二维码、密码、验证码三种登录方式
- **交互式歌单管理** — 可从账号歌单中浏览选择，也支持手动输入 ID 或链接
- **自动增量同步** — 拉取远端新增曲目，清理远端已删除曲目（以远端为准）
- **多线程下载** — 自动根据 CPU 核心数调整并发数
- **无损/有损分流**
  - `lossless`：Hi-Res 音质、逐字歌词 / 标准歌词（含翻译、可选罗马音）、完整元数据 + 封面
  - `lossy`：压缩后 mp3/aac/ogg/opus 格式、LRC 歌词（编码可配）、基本元数据
- **灵活配置** — 下载质量、封面嵌入、歌词嵌入、文件名模板、有损格式/码率、翻译格式、网络参数等均可自定义
- **歌词翻译合并** — 支持独立行（separate）或同行前置（inline）两种翻译格式，可选附带罗马音
- **多歌单共享曲目** — 同一曲目出现在多个歌单时使用硬链接，节省磁盘空间
- **NCM 解密** — 自动解密网易云 `.ncm` 加密文件
- **断点续传安全** — 状态文件采用原子写入，防止中断损坏

## 安装

```bash
pip install -e .
```

依赖项（由 `pyproject.toml` 声明）：

- `pyncm` — 网易云 API 封装
- `mutagen` — 音频元数据写入
- `ncmdump-py` — NCM 文件解密
- `rich` — 终端 UI 美化
- `qrcode` — 终端二维码生成
- `ffmpeg` — 音频转码（系统需安装）

## 用法

### 命令总览

| 命令 | 说明 |
|------|------|
| `msv sync` | 完整流水线：拉取 + 下载 + 处理 |
| `msv pull` | 仅拉取与下载，不进行后处理 |
| `msv process` | 仅对本地下载文件进行后处理 |
| `msv add` | 添加要同步的歌单 |
| `msv remove` | 移除已添加的歌单 |
| `msv list` | 查看已添加的歌单 |
| `msv ls` | `list` 的别名 |
| `msv help` | 显示帮助信息 |

### msv sync — 同步音乐

完整流水线：从网易云拉取歌单 → 下载新曲目 → 处理（解密、转码、写元数据、写歌词）

```bash
msv sync [--config CONFIG] [--cookie COOKIE] [--workspace WORKSPACE] [--force] [--no-translation] [-v]
```

### msv pull — 仅拉取下载

仅执行同步阶段（拉取歌单 + 下载），不进行后处理。适合先下载、稍后再批量处理的场景。

```bash
msv pull [--config CONFIG] [--cookie COOKIE] [--workspace WORKSPACE] [-v]
```

### msv process — 仅后处理

对本地已下载的文件进行后处理（解密、转码、写元数据、写歌词）。需要 `processed_files.json` 索引存在。

```bash
msv process [--config CONFIG] [--cookie COOKIE] [--workspace WORKSPACE] [--force] [--no-translation] [-v]
```

### msv add — 添加歌单

```bash
# 交互式：从账号歌单列表中浏览选择
msv add

# 指定歌单 ID
msv add 123456789

# 指定歌单链接
msv add https://music.163.com/playlist?id=123456789
```

### msv remove — 移除歌单

```bash
# 交互式：从已添加歌单中选择移除
msv remove

# 指定歌单 ID
msv remove 123456789
```

移除歌单时会同时清理对应的本地音乐文件和处理索引。

### msv list — 查看歌单

```bash
msv list
msv ls    # 等效
```

### 通用选项

| 选项 | 说明 |
|------|------|
| `--config PATH` | 配置文件路径（默认 `./config.json`，可通过 `MUSIC_VAULT_CONFIG` 环境变量覆盖） |
| `--cookie STRING` | 网易云 Cookie 字符串 |
| `--workspace PATH` | 工作目录（默认 `./workspace`） |
| `--force` | 强制重处理已处理文件 |
| `--no-translation` | 关闭歌词翻译合并（默认开启） |
| `-v, --verbose` | 启用详细日志输出 |

## 首次使用流程

```
msv add           # 交互式登录选择要同步的歌单
msv sync          # 开始同步
```

## 配置文件

首次运行后自动在项目目录生成 `config.json`。所有配置项均有默认值，可按需修改。

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
  "lyrics": {
    "lossy_lrc_encodings": ["utf-8"],
    "embed_in_metadata": true,
    "write_lrc_file": true,
    "lossless_use_karaoke": true,
    "lossy_use_karaoke": false,
    "include_romaji": false,
    "include_translation": true,
    "translation_format": "separate"
  },
  "lossy": {
    "bitrate": "192k",
    "format": "mp3"
  },
  "download": {
    "quality": "hires"
  },
  "cover": {
    "embed": true,
    "max_size_kb": 0
  },
  "filenames": {
    "lossless": "{artist} - {name}",
    "lossy": "{alias} {name} - {artist}"
  },
  "network": {
    "download_timeout": 30,
    "api_timeout": 15,
    "cover_timeout": 15,
    "max_retries": 3
  },
  "metadata": {
    "fields": null
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
| `lyrics` | `lossy_lrc_encodings` | `["utf-8"]` | LRC 文件编码顺序 |
| `lyrics` | `embed_in_metadata` | `true` | 是否在音频元数据中嵌入歌词 |
| `lyrics` | `write_lrc_file` | `true` | 是否写入独立 `.lrc` 文件 |
| `lyrics` | `lossless_use_karaoke` | `true` | 无损是否启用逐字（Karaoke）歌词 |
| `lyrics` | `lossy_use_karaoke` | `false` | 有损是否启用逐字（Karaoke）歌词 |
| `lyrics` | `include_romaji` | `false` | 是否在歌词中附加罗马音（三行输出） |
| `lyrics` | `include_translation` | `true` | 是否合并翻译歌词 |
| `lyrics` | `translation_format` | `"separate"` | 翻译格式：`separate`（独立行）/ `inline`（同行前置） |
| `lossy` | `bitrate` | `"192k"` | 有损编码码率 |
| `lossy` | `format` | `"mp3"` | 有损输出格式：`mp3`/`aac`/`ogg`/`opus` |
| `download` | `quality` | `"hires"` | 下载音质：`standard`/`higher`/`exhire`/`hires`/`lossless` |
| `cover` | `embed` | `true` | 是否在音频中嵌入封面图 |
| `cover` | `max_size_kb` | `0` | 封面最大尺寸限制（0=不限制） |
| `filenames` | `lossless` | `"{artist} - {name}"` | 无损文件名模板 |
| `filenames` | `lossy` | `"{alias} {name} - {artist}"` | 有损文件名模板 |
| `network` | `download_timeout` | `30` | 下载 HTTP 超时（秒） |
| `network` | `api_timeout` | `15` | API 调用超时（秒） |
| `network` | `cover_timeout` | `15` | 封面下载超时（秒） |
| `network` | `max_retries` | `3` | 最大重试次数 |
| `metadata` | `fields` | `null` | 写入的元数据字段列表（null=全部） |
| `process` | `keep_downloads` | `false` | 是否保留原始下载文件 |
| `playlist` | `default_name` | `"未分类"` | 无歌单关联曲目的默认分类名 |
| `ffmpeg` | `path` | `""` | ffmpeg 手动路径（空=自动从 PATH 检测） |
| `api` | `download_url_chunk_size` | `200` | 下载 URL 批量请求大小 |
| `api` | `track_detail_chunk_size` | `500` | 曲目详情批量请求大小 |
| `alias` | `split_separators` | `"/、;；"` | 别名拆分分隔符字符集 |

### 文件名模板占位符

支持以下占位符，可在 `filenames.lossless` 和 `filenames.lossy` 中自由组合：
为空时自动忽略

| 占位符 | 说明 | 示例 |
|--------|------|------|
| `{name}` / `{title}` | 歌曲名 | `リレイアウター` |
| `{artist}` | 歌手（多个以 `/` 分隔） | `稲葉曇, 歌愛ユキ` |
| `{alias}` | 第一个别名（无别名时为空，不会留下多余空格） | `中继输出者` |
| `{album}` | 专辑名 | `リレイアウター` |
| `{track_id}` | 曲目 ID | `2068041065` |

环境变量：

- `MUSIC_VAULT_CONFIG` — 指定配置文件路径（优先级高于 `--config` 选项的默认值）

## 目录结构

运行后默认在 `workspace/` 下生成：

```
workspace/
├── downloads/           原始下载文件 + canonical 文件（{track_id}.flac/.mp3）
│   └── cache/           下载缓存
├── state/
│   ├── synced_tracks.json     已同步曲目 ID 索引
│   ├── processed_files.json   已处理文件索引
│   └── playlists.json         歌单元数据缓存
└── library/
    ├── lossless/         无损结果（.flac 或 .mp3，完整元数据）
    │   └── <歌单名>/
    └── lossy/            有损结果（.mp3/.m4a/.ogg/.opus，基本元数据 + .lrc）
        └── <歌单名>/
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

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
  - `lossless`：Hi-Res 音质、逐字歌词（含翻译）、完整元数据 + 封面
  - `lossy`：压缩后 mp3 格式、LRC 歌词（GB18030/UTF-8-SIG 编码）、基本元数据
- **歌词翻译合并** — 支持无损内嵌翻译歌词，有损 LRC 翻译前置
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

首次运行后自动在项目目录生成 `config.json`，完整结构如下：

```json
{
  "cookie": "", // 网易云 Cookie（登录后自动填入）
  "workspace": "./workspace", // 工作目录路径
  "playlist_ids": [], // 要同步的歌单 ID 列表
  "force": false, // 是否每次强制重处理
  "include_translation": true, // 是否合并翻译歌词
  "text_cleaning": { // 是否清理 API 返回文本中的不可见字符
    "enabled": true
  },
  "workers": { 
    "download": null, // 下载并发数（null = 自动，取 CPU 核心数，上限 6）
    "process": null, // 处理并发数（null = 自动，取 CPU 核心数一半，上限 4）
    "ffmpeg_threads": null // ffmpeg 编码线程数（null = 自动）
  },
  "lyrics": {
    // LRC 文件编码（按顺序尝试写入，首个失败则尝试下一个）
    "lossy_lrc_encodings": ["gb18030", "utf-8-sig"]
  }
}
```

环境变量：

- `MUSIC_VAULT_CONFIG` — 指定配置文件路径（优先级高于 `--config` 选项的默认值）

## 目录结构

运行后默认在 `workspace/` 下生成：

```
workspace/
├── downloads/           原始下载文件（.ncm / .flac / .mp3）
├── decoded/             NCM 解密后的临时文件
├── state/
│   ├── synced_tracks.json     已同步曲目 ID 索引
│   ├── processed_files.json   已处理文件索引
│   └── playlists.json         歌单元数据缓存
└── library/
    ├── lossless/         无损结果（Hi-Res 音质 + 逐字歌词 + 完整元数据 + 封面）
    │   └── <歌单名>/
    └── lossy/            有损结果（mp3 + LRC 歌词 + 基本元数据）
        └── <歌单名>/
```

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
python -m pytest tests/ -v

# 代码检查
ruff check src/ tests/
ruff format --check src/ tests/
```

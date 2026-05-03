# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# 安装（可编辑模式）
uv pip install -e .

# 初始化（登录并创建配置文件）
msv init

# 同步（拉取 + 后处理）
msv sync

# 仅拉取下载
msv pull

# 仅后处理本地文件
msv process

# 管理歌单
msv add <歌单ID或链接>    # 添加歌单
msv remove [歌单ID]       # 移除歌单（可交互选择）
msv list                  # 列出已添加歌单

# 查看帮助
msv help [子命令]

# 常用参数
msv sync --force          # 强制重新处理所有文件
msv sync -v               # 详细日志

# 运行测试
python -m pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format --check src/ tests/
```

`msv` 是 `musicvault` 的短别名，两者可互换。配置默认读取 `./config.json`，也可通过 `--config` 参数或 `MUSIC_VAULT_CONFIG` 环境变量指定。

## Architecture

MusicVault is a CLI tool that syncs NetEase Cloud Music playlists to a local library, organized into configurable presets (e.g., archive, portable) with embedded metadata and lyrics.

**Three-layer design:** `cli` calls `services`, which use `adapters/providers` and `adapters/processors`. Shared models live in `core/`; shared utilities in `shared/`. 所有源码位于 `src/musicvault/` 下。

```
cli/main.py           → argparse，构建子命令，组装 Config + RunService + 双击 Ctrl+C 信号处理
cli/playlist.py       → 交互式歌单管理（添加/移除/列出）
services/run_service.py   → 顶层流水线：sync → pull / process，组装各服务并注入配置
services/sync_service.py  → 歌单 diff 与本地状态，并行下载新曲目
services/process_service.py → 解密、路由（按 preset 规格去重）、写入元数据与歌词、LRC 侧车
adapters/providers/pyncm_client.py → pyncm 封装：登录、歌单、URL、歌词（接受下载质量、批次大小等配置）
adapters/processors/downloader.py   → HTTP 下载，基于 Content-Type 检测扩展名，可配置文件名模板
adapters/processors/decryptor.py    → .ncm 解密（ncmdump-py）
adapters/processors/organizer.py    → ffmpeg 路由：多规格输出（按 format+bitrate 去重），返回 {spec: Path}
adapters/processors/metadata_writer.py → mutagen：mp3 用 ID3，flac 用 Vorbis + 封面。策略由 caller 按 preset 合并决定（无 is_lossless）
adapters/processors/lyrics.py       → StandardLyrics/KaraokeLyrics 双接口，原文/翻译/罗马音合并输出，GB18030 .lrc 输出
core/preset.py   → Preset 数据类（16 字段，slots=True）：音频格式/质量/元数据/歌词策略全部独立配置
core/models.py   → Track（统一模型）、DownloadedTrack、别名拆分与文本清理
core/config.py   → Config 数据类（presets 驱动，from_dict/to_dict 序列化）
shared/utils.py  → safe_filename、format_track_name（模板化文件名）、load_json/save_json（原子写入）
shared/output.py → 用户向输出（success/warn/error/info），与 logging 分离
shared/tui_progress.py → 基于 Rich 的 BatchProgress 进度条、状态轮播
```

**Config priority:** CLI args > `config.json` file > built-in defaults.

**Pipeline flow:** `RunService.run_pipeline()` creates `SyncService` + `ProcessService`. SyncService logs in via cookie, resolves "liked songs" playlist (specialType=5), diffs against `state/synced_tracks.json`, downloads new tracks in parallel. ProcessService decrypts .ncm, routes audio to N canonical files (deduped by `(format, bitrate)` spec), writes metadata, and links into `library/<preset.name>/<playlist>/`. LRC sidecars are per-preset: `{track_id}.{preset.name}.lrc`.

**Config 结构：** `presets` 是核心 — 每个 preset 定义完整的输出规格（音频、元数据、歌词）。顶层字段为全局公共配置。

```json
{
    "presets": [{
        "name": "archive",          // 唯一 ID，也是 library 目录名
        "quality": "hires",         // standard|higher|exhigh|hires|lossless
        "format": "flac",           // flac|mp3|aac|ogg|opus|null(保持源格式)
        "bitrate": null,            // 有损码率，如 "192k"
        "filename_template": "{artist} - {name}",
        "embed_cover": true,
        "cover_max_size": 0,
        "embed_lyrics": true,
        "metadata_fields": null,    // null=全部, 或 ["year","genre"]
        "use_karaoke": true,        // 逐字 YRC 歌词
        "include_translation": true,
        "translation_format": "separate",  // separate|inline|notimestamp
        "include_romaji": false,
        "write_lrc_file": false,
        "lrc_encodings": ["utf-8"]
    }]
}
```

| 全局分组 | 关键字段 | 默认值 |
|---------|---------|--------|
| `workers` | `download`, `process`, `ffmpeg_threads` | `null` (auto) |
| `network` | `download_timeout`, `api_timeout`, `cover_timeout`, `max_retries` | `30`, `15`, `15`, `3` |
| `process` | `keep_downloads` | `false` |
| `playlist` | `default_name` | `"未分类"` |
| `ffmpeg` | `path` (空=自动检测) | `""` |
| `api` | `download_url_chunk_size`, `track_detail_chunk_size` | `200`, `500` |
| `alias` | `split_separators` | `"/、;；"` |
| `metadata` | `fields` (null=全部，全局回退) | `null` |
| `text_cleaning` | `enabled`, `allowlist` | `true`, `""` |

`download_quality` 从所有 preset 的 `quality` 中自动取最高值。

**Key design decisions:**
- `Config` 和 `Preset` 均为 `@dataclass(slots=True)` 模型，通过 `from_dict()`/`to_dict()` 与嵌套 JSON 互转。
- **Preset 系统**：N 个 preset 替代固定的 lossless/lossy。每个 preset 独立配置输出目录、音频格式/码率、文件名模板、元数据策略、歌词类型与翻译格式。音频文件按 `(format, bitrate)` 去重 — 相同规格的多个 preset 共享同一 canonical 文件，节省磁盘。`download_quality` 从所有 preset 取最高值。
- **元数据策略**：所有 canonical 文件写全量元数据。共享文件时取 preset 间并集（embed_cover/embed_lyrics 用 OR，metadata_fields 用 union）。
- **歌词**：`StandardLyrics`（LRC）和 `KaraokeLyrics`（YRC 逐字）双接口。LRC 侧车文件按 preset 独立命名 `{track_id}.{preset.name}.lrc`。嵌入歌词取共享 preset 中"最丰富"的（Karaoke > 标准，含翻译 > 不含）。
- **Library 链接**：`library/<preset.name>/<playlist>/<filename>.<ext>`，跨歌单共享硬链接。
- `Track.from_ncm_payload()` sanitizes text (zero-width chars, control chars) and normalizes the varied NetEase API field names.
- State files use atomic write (write to `.tmp`, then replace) to survive interruption.
- `format_track_name()` in `shared/utils.py` supports `{name}`, `{artist}`, `{alias}`, `{album}`, `{track_id}` placeholders.
- Old config.json 格式（含 `lossy`、`filenames`、`cover`、`lyrics` 顶层键）启动时报错退出，需手动迁移。
- This is a personal-use tool ("Vibe Coding" by AI/Codex). Chinese is the primary language for comments and user-facing messages.

## Gotchas

- **双击 Ctrl+C 强制退出**：首次 Ctrl+C 触发优雅关闭（保存状态后退出）；在关闭过程中再次 Ctrl+C 会立即 `os._exit(130)` 终止，不保存任何状态。信号处理在 `cli/main.py:_handle_double_sigint()` 中实现。
- **旧版 config.json 启动报错**：含 `lossy`/`filenames`/`cover`/`lyrics` 顶层键的配置文件不再支持，需手动迁移为 preset 格式。
- `playlist_ids` 已从 config.json 迁移到 `state/playlists.json` 独立索引文件。
- 网易云 API 的逐字歌词翻译 key 是 `ytlrc`（不是 `ytlyric`）。`get_track_lyrics()` 同时返回 6 个字段：`lrc`、`tlyric`、`romalrc`、`yrc`、`ytlrc`、`yromalrc`。

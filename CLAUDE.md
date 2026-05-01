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
msv sync --no-translation # 关闭翻译合并
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

MusicVault is a CLI tool that syncs NetEase Cloud Music playlists to a local library, organized into lossless and lossy copies with embedded metadata and lyrics.

**Three-layer design:** `cli` calls `services`, which use `adapters/providers` and `adapters/processors`. Shared models live in `core/`; shared utilities in `shared/`. 所有源码位于 `src/musicvault/` 下。

```
cli/main.py           → argparse，构建子命令，组装 Config + RunService + 双击 Ctrl+C 信号处理
cli/playlist.py       → 交互式歌单管理（添加/移除/列出）
services/run_service.py   → 顶层流水线：sync → pull / process，组装各服务并注入配置
services/sync_service.py  → 歌单 diff 与本地状态，并行下载新曲目
services/process_service.py → 解密、路由（lossless/lossy）、写入元数据与歌词
adapters/providers/pyncm_client.py → pyncm 封装：登录、歌单、URL、歌词（接受下载质量、批次大小等配置）
adapters/processors/downloader.py   → HTTP 下载，基于 Content-Type 检测扩展名，可配置文件名模板
adapters/processors/decryptor.py    → .ncm 解密（ncmdump-py）
adapters/processors/organizer.py    → ffmpeg 路由：lossless → flac；lossy → mp3/aac/ogg/opus（可配置）
adapters/processors/metadata_writer.py → mutagen：mp3 用 ID3，flac 用 Vorbis + 封面（嵌入可配置）
adapters/processors/lyrics.py       → StandardLyrics/KaraokeLyrics 双接口，原文/翻译/罗马音合并输出，GB18030 .lrc 输出
core/models.py   → Track（统一模型）、DownloadedTrack、别名拆分与文本清理
core/config.py   → Config 数据类（30+ 扁平字段，14 个 JSON 分组，from_dict/to_dict 序列化）
shared/utils.py  → safe_filename、format_track_name（模板化文件名）、load_json/save_json（原子写入）
shared/output.py → 用户向输出（success/warn/error/info），与 logging 分离
shared/tui_progress.py → 基于 Rich 的 BatchProgress 进度条、状态轮播
```

**Config priority:** CLI args > `config.json` file > built-in defaults.

**Pipeline flow:** `RunService.run_pipeline()` creates `SyncService` + `ProcessService`. SyncService logs in via cookie, resolves "liked songs" playlist (specialType=5), diffs against `state/synced_tracks.json`, downloads new tracks in parallel. ProcessService decrypts .ncm, routes audio to `library/lossless/` and `library/lossy/`, writes metadata, and outputs `.lrc` sidecar for lossy (all LRC/metadata/cover embedding behavior is configuration-controlled).

**Config 主要 JSON 分组** (每个对应 Config dataclass 的扁平字段):

| 分组 | 关键字段 | 默认值 |
|------|---------|--------|
| `download` | `quality` (standard~lossless) | `"hires"` |
| `cover` | `embed`, `max_size_kb` | `true`, `0` |
| `lyrics` | `lossy_lrc_encodings`, `embed_in_metadata`, `write_lrc_file`, `lossless_use_karaoke`, `lossy_use_karaoke`, `include_romaji`, `include_translation`, `translation_format` | `["utf-8"]`, `true`, `true`, `true`, `false`, `false`, `true`, `"separate"` |
| `lossy` | `bitrate`, `format` (mp3/aac/ogg/opus) | `"192k"`, `"mp3"` |
| `filenames` | `lossless`, `lossy`（`{artist}`,`{name}`,`{alias}`,`{album}`,`{track_id}` 等占位符） | `"{artist} - {name}"`, `"{alias} {name} - {artist}"` |
| `network` | `download_timeout`, `api_timeout`, `cover_timeout`, `max_retries` | `30`, `15`, `15`, `3` |
| `metadata` | `fields` (null=全部) | `null` |
| `process` | `keep_downloads` | `false` |
| `playlist` | `default_name` | `"未分类"` |
| `ffmpeg` | `path` (空=自动检测) | `""` |
| `api` | `download_url_chunk_size`, `track_detail_chunk_size` | `200`, `500` |
| `alias` | `split_separators` | `"/、;；"` |
| `text_cleaning` | `enabled`, `allowlist` | `true`, `""` |
| `workers` | `download`, `process`, `ffmpeg_threads` | `null` (auto) |

**Key design decisions:**
- `Config` 是单一 `@dataclass(slots=True)` 模型，字段扁平存储，通过 `from_dict()`/`to_dict()` 与嵌套 JSON 互转。所有配置项均有默认值，旧 config.json 无需迁移即可正常加载。
- `Track.from_ncm_payload()` sanitizes text (zero-width chars, control chars) and normalizes the varied NetEase API field names. Alias splitting regex is configurable via `alias.split_separators`.
- Lossless files get full metadata. Lossy gets basic ID3 tags only. 封面嵌入、歌词嵌入、LRC 文件写入均可独立开关。元数据字段可通过 `metadata.fields` 精确选择。
- Lyrics 拆分为两个独立接口：`StandardLyrics`（LRC 格式，提供 `lrc`/`tlyric`/`romalrc`）和 `KaraokeLyrics`（YRC 逐字格式，提供 `yrc`/`ytlrc`/`yromalrc`）。每个都支持 `merge_translation()`、`merge_romaji()`、`merge_all()`（三行合并）。`lyrics.translation_format` 控制翻译合并方式：`"separate"`（独立行）或 `"inline"`（同行前置）。`lyrics.include_translation` 控制是否合并翻译（默认开启）。罗马音启用由 `lyrics.include_romaji` 控制（默认关闭）。
- State files use atomic write (write to `.tmp`, then replace) to survive interruption.
- Filename generation uses configurable templates via `format_track_name()` in `shared/utils.py`. Lossless and lossy naming patterns are independently configurable.
- This is a personal-use tool ("Vibe Coding" by AI/Codex). Chinese is the primary language for comments and user-facing messages.

## Gotchas

- **双击 Ctrl+C 强制退出**：首次 Ctrl+C 触发优雅关闭（保存状态后退出）；在关闭过程中再次 Ctrl+C 会立即 `os._exit(130)` 终止，不保存任何状态。信号处理在 `cli/main.py:_handle_double_sigint()` 中实现。
- 有损源文件不再转码为 FLAC，直接用 ID3 存储在 `.mp3` 中（lossless 和 lossy 指向同一文件）。
- `playlist_ids` 已从 config.json 迁移到 `state/playlists.json` 独立索引文件。
- 网易云 API 的逐字歌词翻译 key 是 `ytlrc`（不是 `ytlyric`）。`get_track_lyrics()` 同时返回 6 个字段：`lrc`、`tlyric`、`romalrc`、`yrc`、`ytlrc`、`yromalrc`。

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# 安装（可编辑模式）
uv pip install -e .

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
cli/main.py           → argparse，构建子命令，组装 Config + RunService
cli/playlist.py       → 交互式歌单管理（添加/移除/列出）
services/run_service.py   → 顶层流水线：sync → pull / process
services/sync_service.py  → 歌单 diff 与本地状态，并行下载新曲目
services/process_service.py → 解密、路由（lossless/lossy）、写入元数据与歌词
adapters/providers/pyncm_client.py → pyncm 封装：登录、歌单、URL、歌词
adapters/processors/downloader.py   → HTTP 下载，基于 Content-Type 检测扩展名
adapters/processors/decryptor.py    → .ncm 解密（ncmdump-py）
adapters/processors/organizer.py    → ffmpeg 路由：lossless → flac/wav/ape + mp3 转换
adapters/processors/metadata_writer.py → mutagen：mp3 用 ID3，flac 用 Vorbis + 封面
adapters/processors/lyrics.py       → LRC/YRC 解析，翻译合并，GB18030 .lrc 输出
core/models.py   → Track（统一模型）、DownloadedTrack
core/config.py   → Config 数据类（单一模型，from_dict/to_dict 序列化）
shared/utils.py  → safe_filename、load_json/save_json（通过 .tmp 原子写入）
shared/output.py → 用户向输出（success/warn/error/info），与 logging 分离
shared/tui_progress.py → 基于 Rich 的 BatchProgress 进度条、状态轮播
```

**Config priority:** CLI args > `config.json` file > built-in defaults.

**Pipeline flow:** `RunService.run_pipeline()` creates `SyncService` + `ProcessService`. SyncService logs in via cookie, resolves "liked songs" playlist (specialType=5), diffs against `state/synced_tracks.json`, downloads new tracks in parallel. ProcessService decrypts .ncm, routes audio to `library/lossless/` and `library/lossy/`, writes metadata, and outputs GB18030 `.lrc` sidecar for lossy.

**Key design decisions:**
- `Config` 是单一 `@dataclass(slots=True)` 模型，负责 JSON 文件与运行时配置的映射。通过 `from_dict()`/`to_dict()` 序列化，`load()`/`save()` 管理持久化。workers、lyrics、text_cleaning 均为扁平字段，无需独立子对象。
- `Track.from_ncm_payload()` sanitizes text (zero-width chars, control chars) and normalizes the varied NetEase API field names.
- Lossless files get full metadata + embedded lyrics + cover art. Lossy files get minimal ID3 tags + external `.lrc` only.
- Lyrics use only NetEase's `tlyric` (translation). Lossless: translation as a separate timestamped line. Lossy: translation prepended inline on the same line.
- State files use atomic write (write to `.tmp`, then replace) to survive interruption.
- The `text_cleaning.enabled` config flag controls recursive string sanitization on API responses.
- This is a personal-use tool ("Vibe Coding" by AI/Codex). Chinese is the primary language for comments and user-facing messages.

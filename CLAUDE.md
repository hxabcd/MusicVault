# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in editable mode (ensure .venv deps are installed first)
uv pip install -e .

# Run the full pipeline
musicvault run --cookie "MUSIC_U=...; __csrf=..." --workspace ./workspace

# Sync (download) only
musicvault sync

# Post-process local downloads only
musicvault process

# Run tests
python -m pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format --check src/ tests/
```

## Architecture

MusicVault is a CLI tool that syncs NetEase Cloud Music playlists to a local library, organized into lossless and lossy copies with embedded metadata and lyrics.

**Three-layer design:** `cli` calls `services`, which use `adapters/providers` and `adapters/processors`. Shared models live in `core/`; shared utilities in `shared/`.

```
cli/main.py          → argparse, wires up FileConfig + RunService
services/run_service.py  → top-level pipeline: sync → process
services/sync_service.py → diff playlist vs local state, download new tracks
services/process_service.py → decrypt, route (lossless/lossy), write metadata + lyrics
adapters/providers/pyncm_client.py → pyncm wrapper: login, playlists, URLs, lyrics
adapters/processors/downloader.py   → HTTP download with content-type-based extension detection
adapters/processors/decryptor.py    → .ncm decryption via ncmdump-py
adapters/processors/organizer.py    → ffmpeg routing: lossless → flac/wav/ape + mp3 transcode
adapters/processors/metadata_writer.py → mutagen: ID3 for mp3, Vorbis for flac + cover art
adapters/processors/lyrics.py       → LRC/YRC parsing, translation merging, GB18030 .lrc output
core/models.py   → Track (unified model), DownloadedTrack
core/config.py   → FileConfig (JSON), AppConfig (runtime paths), workers/lyrics sub-configs
shared/utils.py  → safe_filename, load_json/save_json (atomic write via .tmp)
shared/tui_progress.py → Rich-based BatchProgress bar, status spinners
```

**Config priority:** CLI args > `config.json` file > built-in defaults.

**Pipeline flow:** `RunService.run_pipeline()` creates `SyncService` + `ProcessService`. SyncService logs in via cookie, resolves "liked songs" playlist (specialType=5), diffs against `state/synced_tracks.json`, downloads new tracks in parallel. ProcessService decrypts .ncm, routes audio to `library/lossless/` and `library/lossy/`, writes metadata, and outputs GB18030 `.lrc` sidecar for lossy.

**Key design decisions:**
- `FileConfig` represents the JSON file; `AppConfig` is the resolved runtime config with absolute `Path` objects.
- `Track.from_ncm_payload()` sanitizes text (zero-width chars, control chars) and normalizes the varied NetEase API field names.
- Lossless files get full metadata + embedded lyrics + cover art. Lossy files get minimal ID3 tags + external `.lrc` only.
- Lyrics use only NetEase's `tlyric` (translation). Lossless: translation as a separate timestamped line. Lossy: translation prepended inline on the same line.
- State files use atomic write (write to `.tmp`, then replace) to survive interruption.
- The `text_cleaning.enabled` config flag controls recursive string sanitization on API responses.
- This is a personal-use tool ("Vibe Coding" by AI/Codex). Chinese is the primary language for comments and user-facing messages.

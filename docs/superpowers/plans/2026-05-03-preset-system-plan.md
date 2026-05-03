# Preset System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded lossless/lossy dual-output with configurable N-preset system, with audio deduplication by `(format, bitrate)` spec.

**Architecture:** New `Preset` dataclass in `core/preset.py`. Config gains `presets: list[Preset]` and drops 14 lossless/lossy-specific fields. Organizer routes to N canonical files (deduped by spec). ProcessService and SyncService iterate presets instead of branching on `is_lossless`. MetadataWriter drops `is_lossless` — always writes full metadata.

**Tech Stack:** Python 3.12+, dataclasses, mutagen, pytest, unittest.mock

**File Map:**

| File | Action | Responsibility |
|------|--------|----------------|
| `src/musicvault/core/preset.py` | **Create** | `Preset` dataclass + validation + default presets |
| `src/musicvault/core/config.py` | **Modify** | Remove 14 fields, add `presets`, update `from_dict`/`to_dict`/`ensure_dirs`/dir properties |
| `src/musicvault/adapters/processors/organizer.py` | **Modify** | `route_audio()` returns `dict[spec, Path]` instead of `tuple[Path, Path]` |
| `src/musicvault/adapters/processors/metadata_writer.py` | **Modify** | Drop `is_lossless` parameter, always full metadata |
| `src/musicvault/services/process_service.py` | **Modify** | Preset-driven lyrics/metadata/linking |
| `src/musicvault/services/sync_service.py` | **Modify** | Iterate presets for links/rename/prune/reconcile |
| `src/musicvault/services/run_service.py` | **Modify** | Wire presets into service constructors, update rebuild_index, link_only |
| `src/musicvault/cli/main.py` | **Modify** | Remove `--no-translation` (now per-preset) |
| `tests/test_preset_model.py` | **Create** | Preset validation, default presets, dedup helpers |
| `tests/test_preset_organizer.py` | **Create** | Multi-spec routing tests |
| `tests/test_config_model.py` | **Modify** | Update for preset-based config |
| `tests/test_playlist_reconciliation.py` | **Modify** | Update for preset-based linking |

---

### Task 1: Preset Dataclass

**Files:**
- Create: `src/musicvault/core/preset.py`
- Create: `tests/test_preset_model.py`

- [ ] **Step 1: Write failing tests for Preset model**

```python
# tests/test_preset_model.py
from __future__ import annotations

import pytest
from musicvault.core.preset import Preset, validate_presets, default_presets, audio_spec_key, build_audio_specs


class TestPresetDefaults:
    def test_minimal_preset(self):
        p = Preset(name="test")
        assert p.name == "test"
        assert p.quality == "hires"
        assert p.format is None
        assert p.bitrate is None
        assert p.filename_template == "{artist} - {name}"
        assert p.embed_cover is True
        assert p.cover_max_size == 0
        assert p.embed_lyrics is True
        assert p.metadata_fields == ()
        assert p.use_karaoke is False
        assert p.include_translation is True
        assert p.translation_format == "separate"
        assert p.include_romaji is False
        assert p.write_lrc_file is True
        assert p.lrc_encodings == ("utf-8",)


class TestPresetValidation:
    def test_unique_names_pass(self):
        presets = [Preset(name="a"), Preset(name="b")]
        validate_presets(presets)  # no exception

    def test_duplicate_names_raise(self):
        presets = [Preset(name="a"), Preset(name="a")]
        with pytest.raises(ValueError, match="duplicate"):
            validate_presets(presets)

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="at least one preset"):
            validate_presets([])

    def test_invalid_name_characters(self):
        with pytest.raises(ValueError, match="Invalid preset name"):
            Preset(name="my preset")

    def test_invalid_quality(self):
        with pytest.raises(ValueError, match="quality"):
            Preset(name="test", quality="ultra_hd")

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="format"):
            Preset(name="test", format="wma")

    def test_invalid_translation_format(self):
        with pytest.raises(ValueError, match="translation_format"):
            Preset(name="test", translation_format="bad")


class TestAudioSpecKey:
    def test_flac_spec(self):
        assert audio_spec_key("flac", None) == "FLAC"

    def test_mp3_spec(self):
        assert audio_spec_key("mp3", "320k") == "MP3-320k"

    def test_none_format(self):
        assert audio_spec_key(None, None) == "ORIGINAL"

    def test_opus_spec(self):
        assert audio_spec_key("opus", "160k") == "OPUS-160k"


class TestBuildAudioSpecs:
    def test_unique_specs(self):
        presets = [
            Preset(name="a", format="flac"),
            Preset(name="b", format="mp3", bitrate="192k"),
            Preset(name="c", format="mp3", bitrate="192k"),
        ]
        specs = build_audio_specs(presets)
        assert specs == {("flac", None), ("mp3", "192k")}

    def test_none_format_preserved(self):
        presets = [Preset(name="a", format=None)]
        specs = build_audio_specs(presets)
        assert specs == {(None, None)}

    def test_multiple_none_same(self):
        presets = [Preset(name="a", format=None), Preset(name="b", format=None)]
        specs = build_audio_specs(presets)
        assert specs == {(None, None)}


class TestDefaultPresets:
    def test_defaults_have_two_presets(self):
        presets = default_presets()
        assert len(presets) == 2

    def test_archive_preset(self):
        presets = default_presets()
        archive = next(p for p in presets if p.name == "archive")
        assert archive.format == "flac"
        assert archive.use_karaoke is True
        assert archive.translation_format == "separate"
        assert archive.write_lrc_file is False

    def test_portable_preset(self):
        presets = default_presets()
        portable = next(p for p in presets if p.name == "portable")
        assert portable.format == "mp3"
        assert portable.bitrate == "192k"
        assert portable.use_karaoke is False
        assert portable.translation_format == "inline"
        assert portable.write_lrc_file is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_preset_model.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'musicvault.core.preset'`

- [ ] **Step 3: Implement Preset dataclass and helpers**

```python
# src/musicvault/core/preset.py
from __future__ import annotations

import re
from dataclasses import dataclass, field

_VALID_QUALITIES = frozenset({"standard", "higher", "exhigh", "hires", "lossless"})
_VALID_FORMATS = frozenset({"flac", "mp3", "aac", "ogg", "opus"})
_VALID_TRANSLATION_FORMATS = frozenset({"separate", "inline", "notimestamp"})
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


@dataclass(slots=True)
class Preset:
    name: str
    quality: str = "hires"
    format: str | None = None
    bitrate: str | None = None
    filename_template: str = "{artist} - {name}"
    embed_cover: bool = True
    cover_max_size: int = 0
    embed_lyrics: bool = True
    metadata_fields: tuple[str, ...] = ()
    use_karaoke: bool = False
    include_translation: bool = True
    translation_format: str = "separate"
    include_romaji: bool = False
    write_lrc_file: bool = True
    lrc_encodings: tuple[str, ...] = ("utf-8",)

    def __post_init__(self) -> None:
        if not _VALID_NAME_RE.match(self.name):
            raise ValueError(
                f"Invalid preset name '{self.name}': must start with letter/digit, "
                f"contain only letters, digits, underscores, hyphens"
            )
        if self.quality not in _VALID_QUALITIES:
            raise ValueError(
                f"preset '{self.name}': quality must be one of {sorted(_VALID_QUALITIES)}, got '{self.quality}'"
            )
        if self.format is not None and self.format not in _VALID_FORMATS:
            raise ValueError(
                f"preset '{self.name}': format must be one of {sorted(_VALID_FORMATS)}, got '{self.format}'"
            )
        if self.translation_format not in _VALID_TRANSLATION_FORMATS:
            raise ValueError(
                f"preset '{self.name}': translation_format must be one of "
                f"{sorted(_VALID_TRANSLATION_FORMATS)}, got '{self.translation_format}'"
            )

    @property
    def audio_spec(self) -> tuple[str | None, str | None]:
        return (self.format, self.bitrate)


def audio_spec_key(fmt: str | None, bitrate: str | None) -> str:
    if fmt is None:
        return "ORIGINAL"
    fmt_upper = fmt.upper()
    if bitrate:
        return f"{fmt_upper}-{bitrate}"
    return fmt_upper


def build_audio_specs(presets: list[Preset]) -> set[tuple[str | None, str | None]]:
    return {p.audio_spec for p in presets}


def validate_presets(presets: list[Preset]) -> None:
    if not presets:
        raise ValueError("Config must have at least one preset")
    seen: set[str] = set()
    for p in presets:
        if p.name in seen:
            raise ValueError(f"Duplicate preset name: '{p.name}'")
        seen.add(p.name)


def default_presets() -> list[Preset]:
    return [
        Preset(
            name="archive",
            quality="hires",
            format="flac",
            filename_template="{artist} - {name}",
            embed_cover=True,
            embed_lyrics=True,
            use_karaoke=True,
            include_translation=True,
            translation_format="separate",
            write_lrc_file=False,
        ),
        Preset(
            name="portable",
            quality="hires",
            format="mp3",
            bitrate="192k",
            filename_template="{alias} {name} - {artist}",
            embed_cover=False,
            embed_lyrics=False,
            use_karaoke=False,
            include_translation=True,
            translation_format="inline",
            write_lrc_file=True,
            lrc_encodings=("utf-8",),
        ),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_preset_model.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/musicvault/core/preset.py tests/test_preset_model.py
git commit -m "feat: add Preset dataclass with validation and audio spec dedup helpers
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Config — Remove Old Fields, Add Presets, Update Serialization

**Files:**
- Modify: `src/musicvault/core/config.py` (full rewrite of fields, from_dict, to_dict, ensure_dirs, dir properties)
- Modify: `tests/test_config_model.py` (update all old tests)

- [ ] **Step 1: Write updated config tests**

```python
# tests/test_config_model.py
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from musicvault.core.config import Config
from musicvault.core.preset import Preset


def test_load_creates_default_file() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        assert path.exists()
        assert cfg.workspace == "./workspace"
        assert len(cfg.presets) == 2
        assert cfg.presets[0].name == "archive"
        assert cfg.presets[1].name == "portable"


def test_load_with_presets() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "cookie": "abc",
                    "workspace": "./ws",
                    "presets": [
                        {
                            "name": "archive",
                            "quality": "hires",
                            "format": "flac",
                            "filename_template": "{artist} - {name}",
                            "embed_cover": True,
                            "use_karaoke": True,
                            "translation_format": "separate",
                            "write_lrc_file": False,
                        },
                        {
                            "name": "portable",
                            "quality": "hires",
                            "format": "mp3",
                            "bitrate": "192k",
                            "filename_template": "{alias} {name} - {artist}",
                            "embed_cover": False,
                            "write_lrc_file": True,
                            "lrc_encodings": ["utf-8", "gb18030"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.cookie == "abc"
        assert cfg.workspace == "./ws"
        assert len(cfg.presets) == 2
        assert cfg.presets[0].name == "archive"
        assert cfg.presets[0].format == "flac"
        assert cfg.presets[0].use_karaoke is True
        assert cfg.presets[1].name == "portable"
        assert cfg.presets[1].format == "mp3"
        assert cfg.presets[1].bitrate == "192k"
        assert cfg.presets[1].lrc_encodings == ("utf-8", "gb18030")


def test_roundtrip_presets() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        cfg.cookie = "xyz"
        cfg.presets[0].format = "flac"
        cfg.save()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["cookie"] == "xyz"
        assert len(loaded["presets"]) == 2
        assert loaded["presets"][0]["name"] == "archive"
        assert loaded["presets"][1]["name"] == "portable"


def test_old_format_raises() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({
                "lossy": {"bitrate": "192k", "format": "mp3"},
                "filenames": {"lossless": "{artist} - {name}", "lossy": "{name}"},
            }),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="presets"):
            Config.load(path)


def test_ensure_dirs_creates_preset_dirs() -> None:
    with TemporaryDirectory() as tmp:
        cfg = Config(workspace=tmp, presets=[
            Preset(name="archive"),
            Preset(name="portable"),
        ])
        cfg.ensure_dirs()
        assert (Path(tmp) / "library" / "archive").is_dir()
        assert (Path(tmp) / "library" / "portable").is_dir()
        assert (Path(tmp) / "downloads").is_dir()
        assert (Path(tmp) / "state").is_dir()


def test_preset_dir_property() -> None:
    cfg = Config(workspace="./ws", presets=[Preset(name="archive")])
    assert cfg.preset_dir("archive") == Path("./ws").resolve() / "library" / "archive"


def test_all_global_fields_retained() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({
                "workers": {"download": 3, "process": 2, "ffmpeg_threads": 4},
                "network": {"download_timeout": 60},
                "process": {"keep_downloads": True},
                "playlist": {"default_name": "其他"},
                "ffmpeg": {"path": "/usr/bin/ffmpeg"},
                "api": {"download_url_chunk_size": 100},
                "alias": {"split_separators": "|"},
                "presets": [{"name": "test"}],
            }),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.download_workers == 3
        assert cfg.process_workers == 2
        assert cfg.ffmpeg_threads == 4
        assert cfg.network_download_timeout == 60
        assert cfg.keep_downloads is True
        assert cfg.default_playlist_name == "其他"
        assert cfg.ffmpeg_path == "/usr/bin/ffmpeg"
        assert cfg.api_download_url_chunk_size == 100
        assert cfg.alias_split_separators == "|"


class TestSongManagement:
    def test_add_and_get_songs(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.ensure_dirs()
            assert cfg.get_song_ids() == []

            cfg.add_song(123)
            cfg.add_song(456)
            assert cfg.get_song_ids() == [123, 456]

    def test_add_duplicate(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(100)
            cfg.add_song(100)
            assert cfg.get_song_ids() == [100]

    def test_has_song(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(42)
            assert cfg.has_song(42) is True
            assert cfg.has_song(99) is False

    def test_remove_song(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(1)
            cfg.add_song(2)
            cfg.remove_song(1)
            assert cfg.get_song_ids() == [2]

    def test_remove_last_song_deletes_file(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(1)
            assert cfg._songs_path.exists()
            cfg.remove_song(1)
            assert not cfg._songs_path.exists()

    def test_songs_survive_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg1 = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg1.add_song(10)
            cfg1.add_song(20)

            cfg2 = Config(workspace=str(ws), presets=[Preset(name="archive")])
            assert cfg2.get_song_ids() == [10, 20]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_config_model.py -v
```
Expected: FAIL — old config tests reference removed fields. New tests fail on missing `presets` parameter.

- [ ] **Step 3: Rewrite Config dataclass**

```python
# src/musicvault/core/config.py
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from musicvault.core.preset import Preset, default_presets, validate_presets
from musicvault.shared.utils import load_json, save_json

_DOWNLOAD_QUALITY_VALUES = frozenset({"standard", "higher", "exhigh", "hires", "lossless"})
_METADATA_FIELD_NAMES = frozenset(
    {"year", "track_number", "disc_number", "genre", "album_artist", "composer", "lyricist", "comment"}
)
_VALID_QUALITY_ORDER = {"standard": 0, "higher": 1, "exhigh": 2, "hires": 3, "lossless": 4}


@dataclass(slots=True)
class Config:
    cookie: str = ""
    workspace: str = "./workspace"
    force: bool = False
    text_cleaning_enabled: bool = True
    download_workers: int | None = None
    process_workers: int | None = None
    ffmpeg_threads: int | None = None
    download_quality: str = "hires"  # 从 presets 自动推导最高值
    network_download_timeout: int = 30
    network_api_timeout: int = 15
    network_cover_timeout: int = 15
    network_max_retries: int = 3
    text_cleaning_allowlist: str = ""
    metadata_fields: tuple[str, ...] = ()
    keep_downloads: bool = False
    default_playlist_name: str = "未分类"
    ffmpeg_path: str = ""
    api_download_url_chunk_size: int = 200
    api_track_detail_chunk_size: int = 500
    alias_split_separators: str = "/、;；"
    presets: list[Preset] = field(default_factory=default_presets)
    _file: Path | None = field(default=None, init=False, repr=False)

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).resolve()

    @property
    def downloads_dir(self) -> Path:
        return self.workspace_path / "downloads"

    @property
    def downloads_cache_dir(self) -> Path:
        return self.downloads_dir / "cache"

    @property
    def state_dir(self) -> Path:
        return self.workspace_path / "state"

    @property
    def library_dir(self) -> Path:
        return self.workspace_path / "library"

    def preset_dir(self, preset_name: str) -> Path:
        return self.library_dir / preset_name

    @property
    def synced_state_file(self) -> Path:
        return self.state_dir / "synced_tracks.json"

    @property
    def processed_state_file(self) -> Path:
        return self.state_dir / "processed_files.json"

    def ensure_dirs(self) -> None:
        for path in (
            self.workspace_path,
            self.downloads_dir,
            self.downloads_cache_dir,
            self.state_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        for preset in self.presets:
            self.preset_dir(preset.name).mkdir(parents=True, exist_ok=True)

    # -- song management (unchanged) --
    @property
    def _songs_path(self) -> Path:
        return self.state_dir / "songs.json"

    def get_song_ids(self) -> list[int]:
        data = load_json(self._songs_path, {})
        ids = data.get("ids", [])
        return sorted(int(x) for x in ids if isinstance(x, (int, str)))

    def has_song(self, song_id: int) -> bool:
        return song_id in self.get_song_ids()

    def add_song(self, song_id: int) -> None:
        self.ensure_dirs()
        ids = set(self.get_song_ids())
        ids.add(song_id)
        save_json(self._songs_path, {"ids": sorted(ids)})

    def remove_song(self, song_id: int) -> None:
        ids = set(self.get_song_ids())
        ids.discard(song_id)
        if ids:
            save_json(self._songs_path, {"ids": sorted(ids)})
        elif self._songs_path.exists():
            self._songs_path.unlink()

    @property
    def _playlist_index_path(self) -> Path:
        return self.state_dir / "playlists.json"

    def get_playlist_ids(self) -> list[int]:
        index = load_json(self._playlist_index_path, {})
        return sorted(int(k) for k in index if k.lstrip("-").isdigit())

    def has_playlist(self, pid: int) -> bool:
        index = load_json(self._playlist_index_path, {})
        return str(pid) in index

    def add_playlist(self, pid: int, name: str = "", track_count: int = 0) -> None:
        self.ensure_dirs()
        index = load_json(self._playlist_index_path, {})
        index[str(pid)] = {"name": name, "track_count": track_count}
        save_json(self._playlist_index_path, index)

    def remove_playlist(self, pid: int) -> None:
        index = load_json(self._playlist_index_path, {})
        index.pop(str(pid), None)
        save_json(self._playlist_index_path, index)

    # -- serialization --

    @classmethod
    def from_dict(cls, raw: Any) -> Config:
        if not isinstance(raw, dict):
            raise RuntimeError("配置文件格式错误（需为 JSON 对象）")

        # 检测旧格式
        _check_legacy_format(raw)

        workers = raw.get("workers") or {}
        if not isinstance(workers, dict):
            workers = {}

        network = raw.get("network") or {}
        if not isinstance(network, dict):
            network = {}

        text_cleaning = raw.get("text_cleaning") or {}
        if not isinstance(text_cleaning, dict):
            text_cleaning = {}

        metadata_cfg = raw.get("metadata") or {}
        if not isinstance(metadata_cfg, dict):
            metadata_cfg = {}

        process = raw.get("process") or {}
        if not isinstance(process, dict):
            process = {}

        playlist_cfg = raw.get("playlist") or {}
        if not isinstance(playlist_cfg, dict):
            playlist_cfg = {}

        ffmpeg_cfg = raw.get("ffmpeg") or {}
        if not isinstance(ffmpeg_cfg, dict):
            ffmpeg_cfg = {}

        api_cfg = raw.get("api") or {}
        if not isinstance(api_cfg, dict):
            api_cfg = {}

        alias_cfg = raw.get("alias") or {}
        if not isinstance(alias_cfg, dict):
            alias_cfg = {}

        # Parse presets
        presets_raw = raw.get("presets")
        if isinstance(presets_raw, list):
            presets = [Preset(**_normalize_preset_dict(p)) for p in presets_raw]
        else:
            presets = default_presets()
        validate_presets(presets)

        # Derive download_quality from presets (max)
        quality_order = {"standard": 0, "higher": 1, "exhigh": 2, "hires": 3, "lossless": 4}
        max_q = "hires"
        max_val = quality_order["hires"]
        for p in presets:
            qv = quality_order.get(p.quality, 0)
            if qv > max_val:
                max_val = qv
                max_q = p.quality
        download_quality = max_q

        # metadata fields
        metadata_fields_raw = metadata_cfg.get("fields")
        if metadata_fields_raw is None:
            metadata_fields: tuple[str, ...] = ()
        else:
            if not isinstance(metadata_fields_raw, list):
                raise RuntimeError("metadata.fields 格式错误：需为字符串数组或 null")
            metadata_fields = tuple(
                str(f).strip() for f in metadata_fields_raw if str(f).strip() in _METADATA_FIELD_NAMES
            )

        return cls(
            cookie=str(raw.get("cookie") or "").strip(),
            workspace=str(raw.get("workspace") or "./workspace"),
            text_cleaning_enabled=bool(text_cleaning.get("enabled", True)),
            download_workers=_parse_workers_int(workers.get("download")),
            process_workers=_parse_workers_int(workers.get("process")),
            ffmpeg_threads=_parse_workers_int(workers.get("ffmpeg_threads")),
            download_quality=download_quality,
            network_download_timeout=max(5, _parse_positive_int(network.get("download_timeout"), 30)),
            network_api_timeout=max(5, _parse_positive_int(network.get("api_timeout"), 15)),
            network_cover_timeout=max(5, _parse_positive_int(network.get("cover_timeout"), 15)),
            network_max_retries=max(0, min(10, _parse_positive_int(network.get("max_retries"), 3))),
            text_cleaning_allowlist=str(text_cleaning.get("allowlist", "")).strip(),
            metadata_fields=metadata_fields,
            keep_downloads=bool(process.get("keep_downloads", False)),
            default_playlist_name=str(playlist_cfg.get("default_name") or "未分类").strip() or "未分类",
            ffmpeg_path=str(ffmpeg_cfg.get("path") or "").strip(),
            api_download_url_chunk_size=max(50, _parse_positive_int(api_cfg.get("download_url_chunk_size"), 200)),
            api_track_detail_chunk_size=max(50, _parse_positive_int(api_cfg.get("track_detail_chunk_size"), 500)),
            alias_split_separators=str(alias_cfg.get("split_separators") or "/、;；"),
            presets=presets,
        )

    @classmethod
    def load(cls, file: str | Path) -> Config:
        path = Path(file)
        if path.exists():
            raw = load_json(path, {})
            cfg = cls.from_dict(raw)
            if "playlist_ids" in raw or "playlist_id" in raw:
                legacy_ids = _extract_legacy_playlist_ids(raw)
                if legacy_ids:
                    cfg.ensure_dirs()
                    index = load_json(cfg._playlist_index_path, {})
                    for pid in legacy_ids:
                        index.setdefault(str(pid), {"name": "", "track_count": 0})
                    save_json(cfg._playlist_index_path, index)
            cfg.save(path)
        else:
            cfg = cls()
            cfg.save(path)
        cfg._file = path
        return cfg

    def save(self, file: str | Path | None = None) -> None:
        path = Path(file) if file is not None else self._file
        if path is None:
            raise RuntimeError("配置文件路径为空，无法保存")
        save_json(path, self.to_dict(), indent=2)
        self._file = path

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookie": self.cookie,
            "workspace": self.workspace,
            "presets": [
                {
                    "name": p.name,
                    "quality": p.quality,
                    "format": p.format,
                    "bitrate": p.bitrate,
                    "filename_template": p.filename_template,
                    "embed_cover": p.embed_cover,
                    "cover_max_size": p.cover_max_size if p.cover_max_size else None,
                    "embed_lyrics": p.embed_lyrics,
                    "metadata_fields": list(p.metadata_fields) if p.metadata_fields else None,
                    "use_karaoke": p.use_karaoke,
                    "include_translation": p.include_translation,
                    "translation_format": p.translation_format,
                    "include_romaji": p.include_romaji,
                    "write_lrc_file": p.write_lrc_file,
                    "lrc_encodings": list(p.lrc_encodings),
                }
                for p in self.presets
            ],
            "text_cleaning": {
                "enabled": self.text_cleaning_enabled,
                "allowlist": self.text_cleaning_allowlist,
            },
            "workers": {
                "download": self.download_workers,
                "process": self.process_workers,
                "ffmpeg_threads": self.ffmpeg_threads,
            },
            "network": {
                "download_timeout": self.network_download_timeout,
                "api_timeout": self.network_api_timeout,
                "cover_timeout": self.network_cover_timeout,
                "max_retries": self.network_max_retries,
            },
            "metadata": {
                "fields": list(self.metadata_fields) if self.metadata_fields else None,
            },
            "process": {
                "keep_downloads": self.keep_downloads,
            },
            "playlist": {
                "default_name": self.default_playlist_name,
            },
            "ffmpeg": {
                "path": self.ffmpeg_path,
            },
            "api": {
                "download_url_chunk_size": self.api_download_url_chunk_size,
                "track_detail_chunk_size": self.api_track_detail_chunk_size,
            },
            "alias": {
                "split_separators": self.alias_split_separators,
            },
        }

    def build_alias_split_re(self) -> re.Pattern[str]:
        sanitized = re.escape(self.alias_split_separators)
        return re.compile(rf"[{sanitized}]+")


# -- helpers --

def _check_legacy_format(raw: dict[str, Any]) -> None:
    legacy_keys = {"lossy", "filenames", "cover", "lyrics"}
    found = legacy_keys & set(raw.keys())
    if found:
        raise RuntimeError(
            f"旧版配置格式已不再支持。请手动迁移到 preset 格式。"
            f"检测到旧字段：{sorted(found)}。参见文档。"
        )


def _normalize_preset_dict(d: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in d.items():
        result[k] = v

    # lrc_encodings: list -> tuple
    if "lrc_encodings" in result:
        enc = result["lrc_encodings"]
        if isinstance(enc, list):
            result["lrc_encodings"] = tuple(str(e).strip() for e in enc if str(e).strip())
            if not result["lrc_encodings"]:
                result["lrc_encodings"] = ("utf-8",)

    # metadata_fields: list/null -> tuple
    if "metadata_fields" in result:
        mf = result["metadata_fields"]
        if isinstance(mf, list):
            result["metadata_fields"] = tuple(str(f).strip() for f in mf if str(f).strip())
        elif mf is None:
            result["metadata_fields"] = ()
        else:
            result["metadata_fields"] = ()

    # Remove None-valued optional fields from top-level so Preset defaults apply
    for key in ("format", "bitrate"):
        if result.get(key) is None:
            del result[key]

    return result


def _extract_legacy_playlist_ids(raw: dict[str, Any]) -> list[int]:
    playlist_ids = raw.get("playlist_ids") or raw.get("playlist_id")
    if playlist_ids is None:
        return []
    if isinstance(playlist_ids, int):
        return [playlist_ids]
    if isinstance(playlist_ids, list):
        parsed: list[int] = []
        for pid in playlist_ids:
            try:
                parsed.append(int(pid))
            except (TypeError, ValueError):
                raise RuntimeError(f"playlist_ids 格式错误：{pid}") from None
        return parsed
    raise RuntimeError(f"playlist_ids 格式错误：{playlist_ids}")


def _parse_workers_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"workers 值格式错误：{value}") from None
    if parsed <= 0:
        raise RuntimeError(f"workers 值必须大于 0：{value}")
    return parsed


def _parse_positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_config_model.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/musicvault/core/config.py tests/test_config_model.py
git commit -m "feat: add presets to Config, remove legacy lossless/lossy fields
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Organizer — Multi-Spec Routing

**Files:**
- Modify: `src/musicvault/adapters/processors/organizer.py`
- Create: `tests/test_preset_organizer.py`

- [ ] **Step 1: Write failing organizer tests**

```python
# tests/test_preset_organizer.py
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from musicvault.adapters.processors.organizer import Organizer
from musicvault.core.models import Track


def _make_track(track_id: int) -> Track:
    return Track(id=track_id, name="Test", artists=["A"], album="B", cover_url=None, raw={})


class TestRouteAudioSingleSpec:
    def test_flac_source_to_flac(self):
        """FLAC source → single FLAC spec → one canonical file."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.flac"
            src.write_bytes(b"fake-flac-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(src, _make_track(1), output, {("flac", None)})

            assert len(result) == 1
            assert ("flac", None) in result
            assert result[("flac", None)].name == "1.flac"

    def test_mp3_source_no_transcode(self):
        """MP3 source, format=None → copy original."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.mp3"
            src.write_bytes(b"fake-mp3-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(src, _make_track(2), output, {(None, None)})

            assert len(result) == 1
            assert result[(None, None)].name == "2.mp3"

    def test_spec_filename_with_bitrate(self):
        """Multiple mp3 specs → all get bitrate suffix."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.flac"
            src.write_bytes(b"fake-flac-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(
                src, _make_track(3), output,
                {("mp3", "320k"), ("mp3", "192k"), ("mp3", "128k")}
            )

            assert ("mp3", "320k") in result
            assert ("mp3", "192k") in result
            assert ("mp3", "128k") in result
            assert result[("mp3", "320k")].name == "3_320k.mp3"
            assert result[("mp3", "192k")].name == "3_192k.mp3"
            assert result[("mp3", "128k")].name == "3_128k.mp3"

    def test_mixed_specs(self):
        """FLAC + MP3 specs from one source."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.flac"
            src.write_bytes(b"fake-flac-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(
                src, _make_track(4), output,
                {("flac", None), ("mp3", "192k")}
            )

            assert len(result) == 2
            assert result[("flac", None)].name == "4.flac"
            assert result[("mp3", "192k")].name == "4.mp3"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_preset_organizer.py -v
```
Expected: FAIL — `route_audio()` has old signature `(src, track, output_dir) -> tuple[Path, Path]`

- [ ] **Step 3: Rewrite Organizer.route_audio()**

```python
# src/musicvault/adapters/processors/organizer.py
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from musicvault.core.models import Track
from musicvault.shared.output import warn as output_warn

_LOSSY_SUFFIX_MAP = {"mp3": ".mp3", "aac": ".m4a", "ogg": ".ogg", "opus": ".opus"}
_LOSSY_CODEC_MAP = {"mp3": "libmp3lame", "aac": "aac", "ogg": "libvorbis", "opus": "libopus"}


class Organizer:
    def __init__(
        self,
        ffmpeg_threads: int = 1,
        ffmpeg_path: str = "",
    ) -> None:
        self.ffmpeg_threads = max(1, ffmpeg_threads)
        self._ffmpeg_path = ffmpeg_path.strip() or shutil.which("ffmpeg")
        if self._ffmpeg_path is None:
            output_warn("未检测到 ffmpeg，转码功能将不可用")

    def route_audio(
        self,
        src: Path,
        track: Track,
        output_dir: Path,
        audio_specs: set[tuple[str | None, str | None]],
    ) -> dict[tuple[str | None, str | None], Path]:
        """路由音频源文件到 N 个 canonical 文件（按规格去重）。

        返回 {spec: canonical_path}。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = src.suffix.lower()
        result: dict[tuple[str | None, str | None], Path] = {}

        # 检查是否需要多后缀
        same_format_counts = _count_same_formats(audio_specs)

        for fmt, bitrate in audio_specs:
            ext = _format_to_ext(fmt, src.suffix)
            filename = _spec_to_filename(track.id, fmt, bitrate, same_format_counts.get(fmt, 0))
            target = output_dir / filename

            if target.exists():
                result[(fmt, bitrate)] = target
                continue

            if fmt is None:
                # 保持源格式，复制或转码到自身
                if suffix == ext:
                    _copy(src, target)
                else:
                    # 源格式和目标不同（如 FLAC 源但 preset 要原样）
                    _copy(src, target)
            elif self._is_lossless_suffix(suffix) and fmt == "flac":
                if suffix == ".flac":
                    _copy(src, target)
                else:
                    self._transcode_to_flac(src, target)
            elif self._is_lossless_suffix(suffix) and fmt != "flac":
                self._transcode_lossy(src, target, fmt, bitrate or "192k")
            elif not self._is_lossless_suffix(suffix):
                # 源是有损，转码到目标格式
                self._transcode_lossy(src, target, fmt, bitrate or "192k")
            else:
                self._transcode_lossy(src, target, fmt, bitrate or "192k")

            result[(fmt, bitrate)] = target

        return result

    def _transcode_to_flac(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self._ffmpeg_path:
            raise RuntimeError(f"转码失败：未找到 ffmpeg，文件={src.name}")
        cmd = [
            self._ffmpeg_path, "-y", "-threads", str(self.ffmpeg_threads),
            "-i", str(src), "-codec:a", "flac", str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")

    def _transcode_lossy(self, src: Path, dst: Path, fmt: str, bitrate: str) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self._ffmpeg_path:
            raise RuntimeError(f"转码失败：未找到 ffmpeg，文件={src.name}")
        codec = _LOSSY_CODEC_MAP.get(fmt, "libmp3lame")
        cmd = [
            self._ffmpeg_path, "-y", "-threads", str(self.ffmpeg_threads),
            "-i", str(src), "-codec:a", codec, "-b:a", bitrate, str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg 转码失败：文件={src}，错误={stderr}")

    @staticmethod
    def _is_lossless_suffix(suffix: str) -> bool:
        return suffix in {".flac", ".wav", ".ape"}


def _format_to_ext(fmt: str | None, source_suffix: str) -> str:
    if fmt is None:
        return source_suffix
    return _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt}")


def _spec_to_filename(track_id: int, fmt: str | None, bitrate: str | None, same_format_count: int) -> str:
    if fmt is None:
        return f"{track_id}{_LOSSY_SUFFIX_MAP.get('mp3', '.mp3')}"  # actual ext resolved later
    ext = _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt}")
    if same_format_count > 1 and bitrate:
        return f"{track_id}_{bitrate}{ext}"
    return f"{track_id}{ext}"


def _count_same_formats(specs: set[tuple[str | None, str | None]]) -> dict[str | None, int]:
    counts: dict[str | None, int] = {}
    for fmt, _ in specs:
        counts[fmt] = counts.get(fmt, 0) + 1
    return counts


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_preset_organizer.py -v
```
Expected: PASS (Note: transcoding tests will pass because `_copy` is used for same-format; transcode will fail without ffmpeg, but we avoid calling transcode in these tests)

- [ ] **Step 5: Commit**

```bash
git add src/musicvault/adapters/processors/organizer.py tests/test_preset_organizer.py
git commit -m "feat: multi-spec route_audio() in Organizer
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: MetadataWriter — Remove is_lossless

**Files:**
- Modify: `src/musicvault/adapters/processors/metadata_writer.py`

- [ ] **Step 1: Rewrite MetadataWriter — accept per-call params, no is_lossless**

The new API: caller (ProcessService) passes all decisions as parameters. MetadataWriter has no policy — it just executes. Cover download still internal with cache.

Replace the entire file:

```python
# src/musicvault/adapters/processors/metadata_writer.py
from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image

from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.id3 import (
    APIC,
    COMM,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TEXT,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    USLT,
)
from mutagen.mp3 import MP3

from musicvault.core.models import Track


class MetadataWriter:
    def __init__(self) -> None:
        self._cover_cache: dict[str, bytes] = {}
        self._cover_cache_lock = threading.Lock()

    def write(
        self,
        audio_file: Path,
        track: Track,
        *,
        lyric_text: str | None = None,
        embed_cover: bool = True,
        embed_lyrics: bool = True,
        cover_timeout: int = 15,
        cover_max_size: int = 0,
        metadata_fields: frozenset[str] = frozenset(),
    ) -> None:
        """写入元数据到音频文件。policy 由 caller 决定。"""
        cover_data: bytes | None = None
        if embed_cover:
            cover_data = self._download_cover(track.cover_url, cover_timeout, cover_max_size)

        if audio_file.suffix.lower() == ".mp3":
            self._write_mp3(audio_file, track, lyric_text, cover_data, embed_lyrics, metadata_fields)
        elif audio_file.suffix.lower() == ".flac":
            self._write_flac(audio_file, track, lyric_text, cover_data, embed_lyrics, metadata_fields)

    # ------------------------------------------------------------------
    # MP3
    # ------------------------------------------------------------------

    def _write_mp3(
        self, path: Path, track: Track, lyric_text: str | None,
        cover_data: bytes | None, embed_lyrics: bool, metadata_fields: frozenset[str],
    ) -> None:
        audio = MP3(str(path))
        tags = audio.tags or ID3()
        tags.clear()

        self._set_id3_text(tags, "TIT2", TIT2, track.name)
        self._set_id3_text(tags, "TPE1", TPE1, track.artist_text)
        self._set_id3_text(tags, "TALB", TALB, track.album)

        extras = self._build_extra_metadata(track, metadata_fields)
        self._set_id3_text(tags, "TDRC", TDRC, extras.get("year"))
        self._set_id3_text(tags, "TRCK", TRCK, extras.get("track_number"))
        self._set_id3_text(tags, "TPOS", TPOS, extras.get("disc_number"))
        self._set_id3_text(tags, "TCON", TCON, extras.get("genre"))
        self._set_id3_text(tags, "TPE2", TPE2, extras.get("album_artist"))
        self._set_id3_text(tags, "TCOM", TCOM, extras.get("composer"))
        self._set_id3_text(tags, "TEXT", TEXT, extras.get("lyricist"))
        self._set_id3_comment(tags, extras.get("comment"))

        if embed_lyrics and lyric_text:
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="", text=lyric_text))

        if cover_data:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))

        audio.tags = tags
        audio.save(v2_version=3)

    # ------------------------------------------------------------------
    # FLAC
    # ------------------------------------------------------------------

    def _write_flac(
        self, path: Path, track: Track, lyric_text: str | None,
        cover_data: bytes | None, embed_lyrics: bool, metadata_fields: frozenset[str],
    ) -> None:
        audio = FLAC(str(path))
        audio["title"] = track.name
        audio["artist"] = track.artist_text
        audio["album"] = track.album

        extras = self._build_extra_metadata(track, metadata_fields)
        self._set_vorbis_text(audio, "date", extras.get("year"))
        self._set_vorbis_text(audio, "tracknumber", extras.get("track_number"))
        self._set_vorbis_text(audio, "discnumber", extras.get("disc_number"))
        self._set_vorbis_text(audio, "genre", extras.get("genre"))
        self._set_vorbis_text(audio, "albumartist", extras.get("album_artist"))
        self._set_vorbis_text(audio, "composer", extras.get("composer"))
        self._set_vorbis_text(audio, "lyricist", extras.get("lyricist"))
        self._set_vorbis_text(audio, "comment", extras.get("comment"))

        if embed_lyrics and lyric_text:
            audio["lyrics"] = lyric_text
            audio["description"] = "Synced by MusicVault"

        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)

        audio.save()

    # ------------------------------------------------------------------
    # Cover download (internal, cached)
    # ------------------------------------------------------------------

    def _download_cover(self, url: str | None, timeout: int, max_size: int) -> bytes | None:
        if not url:
            return None
        with self._cover_cache_lock:
            cached = self._cover_cache.get(url)
        if cached:
            return cached

        data = self._fetch_cover(url, timeout)
        if not data:
            return None

        if max_size > 0:
            data = self._resize_cover(data, max_size)

        with self._cover_cache_lock:
            self._cover_cache[url] = data
        return data

    def _resize_cover(self, data: bytes, max_size: int) -> bytes:
        try:
            img = Image.open(BytesIO(data))
        except Exception:
            return data

        width, height = img.size
        max_dim = max(width, height)
        if max_dim <= max_size:
            return data

        ratio = max_size / max_dim
        new_size = (int(width * ratio), int(height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def _fetch_cover(self, url: str, timeout: int) -> bytes | None:
        headers = {
            "User-Agent": "MusicVault/1.0",
            "Accept": "image/*,*/*;q=0.8",
            "Connection": "close",
        }
        backoff_seconds = (0.0, 0.5, 1.5)
        for sleep_seconds in backoff_seconds:
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            req = Request(url, headers=headers, method="GET")
            try:
                with urlopen(req, timeout=timeout) as resp:  # nosec B310
                    return resp.read()
            except HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                    break
            except (URLError, TimeoutError, socket.timeout, OSError):
                pass
        return None

    # ------------------------------------------------------------------
    # Extra metadata builders (unchanged from original)
    # ------------------------------------------------------------------

    def _build_extra_metadata(self, track: Track, fields: frozenset[str]) -> dict[str, str | None]:
        raw = track.raw or {}
        extras = {
            "year": self._extract_year(raw),
            "track_number": self._extract_track_number(raw.get("no")),
            "disc_number": self._extract_disc(raw.get("cd")),
            "genre": self._extract_genre(raw.get("genre")),
            "album_artist": self._extract_album_artist(raw) or track.artist_text,
            "composer": self._extract_named_people(raw.get("composer")),
            "lyricist": self._extract_named_people(raw.get("lyricist")),
            "comment": self._extract_comment(raw, track),
        }
        if fields:
            return {k: v for k, v in extras.items() if k in fields}
        return extras

    def _extract_year(self, raw: dict[str, object]) -> str | None:
        ts = raw.get("publishTime")
        if not ts:
            return None
        try:
            value = int(str(ts))
            if value > 1_000_000_000_000:
                value //= 1000
            return str(datetime.fromtimestamp(value, tz=timezone.utc).year)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _extract_track_number(no: object) -> str | None:
        if no is None:
            return None
        try:
            value = int(str(no))
        except (TypeError, ValueError):
            return None
        return str(value) if value > 0 else None

    @staticmethod
    def _extract_disc(cd: object) -> str | None:
        if cd is None:
            return None
        if isinstance(cd, int):
            return str(cd) if cd > 0 else None
        text = str(cd).strip()
        if not text:
            return None
        if "/" in text:
            text = text.split("/", 1)[0].strip()
        if text.isdigit():
            return str(int(text))
        return text

    @staticmethod
    def _extract_genre(value: object) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return "/".join(items) if items else None
        return None

    @staticmethod
    def _extract_album_artist(raw: dict[str, object]) -> str | None:
        artists = raw.get("ar")
        if not isinstance(artists, list):
            return None
        names: list[str] = []
        for item in artists:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                stripped = name.strip()
                if stripped:
                    names.append(stripped)
        return "/".join(names) if names else None

    @staticmethod
    def _extract_named_people(value: object) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str):
                text = name.strip()
                return text or None
            return None
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        stripped = name.strip()
                        if stripped:
                            names.append(stripped)
                elif isinstance(item, str):
                    stripped = item.strip()
                    if stripped:
                        names.append(stripped)
            return "/".join(names) if names else None
        return None

    @staticmethod
    def _extract_comment(raw: dict[str, object], track: Track) -> str | None:
        tns = raw.get("tns")
        if isinstance(tns, list):
            names = [str(item).strip() for item in tns if str(item).strip()]
            if names:
                return "/".join(names)

        alia = raw.get("alia")
        if isinstance(alia, list):
            names = [str(item).strip() for item in alia if str(item).strip()]
            if names:
                return "/".join(names)

        if track.aliases:
            return "/".join(alias for alias in track.aliases if alias)
        return None

    # ------------------------------------------------------------------
    # Tag helpers (unchanged)
    # ------------------------------------------------------------------

    @staticmethod
    def _set_id3_text(tags: ID3, frame_id: str, frame_cls: type, value: str | None) -> None:
        tags.delall(frame_id)
        if value:
            tags.add(frame_cls(encoding=3, text=str(value)))

    @staticmethod
    def _set_id3_comment(tags: ID3, comment: str | None) -> None:
        tags.delall("COMM")
        if comment:
            tags.add(COMM(encoding=3, lang="eng", desc="", text=comment))

    @staticmethod
    def _set_vorbis_text(audio: FLAC, key: str, value: str | None) -> None:
        if value:
            audio[key] = str(value)
        elif key in audio:
            del audio[key]
```

- [ ] **Step 2: Verify no import errors**

```bash
python -c "from musicvault.adapters.processors.metadata_writer import MetadataWriter; print('OK')"
```
Expected: OK (or warning about ffmpeg if not installed, which is fine)

- [ ] **Step 3: Commit**

```bash
git add src/musicvault/adapters/processors/metadata_writer.py
git commit -m "refactor: remove is_lossless from MetadataWriter, accept per-call params
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: ProcessService — Preset-Driven Processing

**Files:**
- Modify: `src/musicvault/services/process_service.py`

- [ ] **Step 1: Rewrite ProcessService for preset-driven pipeline**

Key changes:
1. `_process_file()` returns `dict[str, Path]` (spec_key → canonical path) instead of `(lossless, lossy)`
2. Lyrics built per-preset, LRC per-preset
3. Metadata written once per canonical file with merged policy
4. `_link_track()` iterates presets
5. `_mark_processed()` uses new format

```python
# src/musicvault/services/process_service.py
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Mapping

from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.adapters.processors.lyrics import (
    StandardLyrics,
    KaraokeLyrics,
    write_gb18030_lrc,
)
from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.adapters.processors.organizer import Organizer
from musicvault.adapters.providers.pyncm_client import PyncmClient
from musicvault.core.config import Config
from musicvault.core.models import DownloadedTrack, Track
from musicvault.core.preset import Preset, audio_spec_key, build_audio_specs
from musicvault.shared.tui_progress import BatchProgress
from musicvault.shared.utils import (
    create_link,
    format_track_name,
    hardlink_or_copy,
    load_json,
    remove_link,
    safe_filename,
    save_json,
    workspace_rel_path,
)

logger = logging.getLogger(__name__)


class ProcessService:
    def __init__(
        self,
        cfg: Config,
        api: PyncmClient,
        decryptor: Decryptor,
        organizer: Organizer,
        metadata: MetadataWriter,
        workers: int,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.decryptor = decryptor
        self.organizer = organizer
        self.metadata = metadata
        self.workers = max(1, workers)

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def run_process(
        self,
        downloaded: list[DownloadedTrack],
        force: bool,
        playlist_index: dict[str, dict[str, object]] | None = None,
    ) -> None:
        if downloaded:
            playlist_index = playlist_index or {}
            tasks: list[tuple[Path, Track, list[str]]] = []
            for item in downloaded:
                names = self._resolve_playlist_names(item.playlist_ids, playlist_index)
                tasks.append((Path(item.source_file), item.track, names))
            self._run_process_batch(tasks, "处理中", force)
            return
        self._process_local(force)

    # ------------------------------------------------------------------
    # 处理管线
    # ------------------------------------------------------------------

    def _run_process_batch(
        self,
        tasks: list[tuple[Path, Track, list[str]]],
        stage_name: str,
        force: bool,
    ) -> None:
        if not tasks:
            return

        processed_index = self._load_processed_index()
        pending, skipped = self._filter_pending(tasks, processed_index, force=force)
        logger.info("已处理索引过滤：阶段=%s force=%s 跳过=%s 待处理=%s", stage_name, force, skipped, len(pending))
        if not pending:
            return

        total = len(pending)
        workers = min(self.workers, total)
        results: list[tuple[dict[str, Path], Track, list[str]]] = []

        with ThreadPoolExecutor(max_workers=workers) as pool, BatchProgress(total=total, phase=stage_name) as bp:
            future_map = {
                pool.submit(self._process_file, raw_file, track_info): (idx, raw_file)
                for idx, (raw_file, track_info, _names) in enumerate(pending, start=1)
            }

            try:
                for future in as_completed(future_map):
                    idx, raw_file = future_map[future]
                    try:
                        audio_map = future.result()
                        track_info = None
                        playlist_names = None
                        for rf, ti, pn in pending:
                            if rf == raw_file:
                                track_info, playlist_names = ti, pn
                                break
                        self._mark_processed(audio_map, processed_index)
                        if track_info and playlist_names:
                            results.append((audio_map, track_info, playlist_names))
                        bp.advance(success=True, idx=idx, item_name=raw_file.name)
                    except Exception as exc:
                        bp.advance(success=False, idx=idx, item_name=raw_file.name)
                        logger.error("处理失败：阶段=%s #%s %s，原因：%s", stage_name, idx, raw_file.name, exc, exc_info=True)
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                if processed_index:
                    self._save_processed_index(processed_index)
                raise

        self._save_processed_index(processed_index)

        for audio_map, track_info, playlist_names in results:
            self._link_track(audio_map, track_info, playlist_names)

    def _process_file(
        self,
        raw_file: Path,
        prefetched_track: Track | None = None,
    ) -> dict[str, Path]:
        """处理单个文件，返回 {spec_key: canonical_path}。"""
        track_info = prefetched_track
        track_id = prefetched_track.id if prefetched_track else None
        if track_info is None:
            track_id = self._guess_track_id(raw_file)
            if track_id is None:
                raise RuntimeError(f"无法推断 track_id：{raw_file.name}")
            track_info = self._safe_track(track_id, raw_file.stem)

        if track_id is None:
            raise RuntimeError(f"无法推断 track_id：{raw_file.name}")

        # 年份回退
        if not track_info.raw.get("publishTime"):
            al = track_info.raw.get("al") or {}
            album_id = al.get("id")
            if album_id:
                try:
                    import pyncm.apis.album as album_api
                    from musicvault.adapters.providers.pyncm_client import _retry_api
                    alb_resp = _retry_api(album_api.GetAlbumInfo, int(album_id))
                    alb_pt = (alb_resp.get("album") or {}).get("publishTime")
                    if alb_pt:
                        track_info.raw["publishTime"] = alb_pt
                except Exception:
                    pass

        # 判断是否已是 canonical 文件
        is_canonical = (
            raw_file.parent.resolve() == self.cfg.downloads_dir.resolve()
            and raw_file.stem.isdigit()
        )

        # 解密 + 路由
        audio_specs = build_audio_specs(self.cfg.presets)
        if is_canonical:
            # 已有 canonical：根据后缀判断规格，补全缺失的规格
            audio_map: dict[str, Path] = {}
            existing_spec = self._spec_from_canonical(raw_file)
            if existing_spec:
                audio_map[audio_spec_key(*existing_spec)] = raw_file
            # 为其他规格转码
            for spec in audio_specs:
                key = audio_spec_key(*spec)
                if key not in audio_map:
                    # 需要从已有文件转码生成
                    result = self.organizer.route_audio(raw_file, track_info, self.cfg.downloads_dir, {spec})
                    if spec in result:
                        audio_map[key] = result[spec]
        else:
            downloaded = DownloadedTrack(
                track=track_info, source_file=str(raw_file),
                is_ncm=raw_file.suffix.lower() == ".ncm",
            )
            decoded = self.decryptor.decrypt_if_needed(downloaded, self.cfg.workspace_path / "decoded")
            raw_result = self.organizer.route_audio(decoded, track_info, self.cfg.downloads_dir, audio_specs)
            audio_map = {audio_spec_key(fmt, br): p for (fmt, br), p in raw_result.items()}

        # 获取歌词（一次 API 调用）
        lyrics = self.api.get_track_lyrics(track_id)

        # 确定每个 canonical 文件的合并策略
        spec_presets: dict[str, list[Preset]] = {}
        for preset in self.cfg.presets:
            key = audio_spec_key(preset.format, preset.bitrate)
            spec_presets.setdefault(key, []).append(preset)

        # 写元数据（每个 canonical 文件一次，合并策略取并集/最大值）
        for spec_key, canon_path in audio_map.items():
            presets_for_spec = spec_presets.get(spec_key, [])
            embed_cover = any(p.embed_cover for p in presets_for_spec)
            embed_lyrics = any(p.embed_lyrics for p in presets_for_spec)
            cover_max_size = max((p.cover_max_size for p in presets_for_spec), default=0)
            cover_timeout = self.cfg.network_cover_timeout
            mf_union: frozenset[str] = frozenset()
            for p in presets_for_spec:
                mf_union = mf_union | set(p.metadata_fields)

            # 选择最丰富的歌词嵌入
            best_lyric = self._pick_best_lyric(lyrics, presets_for_spec)

            self.metadata.write(
                canon_path, track_info,
                lyric_text=best_lyric if embed_lyrics else None,
                embed_cover=embed_cover,
                embed_lyrics=embed_lyrics,
                cover_timeout=cover_timeout,
                cover_max_size=cover_max_size,
                metadata_fields=mf_union,
            )

        # LRC 文件（按 preset 独立）
        for preset in self.cfg.presets:
            if not preset.write_lrc_file:
                continue
            spec_key = audio_spec_key(preset.format, preset.bitrate)
            canon_path = audio_map.get(spec_key)
            if not canon_path:
                continue
            lyric_text = self._build_lyrics_for_preset(lyrics, preset)
            lrc_path = canon_path.with_name(f"{track_id}.{preset.name}.lrc")
            write_gb18030_lrc_raw(lrc_path, lyric_text, encodings=preset.lrc_encodings)

        # 清理临时文件
        if not is_canonical:
            if not raw_file.suffix.lower() == ".flac" and "decoded" in str(raw_file.parent):
                pass  # decoded files managed by decryptor
            if not self.cfg.keep_downloads:
                if raw_file.exists():
                    # 只删除非 canonical 的原始下载文件
                    raw_file.unlink(missing_ok=True)

        return audio_map

    def _pick_best_lyric(
        self, lyrics: dict[str, str], presets: list[Preset]
    ) -> str | None:
        """选择最丰富的歌词。优先级：karaoke+trans+romaji > karaoke+trans > karaoke > standard+trans+romaji > standard+trans > standard。"""
        if not presets:
            return None

        def score(p: Preset) -> int:
            s = 0
            if p.use_karaoke:
                s += 100
            if p.include_translation:
                s += 10
            if p.include_romaji:
                s += 1
            return s

        best = max(presets, key=score)
        fmt = best.translation_format

        if best.use_karaoke and lyrics.get("yrc"):
            lyr = KaraokeLyrics(lyrics)
        else:
            lyr = StandardLyrics(lyrics)

        if best.include_translation and best.include_romaji:
            return lyr.merge_all(format=fmt)
        if best.include_translation:
            return lyr.merge_translation(format=fmt)
        if best.include_romaji:
            return lyr.merge_romaji(format=fmt)
        return lyr.original

    def _build_lyrics_for_preset(self, lyrics: dict[str, str], preset: Preset) -> str:
        if preset.use_karaoke and lyrics.get("yrc"):
            lyr = KaraokeLyrics(lyrics)
        else:
            lyr = StandardLyrics(lyrics)

        if preset.include_translation and preset.include_romaji:
            return lyr.merge_all(format=preset.translation_format)
        if preset.include_translation:
            return lyr.merge_translation(format=preset.translation_format)
        if preset.include_romaji:
            return lyr.merge_romaji(format=preset.translation_format)
        return lyr.original

    def _spec_from_canonical(self, path: Path) -> tuple[str | None, str | None] | None:
        """从 canonical 文件名推断音频规格。"""
        name = path.stem
        suffix = path.suffix.lower()
        fmt_map = {".flac": "flac", ".mp3": "mp3", ".m4a": "aac", ".ogg": "ogg", ".opus": "opus"}
        fmt = fmt_map.get(suffix)
        if fmt is None:
            return None
        # 检查是否有 bitrate 后缀
        if "_" in name:
            parts = name.split("_", 1)
            if parts[1].rstrip("k").isdigit():
                return (fmt, parts[1])
        return (fmt, None)

    # ------------------------------------------------------------------
    # Library 硬链接
    # ------------------------------------------------------------------

    def _link_track(
        self, audio_map: dict[str, Path], track: Track, playlist_names: list[str],
    ) -> None:
        names = playlist_names or [self.cfg.default_playlist_name]
        for preset in self.cfg.presets:
            spec_key = audio_spec_key(preset.format, preset.bitrate)
            audio_src = audio_map.get(spec_key)
            if not audio_src:
                continue
            link_stem = format_track_name(preset.filename_template, track)
            for pl_name in names:
                dst_dir = self.cfg.preset_dir(preset.name) / pl_name
                dst_dir.mkdir(parents=True, exist_ok=True)
                create_link(audio_src, dst_dir / f"{link_stem}{audio_src.suffix}")
                if preset.write_lrc_file:
                    lrc_src = audio_src.with_name(f"{track.id}.{preset.name}.lrc")
                    if lrc_src.exists():
                        create_link(lrc_src, dst_dir / f"{link_stem}.lrc")

    def _unlink_track(self, track: Track, preset: Preset, playlist_names: list[str]) -> None:
        spec_key = audio_spec_key(preset.format, preset.bitrate)
        for name in playlist_names:
            dst_dir = self.cfg.preset_dir(preset.name) / name
            link_stem = format_track_name(preset.filename_template, track)
            for suffix in (".flac", ".mp3", ".lrc"):
                remove_link(dst_dir / f"{link_stem}{suffix}")

    def _link_name(self, track: Track, preset: Preset, suffix: str = ".flac") -> str:
        return format_track_name(preset.filename_template, track) + suffix

    # ------------------------------------------------------------------
    # processed_files.json
    # ------------------------------------------------------------------

    def _load_processed_index(self) -> dict[str, dict[str, object]]:
        loaded = load_json(self.cfg.processed_state_file, {})
        if not isinstance(loaded, dict):
            return {}
        normalized: dict[str, dict[str, object]] = {}
        for key, value in loaded.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            normalized[key] = dict(value)
        return normalized

    def _save_processed_index(self, index: dict[str, dict[str, object]]) -> None:
        save_json(self.cfg.processed_state_file, index)

    def _filter_pending(
        self,
        tasks: list[tuple[Path, Track, list[str]]],
        processed_index: Mapping[str, Mapping[str, object]],
        force: bool,
    ) -> tuple[list[tuple[Path, Track, list[str]]], int]:
        if force:
            return tasks, 0
        pending: list[tuple[Path, Track, list[str]]] = []
        skipped = 0
        for raw_file, track, playlist_names in tasks:
            if str(track.id) in processed_index:
                skipped += 1
                logger.info("跳过已处理文件：track_id=%s", track.id)
                continue
            pending.append((raw_file, track, playlist_names))
        return pending, skipped

    def _mark_processed(
        self, audio_map: dict[str, Path], processed_index: dict[str, dict[str, object]],
    ) -> None:
        if not audio_map:
            return
        # 从 audio_map 推断 track_id（从第一个文件名）
        first_path = next(iter(audio_map.values()))
        track_id = first_path.stem.split("_")[0]
        audios: dict[str, str] = {}
        for spec_key, p in audio_map.items():
            rel = workspace_rel_path(p, self.cfg.workspace_path)
            audios[spec_key] = rel
        processed_index[track_id] = {
            "audios": audios,
            "updated_at": int(time.time()),
        }

    # ------------------------------------------------------------------
    # 本地处理（msv process 独立模式）
    # ------------------------------------------------------------------

    def _process_local(self, force: bool) -> None:
        pending: list[tuple[Path, int]] = []

        cache_files = [f for f in self._iter_downloads() if not f.stem.isdigit()]
        if cache_files:
            processed = load_json(self.cfg.processed_state_file, {})
            if not isinstance(processed, dict):
                processed = {}
            for raw_file in cache_files:
                track_id = self._guess_track_id(raw_file, index=processed)
                if track_id is None:
                    logger.info("跳过文件：无法推断 track_id，文件=%s", raw_file.name)
                    continue
                pending.append((raw_file, track_id))

        seen_ids = {pid for _, pid in pending}
        for canon_path, track_id in self._scan_canonical_files():
            if track_id not in seen_ids:
                pending.append((canon_path, track_id))
                seen_ids.add(track_id)

        if not pending:
            logger.info("下载目录中无待处理文件")
            return

        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        detail_map = self.api.get_tracks_detail([track_id for _, track_id in pending])
        tasks: list[tuple[Path, Track, list[str]]] = []
        for raw_file, track_id in pending:
            track_info = detail_map.get(track_id) or self._fallback_track(track_id, raw_file.stem)
            pids = self._build_track_playlist_map().get(track_id, [])
            names = self._resolve_playlist_names(pids, playlist_index)
            tasks.append((raw_file, track_info, names))

        self._run_process_batch(tasks, "处理中", force)

    def _build_track_playlist_map(self) -> dict[int, list[int]]:
        mapping: dict[int, list[int]] = {}
        for pid in self.cfg.get_playlist_ids():
            try:
                tracks = self.api.get_playlist_tracks(pid)
            except Exception:
                logger.info("获取歌单曲目失败 playlist_id=%s，跳过分类", pid)
                continue
            for track in tracks:
                mapping.setdefault(track.id, []).append(pid)
        return mapping

    def _iter_downloads(self) -> Iterable[Path]:
        allowed = {".ncm", ".flac", ".mp3", ".m4a", ".aac", ".wav"}
        if not self.cfg.downloads_cache_dir.exists():
            return
        for file_path in self.cfg.downloads_cache_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in allowed:
                yield file_path

    def _scan_canonical_files(self) -> list[tuple[Path, int]]:
        downloads = self.cfg.downloads_dir
        if not downloads.exists():
            return []
        seen: set[int] = set()
        result: list[tuple[Path, int]] = []
        for file_path in sorted(downloads.iterdir()):
            if not file_path.is_file() or file_path.suffix.lower() not in (".flac", ".mp3"):
                continue
            stem = file_path.stem.split("_")[0]
            if not stem.isdigit():
                continue
            track_id = int(stem)
            if track_id in seen:
                continue
            if file_path.suffix.lower() == ".mp3":
                flac_eq = downloads / f"{track_id}.flac"
                mp3_bp = downloads / f"{track_id}_192k.mp3"
                if flac_eq.exists() or mp3_bp.exists():
                    seen.add(track_id)
                    if flac_eq.exists():
                        result.append((flac_eq, track_id))
                    if mp3_bp.exists():
                        result.append((mp3_bp, track_id))
                    continue
            result.append((file_path, track_id))
            seen.add(track_id)
        return result

    def _resolve_playlist_names(
        self, playlist_ids: list[int], playlist_index: Mapping[str, Mapping[str, object]],
    ) -> list[str]:
        names: list[str] = []
        for pid in playlist_ids:
            entry = playlist_index.get(str(pid))
            name = str(entry["name"]) if entry and entry.get("name") else str(pid)
            names.append(safe_filename(name))
        return names or [self.cfg.default_playlist_name]

    def _guess_track_id(self, file_path: Path, index: Mapping[str, object] | None = None) -> int | None:
        index_map: Mapping[str, object]
        if index is None:
            index_path = self.cfg.processed_state_file
            loaded = load_json(index_path, {})
            index_map = loaded if isinstance(loaded, dict) else {}
        else:
            index_map = index

        rel = workspace_rel_path(file_path, self.cfg.workspace_path)
        for key, value in index_map.items():
            if not isinstance(value, dict):
                continue
            audios = value.get("audios")
            if isinstance(audios, dict):
                for spec_key, spec_rel in audios.items():
                    if spec_rel == rel:
                        try:
                            return int(key)
                        except (TypeError, ValueError):
                            pass
            # 兼容旧格式
            for field in ("flac", "mp3", "source"):
                if value.get(field) == rel:
                    try:
                        return int(key)
                    except (TypeError, ValueError):
                        pass
        return None

    def _safe_track(self, track_id: int, fallback_name: str) -> Track:
        detail = self.api.get_track_detail(track_id)
        if detail is not None:
            return detail
        return self._fallback_track(track_id, fallback_name)

    @staticmethod
    def _fallback_track(track_id: int, fallback_name: str) -> Track:
        return Track(id=track_id, name=fallback_name, artists=[], album="Unknown Album", cover_url=None, raw={})


def write_gb18030_lrc_raw(target: Path, lyric_text: str, encodings: tuple[str, ...] = ("utf-8",)) -> Path:
    """写入 LRC 文件（独立版本，不依赖 write_gb18030_lrc 的 audio 推导逻辑）。"""
    content = lyric_text or ""
    fallback_encodings = tuple(e for e in encodings if str(e).strip())
    if not fallback_encodings:
        fallback_encodings = ("utf-8",)
    for encoding in fallback_encodings:
        try:
            target.write_bytes(content.encode(encoding))
            return target
        except UnicodeEncodeError:
            continue
    target.write_bytes(content.encode("utf-8", errors="replace"))
    return target
```

- [ ] **Step 2: Run existing tests to check for breakage**

```bash
python -m pytest tests/ -v --ignore=tests/test_preset_model.py --ignore=tests/test_preset_organizer.py
```
Expected: Some tests may fail due to changed interfaces — this is expected. We'll fix test_playlist_reconciliation in Task 7.

- [ ] **Step 3: Verify import works**

```bash
python -c "from musicvault.services.process_service import ProcessService; print('OK')"
```
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add src/musicvault/services/process_service.py
git commit -m "feat: preset-driven processing in ProcessService
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: SyncService + RunService — Preset-Driven Linking

**Files:**
- Modify: `src/musicvault/services/sync_service.py`
- Modify: `src/musicvault/services/run_service.py`
- Modify: `src/musicvault/cli/main.py`

- [ ] **Step 1: Update SyncService for preset-based linking**

Replace lossless/lossy directory references with preset iteration:

```python
# In sync_service.py — key changes only (full file rewrite)

# _handle_playlist_rename: iterate cfg.presets instead of [lossless_dir, lossy_dir]
def _handle_playlist_rename(self, pid: int, old_name: str, new_name: str, all_tracks: dict[int, Track]) -> None:
    old_safe = safe_filename(old_name)
    new_safe = safe_filename(new_name)
    if old_safe == new_safe:
        return

    for preset in self.cfg.presets:
        old_dir = self.cfg.preset_dir(preset.name) / old_safe
        if old_dir.is_dir():
            shutil.rmtree(old_dir)

    state_map = self._load_synced_state(self.cfg)
    for track_id, pids in state_map.items():
        if pid not in pids:
            continue
        track = all_tracks.get(track_id)
        if track is None:
            continue
        for preset in self.cfg.presets:
            spec_key = audio_spec_key(preset.format, preset.bitrate)
            audio_src = self._find_canonical_for_spec(track_id, spec_key)
            if not audio_src:
                continue
            dst_dir = self.cfg.preset_dir(preset.name) / new_safe
            create_link(audio_src, dst_dir / self._link_name(track, preset, audio_src.suffix))
            lrc_src = self.cfg.downloads_dir / f"{track_id}.{preset.name}.lrc"
            if lrc_src.exists():
                create_link(lrc_src, dst_dir / f"{format_track_name(preset.filename_template, track)}.lrc")

# _create_track_links / _remove_track_links: iterate presets
def _create_track_links(self, audio_map: dict[str, Path], track: Track, dirname: str) -> None:
    for preset in self.cfg.presets:
        spec_key = audio_spec_key(preset.format, preset.bitrate)
        audio_src = audio_map.get(spec_key)
        if not audio_src:
            continue
        dst_dir = self.cfg.preset_dir(preset.name) / dirname
        create_link(audio_src, dst_dir / self._link_name(track, preset, audio_src.suffix))
        lrc_src = self.cfg.downloads_dir / f"{track.id}.{preset.name}.lrc"
        if lrc_src.exists():
            create_link(lrc_src, dst_dir / f"{format_track_name(preset.filename_template, track)}.lrc")

def _remove_track_links(self, track: Track, dirname: str) -> None:
    for preset in self.cfg.presets:
        for suffix in (".flac", ".mp3"):
            remove_link(self.cfg.preset_dir(preset.name) / dirname / self._link_name(track, preset, suffix))
        remove_link(self.cfg.preset_dir(preset.name) / dirname / f"{format_track_name(preset.filename_template, track)}.lrc")

# _prune_stale_tracks: iterate presets
# Change: for parent in (self.cfg.lossless_dir, self.cfg.lossy_dir) →
#         for preset in self.cfg.presets: parent = self.cfg.preset_dir(preset.name)

# New helpers
def _link_name(self, track: Track, preset: Preset, suffix: str = ".flac") -> str:
    return format_track_name(preset.filename_template, track) + suffix

def _find_canonical_for_spec(self, track_id: int, spec_key: str) -> Path | None:
    """查找指定规格的 canonical 文件。"""
    downloads = self.cfg.downloads_dir
    # Try exact match
    for suffix in _SPEC_KEY_SUFFIXES.get(spec_key, []):
        p = downloads / f"{track_id}{suffix}"
        if p.exists():
            return p
    # Try generic mp3/flac for single-spec cases
    for ext in (".flac", ".mp3"):
        p = downloads / f"{track_id}{ext}"
        if p.exists():
            return p
    return None

_SPEC_KEY_SUFFIXES = {
    "FLAC": [".flac"],
    "MP3-320k": ["_320k.mp3", ".mp3"],
    "MP3-192k": ["_192k.mp3", ".mp3"],
    "MP3-128k": ["_128k.mp3"],
    "AAC": [".m4a"],
    "OGG": [".ogg"],
    "OPUS": [".opus"],
}
```

- [ ] **Step 2: Update RunService for preset wiring**

```python
# In run_service.py — key changes:

class RunService:
    def __init__(self, cfg: Config, api: PyncmClient) -> None:
        self.cfg = cfg
        self.api = api

        cpu = os.cpu_count() or 4
        auto_download = max(1, min(6, cpu))
        auto_process = max(1, min(4, cpu // 2))
        auto_ffmpeg = max(1, cpu // auto_process)

        download_workers = cfg.download_workers or auto_download
        process_workers = cfg.process_workers or auto_process
        ffmpeg_threads = cfg.ffmpeg_threads or auto_ffmpeg

        # Downloader uses first preset's filename template (for cache naming)
        first_preset = cfg.presets[0] if cfg.presets else None
        dl_template = first_preset.filename_template if first_preset else "{artist} - {name}"

        self.sync_service = SyncService(
            cfg=cfg, api=api,
            downloader=Downloader(filename_template=dl_template),
            workers=max(1, download_workers),
        )
        self.process_service = ProcessService(
            cfg=cfg, api=api,
            decryptor=Decryptor(),
            organizer=Organizer(
                ffmpeg_threads=max(1, ffmpeg_threads),
                ffmpeg_path=cfg.ffmpeg_path,
            ),
            metadata=MetadataWriter(),
            workers=max(1, process_workers),
        )

    def rebuild_index(self) -> tuple[int, int]:
        self.cfg.ensure_dirs()
        downloads = self.cfg.downloads_dir

        # 1. Scan canonical files
        track_ids: set[int] = set()
        for f in downloads.iterdir():
            if not f.is_file():
                continue
            stem = f.stem.split("_")[0]
            if stem.isdigit() and f.suffix.lower() in (".flac", ".mp3"):
                track_ids.add(int(stem))

        if not track_ids:
            console.print("[dim]downloads 目录中未找到任何 canonical 文件[/dim]")
            return 0, 0

        # 2. Build inode map
        inode_to_tid: dict[tuple[int, int], int] = {}
        for tid in track_ids:
            for f in downloads.iterdir():
                stem = f.stem.split("_")[0]
                if stem.isdigit() and int(stem) == tid and f.suffix.lower() in (".flac", ".mp3"):
                    try:
                        st = f.stat()
                        inode_to_tid[(st.st_dev, st.st_ino)] = tid
                    except OSError:
                        continue

        # 3. Build playlist dirname → pid mapping
        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        dirname_to_pid: dict[str, int] = {}
        for pid_str, entry in playlist_index.items():
            name = entry.get("name") if isinstance(entry, dict) else None
            if name:
                dirname_to_pid[safe_filename(str(name))] = int(pid_str)

        # 4. Scan library through presets
        track_playlists: dict[int, set[int]] = {tid: set() for tid in track_ids}
        for preset in self.cfg.presets:
            preset_lib = self.cfg.preset_dir(preset.name)
            if not preset_lib.is_dir():
                continue
            for pl_dir in preset_lib.iterdir():
                if not pl_dir.is_dir():
                    continue
                pid = dirname_to_pid.get(pl_dir.name)
                if pid is None:
                    continue
                for f in pl_dir.iterdir():
                    if not f.is_file():
                        continue
                    try:
                        st = f.stat()
                        tid = inode_to_tid.get((st.st_dev, st.st_ino))
                    except OSError:
                        continue
                    if tid is not None:
                        track_playlists.setdefault(tid, set()).add(pid)

        # 5. Rebuild synced_tracks.json
        synced: dict[int, list[int]] = {}
        for tid in sorted(track_ids):
            synced[tid] = sorted(track_playlists.get(tid, set()))
        SyncService._save_synced_state(self.cfg, synced)

        # 6. Rebuild processed_files.json
        processed: dict[str, dict[str, object]] = {}
        for tid in sorted(track_ids):
            entry: dict[str, object] = {"audios": {}}
            for f in downloads.iterdir():
                stem = f.stem.split("_")[0]
                if stem.isdigit() and int(stem) == tid and f.suffix.lower() in (".flac", ".mp3"):
                    rel = workspace_rel_path(f, self.cfg.workspace_path)
                    # Guess spec key from filename
                    spec = _guess_spec_from_filename(f)
                    entry["audios"][spec] = rel
            if entry["audios"]:
                entry["updated_at"] = int(time.time())
                processed[str(tid)] = entry
        save_json(self.cfg.processed_state_file, processed)

        orphaned = sum(1 for tid in track_ids if not track_playlists.get(tid))
        playlist_count = len({pid for pids in track_playlists.values() for pid in pids})
        console.print(f"  重建完成：[cyan]{len(track_ids)}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单")
        if orphaned:
            console.print(f"  [dim]（其中 {orphaned} 首未关联到任何歌单）[/dim]")
        return len(track_ids), playlist_count

    def link_only(self, cookie: str) -> tuple[int, int]:
        self.cfg.ensure_dirs()
        state_map = SyncService._load_synced_state(self.cfg)
        if not state_map:
            console.print("[dim]synced_tracks.json 为空，无需创建链接[/dim]")
            return 0, 0

        playlist_index = load_json(self.cfg.state_dir / "playlists.json", {})
        self.api.login_with_cookie(cookie)
        all_track_ids = list(state_map.keys())
        track_details = self.api.get_tracks_detail(all_track_ids)

        linked_tracks = 0
        for track_id, playlist_ids in state_map.items():
            track = track_details.get(track_id) or Track(
                id=track_id, name=str(track_id), artists=[], album="Unknown Album", raw={}
            )
            has_linked = False
            for pid in playlist_ids:
                entry = playlist_index.get(str(pid))
                dirname = safe_filename(str(entry["name"])) if entry and entry.get("name") else str(pid)
                for preset in self.cfg.presets:
                    spec_key = audio_spec_key(preset.format, preset.bitrate)
                    audio_src = self._find_canonical_for_spec(track_id, spec_key)
                    if not audio_src:
                        continue
                    dst = self.cfg.preset_dir(preset.name) / dirname / f"{format_track_name(preset.filename_template, track)}{audio_src.suffix}"
                    if not dst.exists():
                        create_link(audio_src, dst)
                        has_linked = True
                    lrc_src = self.cfg.downloads_dir / f"{track_id}.{preset.name}.lrc"
                    lrc_dst = self.cfg.preset_dir(preset.name) / dirname / f"{format_track_name(preset.filename_template, track)}.lrc"
                    if lrc_src.exists() and not lrc_dst.exists():
                        create_link(lrc_src, lrc_dst)
                        has_linked = True
            if has_linked:
                linked_tracks += 1

        playlist_count = len({pid for pids in state_map.values() for pid in pids})
        if linked_tracks:
            console.print(f"  链接完成：[cyan]{linked_tracks}[/cyan] 首曲目，[cyan]{playlist_count}[/cyan] 个歌单")
        else:
            console.print("[dim]所有 library 链接均已就绪[/dim]")
        return linked_tracks, playlist_count

    def run_pipeline(self, cookie: str, command: str) -> None:
        self.cfg.ensure_dirs()

        only_pull = command == "pull"
        only_process = command == "process"

        playlist_index: dict[str, dict[str, object]] = {}
        downloaded: list = []
        if not only_process:
            downloaded = self.sync_service.run_sync(cookie=cookie, playlist_ids=self.cfg.get_playlist_ids())
            playlist_index = self.sync_service.playlist_index

        if not only_pull:
            self.process_service.run_process(
                downloaded=downloaded,
                force=self.cfg.force,
                playlist_index=playlist_index,
            )

        ok("完成")


def _guess_spec_from_filename(path: Path) -> str:
    name = path.stem
    suffix = path.suffix.lower()
    fmt_map = {".flac": "FLAC", ".mp3": "MP3", ".m4a": "AAC", ".ogg": "OGG", ".opus": "OPUS"}
    fmt = fmt_map.get(suffix, "UNKNOWN")
    if "_" in name:
        parts = name.split("_", 1)
        if len(parts) > 1 and parts[1].rstrip("k").isdigit():
            return f"{fmt}-{parts[1]}"
    return fmt
```

Add imports at top of run_service.py:
```python
from musicvault.core.preset import audio_spec_key
```

- [ ] **Step 3: Remove --no-translation from CLI**

In `cli/main.py`:
- Remove `--no-translation` from `_add_common_args()`
- Remove `if getattr(args, "no_translation", False): cfg.include_translation = False` from `main()`

- [ ] **Step 4: Verify imports**

```bash
python -c "from musicvault.services.run_service import RunService; print('OK')"
python -c "from musicvault.services.sync_service import SyncService; print('OK')"
```
Expected: OK (both)

- [ ] **Step 5: Commit**

```bash
git add src/musicvault/services/sync_service.py src/musicvault/services/run_service.py src/musicvault/cli/main.py
git commit -m "feat: preset-driven linking in SyncService and RunService
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Update Playlist Reconciliation Tests

**Files:**
- Modify: `tests/test_playlist_reconciliation.py`

- [ ] **Step 1: Update _make_config helper for preset structure**

```python
# tests/test_playlist_reconciliation.py — update _make_config:

from musicvault.core.preset import Preset

def _make_config(tmp_path: Path) -> Config:
    cfg = MagicMock(spec=Config)
    cfg.workspace_path = tmp_path
    cfg.synced_state_file = tmp_path / "state" / "synced_tracks.json"
    cfg.processed_state_file = tmp_path / "state" / "processed_files.json"
    cfg.state_dir = tmp_path / "state"
    cfg.downloads_dir = tmp_path / "downloads"
    cfg.presets = [
        Preset(name="archive", format="flac", filename_template="{artist} - {name}",
               use_karaoke=False, write_lrc_file=False),
        Preset(name="portable", format="mp3", bitrate="192k",
               filename_template="{alias} {name} - {artist}",
               use_karaoke=False, write_lrc_file=True),
    ]
    cfg.preset_dir = lambda name: tmp_path / "library" / name
    cfg.library_dir = tmp_path / "library"
    return cfg
```

Update test assertions that reference `cfg.lossless_dir` / `cfg.lossy_dir`:
- `cfg.lossless_dir / "歌单B"` → `cfg.preset_dir("archive") / "歌单B"`
- `cfg.lossy_dir / "歌单B"` → `cfg.preset_dir("portable") / "歌单B"`

Update `_create_track_links` / `_remove_track_links` calls:
- The old `_create_track_links(flac_src, mp3_src, track, name)` → new `_create_track_links(audio_map, track, name)`
- Tests need to pass an audio_map dict instead of flac_src/mp3_src

Update `_find_lossless_canonical` references:
- Replace with `_find_canonical_for_spec(track_id, "FLAC")`

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests/test_playlist_reconciliation.py -v
```
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_playlist_reconciliation.py
git commit -m "test: update playlist reconciliation tests for preset system
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 8: Integration Verification

**Files:**
- No code changes — verify the whole test suite passes

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -v
```
Expected: All tests PASS

- [ ] **Step 2: Fix any remaining failures**

Fix any broken references or import errors found by the full suite.

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: remaining preset migration fixes
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
- [ ] **Step 4: Run lint**

```bash
ruff check src/ tests/
```
Expected: Clean (or fix issues)

- [ ] **Step 5: Final status**

```bash
git status
git log --oneline -10
```
Expected: Clean working tree, all commits in order.<｜end▁of▁thinking｜>

<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="Read">
<｜｜DSML｜｜parameter name="file_path" string="true">D:\MyPC\Advanced\Code\Python\Projects\MusicVault\src\musicvault\adapters\processors\metadata_writer.py
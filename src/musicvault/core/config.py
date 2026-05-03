from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from musicvault.core.preset import Preset, default_presets, validate_presets
from musicvault.shared.utils import load_json, save_json

_METADATA_FIELD_NAMES = frozenset(
    {"year", "track_number", "disc_number", "genre", "album_artist", "composer", "lyricist", "comment"}
)


@dataclass(slots=True)
class Config:
    cookie: str = ""
    workspace: str = "./workspace"
    force: bool = False
    text_cleaning_enabled: bool = True
    download_workers: int | None = None
    process_workers: int | None = None
    ffmpeg_threads: int | None = None
    download_quality: str = "hires"
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

    # -- song management --
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
        if isinstance(presets_raw, list) and presets_raw:
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
                    "cover_max_size": p.cover_max_size,
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

    if "lrc_encodings" in result:
        enc = result["lrc_encodings"]
        if isinstance(enc, list):
            result["lrc_encodings"] = tuple(str(e).strip() for e in enc if str(e).strip())
            if not result["lrc_encodings"]:
                result["lrc_encodings"] = ("utf-8",)
        elif enc is None:
            result["lrc_encodings"] = ("utf-8",)

    if "metadata_fields" in result:
        mf = result["metadata_fields"]
        if isinstance(mf, list):
            result["metadata_fields"] = tuple(str(f).strip() for f in mf if str(f).strip())
        elif mf is None:
            result["metadata_fields"] = ()
        else:
            result["metadata_fields"] = ()

    # Remove None-valued optional fields so Preset defaults apply
    for key in ("format", "bitrate"):
        if result.get(key) is None and key in result:
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

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from musicvault.shared.utils import load_json, save_json

DEFAULT_LOSSY_LRC_ENCODINGS = ("utf-8",)

_DOWNLOAD_QUALITY_VALUES = frozenset({"standard", "higher", "exhire", "hires", "lossless"})
_LOSSY_FORMAT_VALUES = frozenset({"mp3", "aac", "ogg", "opus"})
_METADATA_FIELD_NAMES = frozenset(
    {"year", "track_number", "disc_number", "genre", "album_artist", "composer", "lyricist", "comment"}
)


@dataclass(slots=True)
class Config:
    cookie: str = ""
    workspace: str = "./workspace"
    force: bool = False
    include_translation: bool = True
    text_cleaning_enabled: bool = True
    download_workers: int | None = None
    process_workers: int | None = None
    ffmpeg_threads: int | None = None
    lossy_lrc_encodings: tuple[str, ...] = DEFAULT_LOSSY_LRC_ENCODINGS
    lossy_bitrate: str = "192k"
    lossy_format: str = "mp3"
    translation_format: str = "separate"
    download_quality: str = "hires"
    embed_cover: bool = True
    cover_max_size_kb: int = 0
    lyrics_embed_in_metadata: bool = True
    lyrics_write_lrc_file: bool = True
    filename_lossless: str = "{artist} - {name}"
    filename_lossy: str = "{alias} {name} - {artist}"
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

    @property
    def lossless_dir(self) -> Path:
        return self.library_dir / "lossless"

    @property
    def lossy_dir(self) -> Path:
        return self.library_dir / "lossy"

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
            self.lossless_dir,
            self.lossy_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

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

    # -- serialization -------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Any) -> Config:
        if not isinstance(raw, dict):
            raise RuntimeError("配置文件格式错误（需为 JSON 对象）")

        workers = raw.get("workers") or {}
        if not isinstance(workers, dict):
            workers = {}

        lyrics = raw.get("lyrics") or {}
        if not isinstance(lyrics, dict):
            lyrics = {}

        text_cleaning = raw.get("text_cleaning") or {}
        if not isinstance(text_cleaning, dict):
            text_cleaning = {}

        raw_encodings = lyrics.get("lossy_lrc_encodings")
        if raw_encodings is not None:
            if not isinstance(raw_encodings, list):
                raise RuntimeError("lyrics.lossy_lrc_encodings 格式错误：需为字符串数组")
            encodings = tuple(str(item).strip() for item in raw_encodings if str(item).strip())
            if not encodings:
                raise RuntimeError("lyrics.lossy_lrc_encodings 不能为空")
        else:
            encodings = DEFAULT_LOSSY_LRC_ENCODINGS

        # -- lossy section --
        lossy = raw.get("lossy") or {}
        if not isinstance(lossy, dict):
            lossy = {}
        lossy_bitrate = str(lossy.get("bitrate") or "192k").strip()
        lossy_format = str(lossy.get("format") or "mp3").strip()
        if lossy_format not in _LOSSY_FORMAT_VALUES:
            raise RuntimeError(f"lossy.format 格式错误：需为 {sorted(_LOSSY_FORMAT_VALUES)}，当前={lossy_format}")

        # -- translation_format --
        translation_format = str(raw.get("translation_format") or "separate").strip()
        if translation_format not in ("separate", "inline"):
            raise RuntimeError(f"translation_format 格式错误：需为 separate 或 inline，当前={translation_format}")


        # -- download section --
        download = raw.get("download") or {}
        if not isinstance(download, dict):
            download = {}
        download_quality = str(download.get("quality") or "hires").strip()
        if download_quality not in _DOWNLOAD_QUALITY_VALUES:
            raise RuntimeError(
                f"download.quality 格式错误：需为 {sorted(_DOWNLOAD_QUALITY_VALUES)}，当前={download_quality}"
            )

        # -- cover section --
        cover = raw.get("cover") or {}
        if not isinstance(cover, dict):
            cover = {}
        embed_cover = bool(cover.get("embed", True))
        cover_max_size_kb = _parse_positive_int(cover.get("max_size_kb"), 0)

        # -- lyrics extended --
        lyrics_embed_in_metadata = bool(lyrics.get("embed_in_metadata", True))
        lyrics_write_lrc_file = bool(lyrics.get("write_lrc_file", True))

        # -- filenames section --
        filenames = raw.get("filenames") or {}
        if not isinstance(filenames, dict):
            filenames = {}
        filename_lossless = str(filenames.get("lossless") or "{artist} - {name}").strip()
        filename_lossy = str(filenames.get("lossy") or "{alias} {name} - {artist}").strip()

        # -- network section --
        network = raw.get("network") or {}
        if not isinstance(network, dict):
            network = {}
        network_download_timeout = max(5, _parse_positive_int(network.get("download_timeout"), 30))
        network_api_timeout = max(5, _parse_positive_int(network.get("api_timeout"), 15))
        network_cover_timeout = max(5, _parse_positive_int(network.get("cover_timeout"), 15))
        network_max_retries = max(0, min(10, _parse_positive_int(network.get("max_retries"), 3)))

        # -- text_cleaning extended --
        text_cleaning_allowlist = str(text_cleaning.get("allowlist", "")).strip()

        # -- metadata section --
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata_fields_raw = metadata.get("fields")
        if metadata_fields_raw is None:
            metadata_fields: tuple[str, ...] = ()
        else:
            if not isinstance(metadata_fields_raw, list):
                raise RuntimeError("metadata.fields 格式错误：需为字符串数组或 null")
            metadata_fields = tuple(
                str(f).strip() for f in metadata_fields_raw if str(f).strip() in _METADATA_FIELD_NAMES
            )

        # -- process section --
        process = raw.get("process") or {}
        if not isinstance(process, dict):
            process = {}
        keep_downloads = bool(process.get("keep_downloads", False))

        # -- playlist section --
        playlist_cfg = raw.get("playlist") or {}
        if not isinstance(playlist_cfg, dict):
            playlist_cfg = {}
        default_playlist_name = str(playlist_cfg.get("default_name") or "未分类").strip() or "未分类"

        # -- ffmpeg section --
        ffmpeg_cfg = raw.get("ffmpeg") or {}
        if not isinstance(ffmpeg_cfg, dict):
            ffmpeg_cfg = {}
        ffmpeg_path = str(ffmpeg_cfg.get("path") or "").strip()

        # -- api section --
        api_cfg = raw.get("api") or {}
        if not isinstance(api_cfg, dict):
            api_cfg = {}
        api_download_url_chunk_size = max(50, _parse_positive_int(api_cfg.get("download_url_chunk_size"), 200))
        api_track_detail_chunk_size = max(50, _parse_positive_int(api_cfg.get("track_detail_chunk_size"), 500))

        # -- alias section --
        alias_cfg = raw.get("alias") or {}
        if not isinstance(alias_cfg, dict):
            alias_cfg = {}
        alias_split_separators = str(alias_cfg.get("split_separators") or "/、;；")

        return cls(
            cookie=str(raw.get("cookie") or "").strip(),
            workspace=str(raw.get("workspace") or "./workspace"),
            force=bool(raw.get("force", False)),
            include_translation=bool(raw.get("include_translation", True)),
            text_cleaning_enabled=bool(text_cleaning.get("enabled", True)),
            download_workers=_parse_workers_int(workers.get("download")),
            process_workers=_parse_workers_int(workers.get("process")),
            ffmpeg_threads=_parse_workers_int(workers.get("ffmpeg_threads")),
            lossy_lrc_encodings=encodings,
            lossy_bitrate=lossy_bitrate,
            lossy_format=lossy_format,
            translation_format=translation_format,
            download_quality=download_quality,
            embed_cover=embed_cover,
            cover_max_size_kb=cover_max_size_kb,
            lyrics_embed_in_metadata=lyrics_embed_in_metadata,
            lyrics_write_lrc_file=lyrics_write_lrc_file,
            filename_lossless=filename_lossless,
            filename_lossy=filename_lossy,
            network_download_timeout=network_download_timeout,
            network_api_timeout=network_api_timeout,
            network_cover_timeout=network_cover_timeout,
            network_max_retries=network_max_retries,
            text_cleaning_allowlist=text_cleaning_allowlist,
            metadata_fields=metadata_fields,
            keep_downloads=keep_downloads,
            default_playlist_name=default_playlist_name,
            ffmpeg_path=ffmpeg_path,
            api_download_url_chunk_size=api_download_url_chunk_size,
            api_track_detail_chunk_size=api_track_detail_chunk_size,
            alias_split_separators=alias_split_separators,
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
            "force": self.force,
            "include_translation": self.include_translation,
            "translation_format": self.translation_format,
            "text_cleaning": {
                "enabled": self.text_cleaning_enabled,
                "allowlist": self.text_cleaning_allowlist,
            },
            "workers": {
                "download": self.download_workers,
                "process": self.process_workers,
                "ffmpeg_threads": self.ffmpeg_threads,
            },
            "lyrics": {
                "lossy_lrc_encodings": list(self.lossy_lrc_encodings),
                "embed_in_metadata": self.lyrics_embed_in_metadata,
                "write_lrc_file": self.lyrics_write_lrc_file,
            },
            "lossy": {
                "bitrate": self.lossy_bitrate,
                "format": self.lossy_format,
            },
            "download": {
                "quality": self.download_quality,
            },
            "cover": {
                "embed": self.embed_cover,
                "max_size_kb": self.cover_max_size_kb,
            },
            "filenames": {
                "lossless": self.filename_lossless,
                "lossy": self.filename_lossy,
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

    # -- alias regex cache (built from config) -------------------------------

    def build_alias_split_re(self) -> re.Pattern[str]:
        sanitized = re.escape(self.alias_split_separators)
        return re.compile(rf"[{sanitized}]+")


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

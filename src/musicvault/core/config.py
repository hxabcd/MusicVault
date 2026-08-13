from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.shared.utils import load_json, save_json


@dataclass(slots=True)
class Config:
    cookie: str = ""
    workspace: str = "./workspace"
    force: bool = False
    text_cleaning_enabled: bool = True
    download_workers: int | None = None
    process_workers: int | None = None
    ffmpeg_threads: int | None = None
    # bootstrap 音质回退用：preset 脚本化后不再由 presets 推导，真实音质取自注册表
    download_quality: str = "hires"
    network_download_timeout: int = 30
    network_api_timeout: int = 15
    network_cover_timeout: int = 15
    network_max_retries: int = 3
    text_cleaning_allowlist: str = ""
    keep_downloads: bool = False
    default_playlist_name: str = "未分类"
    ffmpeg_path: str = ""
    api_download_url_chunk_size: int = 200
    api_track_detail_chunk_size: int = 500
    alias_split_separators: str = "/、;；"
    script_directories: tuple[str, ...] = ()
    builtin_scripts_enabled: bool = True
    _file: Path | None = field(default=None, init=False, repr=False)

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).resolve()

    @property
    def cache_dir(self) -> Path:
        return self.workspace_path / "cache"

    @property
    def media_store_dir(self) -> Path:
        return self.workspace_path / "media_store"

    @property
    def state_db_file(self) -> Path:
        return self.workspace_path / "state.db"

    @property
    def logs_dir(self) -> Path:
        return self.workspace_path / "logs"

    @property
    def library_dir(self) -> Path:
        return self.workspace_path / "library"

    def ensure_dirs(self) -> None:
        # 新布局五区域（cache/media_store/library/logs/state.db）由 WorkspacePaths 单一定义；
        # preset 目录由 preset 脚本自管（内置 hardlink 直接写 library/），此处不再创建。
        WorkspacePaths(self.workspace_path).ensure()

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

        script_system = raw.get("script_system") or raw.get("preset_system") or raw.get("preset") or {}
        if not isinstance(script_system, dict):
            script_system = {}
        script_dirs_raw = (
            raw.get("script_directories")
            if raw.get("script_directories") is not None
            else raw.get("preset_directories")
            if raw.get("preset_directories") is not None
            else script_system.get("directories", [])
        )
        if not isinstance(script_dirs_raw, list):
            script_dirs_raw = []
        script_directories = tuple(str(item).strip() for item in script_dirs_raw if str(item).strip())

        # 旧声明式 presets 数组宽容忽略（preset 已脚本化，不解析不报错）；
        # 旧 preset_system.playlist_links 迁移为 script_system.builtin。
        builtin_scripts_enabled = bool(script_system.get("builtin", script_system.get("playlist_links", True)))

        return cls(
            cookie=str(raw.get("cookie") or "").strip(),
            workspace=str(raw.get("workspace") or "./workspace"),
            text_cleaning_enabled=bool(text_cleaning.get("enabled", True)),
            download_workers=_parse_workers_int(workers.get("download")),
            process_workers=_parse_workers_int(workers.get("process")),
            ffmpeg_threads=_parse_workers_int(workers.get("ffmpeg_threads")),
            download_quality="hires",
            network_download_timeout=max(5, _parse_positive_int(network.get("download_timeout"), 30)),
            network_api_timeout=max(5, _parse_positive_int(network.get("api_timeout"), 15)),
            network_cover_timeout=max(5, _parse_positive_int(network.get("cover_timeout"), 15)),
            network_max_retries=max(0, min(10, _parse_positive_int(network.get("max_retries"), 3))),
            text_cleaning_allowlist=str(text_cleaning.get("allowlist", "")).strip(),
            keep_downloads=bool(process.get("keep_downloads", False)),
            default_playlist_name=str(playlist_cfg.get("default_name") or "未分类").strip() or "未分类",
            ffmpeg_path=str(ffmpeg_cfg.get("path") or "").strip(),
            api_download_url_chunk_size=max(50, _parse_positive_int(api_cfg.get("download_url_chunk_size"), 200)),
            api_track_detail_chunk_size=max(50, _parse_positive_int(api_cfg.get("track_detail_chunk_size"), 500)),
            alias_split_separators=str(alias_cfg.get("split_separators") or "/、;；"),
            script_directories=script_directories,
            builtin_scripts_enabled=builtin_scripts_enabled,
        )

    @classmethod
    def load(cls, file: str | Path) -> Config:
        path = Path(file)
        if path.exists():
            raw = load_json(path, {})
            cfg = cls.from_dict(raw)
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
            "script_system": {
                "directories": list(self.script_directories),
                "builtin": self.builtin_scripts_enabled,
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
            f"旧版配置格式已不再支持。请手动迁移到新配置格式（preset 脚本化）。检测到旧字段：{sorted(found)}。参见文档。"
        )


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

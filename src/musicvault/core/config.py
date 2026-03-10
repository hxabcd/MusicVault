from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from musicvault.core.options import RunOptions
from musicvault.shared.utils import load_json, save_json

DEFAULT_LOSSY_LRC_ENCODINGS = ("gb2312", "gb18030", "utf-8-sig")


@dataclass(slots=True)
class AppConfig:
    """应用目录配置"""

    # 所有运行期数据都落在 workspace 下，便于迁移与备份。
    workspace: Path
    text_cleaning_enabled: bool = True
    download_workers: int | None = None
    process_workers: int | None = None
    ffmpeg_threads: int | None = None
    lossy_lrc_encodings: tuple[str, ...] = ("gb2312", "gb18030", "utf-8-sig")

    @property
    def downloads_dir(self) -> Path:
        return self.workspace / "downloads"

    @property
    def state_dir(self) -> Path:
        return self.workspace / "state"

    @property
    def library_dir(self) -> Path:
        return self.workspace / "library"

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
        """创建运行所需目录"""
        # 统一初始化目录，避免后续流程分支里重复 mkdir。
        for path in (
            self.workspace,
            self.downloads_dir,
            self.state_dir,
            self.lossless_dir,
            self.lossy_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class TextCleaningConfig:
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "TextCleaningConfig":
        if not isinstance(raw, dict):
            return cls()
        return cls(enabled=bool(raw.get("enabled", True)))

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled}


@dataclass(slots=True)
class WorkersConfig:
    download: int | None = None
    process: int | None = None
    ffmpeg_threads: int | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkersConfig":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            download=_to_positive_int_or_none(raw.get("download"), "workers.download"),
            process=_to_positive_int_or_none(raw.get("process"), "workers.process"),
            ffmpeg_threads=_to_positive_int_or_none(raw.get("ffmpeg_threads"), "workers.ffmpeg_threads"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "download": self.download,
            "process": self.process,
            "ffmpeg_threads": self.ffmpeg_threads,
        }


@dataclass(slots=True)
class LyricsConfig:
    lossy_lrc_encodings: tuple[str, ...] = DEFAULT_LOSSY_LRC_ENCODINGS

    @classmethod
    def from_dict(cls, raw: Any) -> "LyricsConfig":
        if not isinstance(raw, dict):
            return cls()
        raw_encodings = raw.get("lossy_lrc_encodings")
        if raw_encodings is None:
            return cls()
        if not isinstance(raw_encodings, list):
            raise RuntimeError("lyrics.lossy_lrc_encodings 格式错误：需为字符串数组")
        encodings = tuple(str(item).strip() for item in raw_encodings if str(item).strip())
        if not encodings:
            raise RuntimeError("lyrics.lossy_lrc_encodings 不能为空")
        return cls(lossy_lrc_encodings=encodings)

    def to_dict(self) -> dict[str, Any]:
        return {"lossy_lrc_encodings": list(self.lossy_lrc_encodings)}


@dataclass(slots=True)
class FileConfig:
    cookie: str = ""
    workspace: str = "./workspace"
    playlist_id: int | None = None
    only_sync: bool = False
    only_process: bool = False
    force: bool = False
    include_translation: bool = True
    text_cleaning: TextCleaningConfig = field(default_factory=TextCleaningConfig)
    workers: WorkersConfig = field(default_factory=WorkersConfig)
    lyrics: LyricsConfig = field(default_factory=LyricsConfig)
    _file: Path | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_dict(cls, raw: Any) -> "FileConfig":
        if not isinstance(raw, dict):
            raise RuntimeError("配置文件格式错误（需为 JSON 对象）")
        playlist_id = raw.get("playlist_id")
        if playlist_id is not None:
            try:
                playlist_id = int(playlist_id)
            except (TypeError, ValueError):
                raise RuntimeError(f"playlist_id 格式错误：{playlist_id}") from None
        return cls(
            cookie=str(raw.get("cookie") or "").strip(),
            workspace=str(raw.get("workspace") or "./workspace"),
            playlist_id=playlist_id,
            only_sync=bool(raw.get("only_sync", False)),
            only_process=bool(raw.get("only_process", False)),
            force=bool(raw.get("force", False)),
            include_translation=bool(raw.get("include_translation", True)),
            text_cleaning=TextCleaningConfig.from_dict(raw.get("text_cleaning")),
            workers=WorkersConfig.from_dict(raw.get("workers")),
            lyrics=LyricsConfig.from_dict(raw.get("lyrics")),
        )

    @classmethod
    def load(cls, file: str | Path) -> "FileConfig":
        path = Path(file)
        if path.exists():
            cfg = cls.from_dict(load_json(path, {}))
        else:
            cfg = cls()
            cfg.save(path)
        cfg._file = path
        return cfg

    def save(self, file: str | Path | None = None) -> None:
        path = Path(file) if file is not None else self._file
        if path is None:
            raise RuntimeError("配置文件路径为空，无法保存")
        save_json(path, self.to_dict())
        self._file = path

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookie": self.cookie,
            "workspace": self.workspace,
            "playlist_id": self.playlist_id,
            "only_sync": self.only_sync,
            "only_process": self.only_process,
            "force": self.force,
            "include_translation": self.include_translation,
            "text_cleaning": self.text_cleaning.to_dict(),
            "workers": self.workers.to_dict(),
            "lyrics": self.lyrics.to_dict(),
        }

    def to_app_config(self, workspace_override: str | None = None) -> AppConfig:
        workspace_value = workspace_override or self.workspace or "./workspace"
        return AppConfig(
            workspace=Path(workspace_value).resolve(),
            text_cleaning_enabled=self.text_cleaning.enabled,
            download_workers=self.workers.download,
            process_workers=self.workers.process,
            ffmpeg_threads=self.workers.ffmpeg_threads,
            lossy_lrc_encodings=self.lyrics.lossy_lrc_encodings,
        )

    def to_run_options(
        self,
        *,
        command: str,
        playlist_id_override: int | None,
        no_translation: bool,
        force_override: bool,
    ) -> RunOptions:
        include_translation = False if no_translation else self.include_translation
        return RunOptions(
            playlist_id=playlist_id_override if playlist_id_override is not None else self.playlist_id,
            only_sync=command == "sync",
            only_process=command == "process",
            include_translation=include_translation,
            force=bool(force_override or self.force),
        )


def _to_positive_int_or_none(value: Any, key: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"{key} 格式错误：{value}") from None
    if parsed <= 0:
        raise RuntimeError(f"{key} 必须大于 0：{value}")
    return parsed


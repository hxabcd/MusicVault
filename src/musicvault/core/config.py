from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from musicvault.shared.utils import load_json, save_json

DEFAULT_LOSSY_LRC_ENCODINGS = ("gb18030", "utf-8-sig")


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
    _file: Path | None = field(default=None, init=False, repr=False)

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).resolve()

    @property
    def downloads_dir(self) -> Path:
        return self.workspace_path / "downloads"

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
            self.state_dir,
            self.lossless_dir,
            self.lossy_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

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
        save_json(path, self.to_dict())
        self._file = path

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookie": self.cookie,
            "workspace": self.workspace,
            "force": self.force,
            "include_translation": self.include_translation,
            "text_cleaning": {"enabled": self.text_cleaning_enabled},
            "workers": {
                "download": self.download_workers,
                "process": self.process_workers,
                "ffmpeg_threads": self.ffmpeg_threads,
            },
            "lyrics": {"lossy_lrc_encodings": list(self.lossy_lrc_encodings)},
        }


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

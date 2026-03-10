from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

ALIAS_SPLIT_RE = re.compile(r"[\/、;；]+")


@dataclass(slots=True)
class Track:
    """统一曲目模型"""

    # 统一曲目模型，屏蔽上游接口字段差异。
    id: int
    name: str
    artists: list[str]
    album: str
    aliases: list[str] = field(default_factory=list)
    cover_url: str | None = None
    duration_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def artist_text(self) -> str:
        """返回用于展示/文件名的歌手字符串"""
        return "/".join(self.artists) if self.artists else "Unknown Artist"

    @property
    def alias(self) -> str | None:
        """获取第一个别名"""
        return self.aliases[0] if self.aliases else None

    @staticmethod
    def _clean_metadata_text(value: str) -> str:
        # normalized = unicodedata.normalize("NFKC", value) 避免过度清理
        cleaned_chars: list[str] = []
        for ch in value:
            if ch in {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"}:
                continue
            if "\ufff0" <= ch <= "\uffff":
                continue
            if unicodedata.category(ch).startswith("C") and ch not in {"\n", "\r", "\t"}:
                continue
            cleaned_chars.append(ch)
        cleaned = "".join(cleaned_chars)
        compacted = re.sub(r"[^\S\r\n]+", " ", cleaned)
        return compacted.strip()

    @classmethod
    def from_ncm_payload(cls, payload: dict[str, Any], *, clean_text: bool = True) -> "Track":
        """从网易云接口数据构建 Track"""
        # 兼容网易云接口常见字段：ar/al 与 artists/album。
        def clean(value: str) -> str:
            return cls._clean_metadata_text(value) if clean_text else value

        artists = payload.get("ar") or payload.get("artists") or []
        artist_names = [clean(a.get("name", "")) for a in artists if a.get("name")]
        aliases_raw = (payload.get("tns") or []) + (payload.get("alia") or [])
        aliases: list[str] = []
        for item in aliases_raw:
            text = clean(str(item))
            if not text:
                continue
            parts = [part.strip() for part in ALIAS_SPLIT_RE.split(text)]
            for part in parts:
                if part and part not in aliases:
                    aliases.append(part)
        album = payload.get("al") or payload.get("album") or {}
        return cls(
            id=int(payload["id"]),
            name=clean(payload.get("name", f"track_{payload['id']}")),
            aliases=aliases,
            artists=artist_names,
            album=clean(album.get("name", "Unknown Album")),
            cover_url=album.get("picUrl"),
            duration_ms=payload.get("dt"),
            raw=payload,
        )


@dataclass(slots=True)
class DownloadedTrack:
    """下载阶段的文件与曲目信息"""

    # 表示下载产物及其来源信息，供解密与分流阶段使用。
    track: Track
    source_file: str
    is_ncm: bool


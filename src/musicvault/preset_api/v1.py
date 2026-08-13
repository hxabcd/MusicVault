from __future__ import annotations

import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from musicvault.domain.lyrics import LyricLine
from musicvault.shared.utils import audio_spec_key

API_VERSION = "v1"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
__all__ = [
    "API_VERSION",
    "AudioFormat",
    "BasePreset",
    "LyricEncoding",
    "MetadataSpec",
    "PresetLoadError",
    "PresetRegistration",
    "PresetRegistry",
    "Quality",
    "audio_spec_key",
]


class PresetLoadError(RuntimeError):
    """preset 发现、校验或初始化失败。"""


@dataclass(frozen=True, slots=True)
class PresetRegistration:
    name: str
    factory: Any
    api_version: str = API_VERSION
    enabled: bool = True
    source: str = "<runtime>"

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise PresetLoadError(f"preset 名称非法：{self.name}")

    def create(self) -> Any:
        if inspect.isclass(self.factory):
            return self.factory()
        if callable(self.factory):
            return self.factory()
        raise PresetLoadError(f"preset '{self.name}' 的 factory 不可调用：{self.source}")


class PresetRegistry:
    """内置和外部 Python preset 的确定性注册表。"""

    def __init__(self) -> None:
        self._registrations: dict[str, PresetRegistration] = {}
        self._loading_source: str | None = None

    def register_preset(self, registration: PresetRegistration) -> PresetRegistration:
        if registration.api_version != API_VERSION:
            raise PresetLoadError(
                f"preset '{registration.name}' 使用不兼容的 API {registration.api_version}，"
                f"当前支持 {API_VERSION}（来源：{registration.source}）"
            )
        if registration.source == "<runtime>" and self._loading_source is not None:
            registration = replace(registration, source=self._loading_source)
        previous = self._registrations.get(registration.name)
        if previous is not None:
            raise PresetLoadError(f"发现同名 preset '{registration.name}'：{previous.source} 与 {registration.source}")
        self._registrations[registration.name] = registration
        return registration

    def preset_registrations(self, *, enabled_only: bool = False) -> tuple[PresetRegistration, ...]:
        values = sorted(self._registrations.values(), key=lambda item: item.name)
        return tuple(item for item in values if item.enabled) if enabled_only else tuple(values)

    def create_preset(self, name: str) -> Any:
        registration = self._registrations.get(name)
        if registration is None:
            raise PresetLoadError(f"未找到 preset：{name}")
        return registration.create()


class Quality(Enum):
    """下载音质等级（按声明顺序从低到高）。"""

    STANDARD = "standard"
    HIGHER = "higher"
    EXHIGH = "exhigh"
    HIRES = "hires"
    LOSSLESS = "lossless"

    @classmethod
    def maximum(cls, items: Iterable["Quality"]) -> "Quality":
        """取列表中的最高音质，空输入回退 HIRES。"""
        ordered = [cls.STANDARD, cls.HIGHER, cls.EXHIGH, cls.HIRES, cls.LOSSLESS]
        candidates = [item for item in items if isinstance(item, Quality)]
        if not candidates:
            return cls.HIRES
        return max(candidates, key=ordered.index)


class AudioFormat(Enum):
    """音频容器格式。"""

    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    OGG = "ogg"
    OPUS = "opus"


class LyricEncoding(Enum):
    """歌词文件编码。"""

    UTF_8 = "utf-8"
    GB18030 = "gb18030"


_FULL_FIELDS = ("year", "track_number", "disc_number", "genre", "album_artist", "composer", "lyricist")


@dataclass(frozen=True, slots=True)
class MetadataSpec:
    """元数据写入规格：封面嵌入与额外字段粒度。"""

    embed_cover: bool = True
    cover_max_size: int = 0
    fields: tuple[str, ...] = _FULL_FIELDS

    @classmethod
    def full(cls, **kwargs) -> "MetadataSpec":
        """全部额外字段 + 嵌入封面；构造函数可覆盖任意项。"""
        return cls(
            embed_cover=kwargs.pop("embed_cover", True),
            cover_max_size=kwargs.pop("cover_max_size", 0),
            fields=kwargs.pop("fields", _FULL_FIELDS),
            **kwargs,
        )

    @classmethod
    def basic(cls, **kwargs) -> "MetadataSpec":
        """基础元数据：嵌入封面；fields 为空时写入器按全部可用字段写入（空集=不限制）；构造函数可覆盖任意项。"""
        return cls(
            embed_cover=kwargs.pop("embed_cover", True),
            cover_max_size=kwargs.pop("cover_max_size", 0),
            fields=kwargs.pop("fields", ()),
            **kwargs,
        )

    @classmethod
    def none(cls, **kwargs) -> "MetadataSpec":
        """不嵌入封面；fields 为空时写入器按全部可用字段写入（空集=不限制）；构造函数可覆盖任意项。"""
        return cls(
            embed_cover=kwargs.pop("embed_cover", False),
            cover_max_size=kwargs.pop("cover_max_size", 0),
            fields=kwargs.pop("fields", ()),
            **kwargs,
        )


class BasePreset:
    """preset 声明基类：音频规格、歌词编码与元数据粒度。"""

    # 注册名：与 PresetRegistration.name 一致；脚本未声明时为空串（运行时仅做键名校验）
    name: str = ""
    quality: Quality = Quality.HIRES
    format: AudioFormat | None = None
    bitrate: str | None = None
    lyrics_encodings: tuple[LyricEncoding, ...] = (LyricEncoding.UTF_8,)
    metadata: MetadataSpec = MetadataSpec.basic()

    def build_lyrics(self, line: LyricLine) -> str:
        from musicvault.preset_api.render import standard_lrc_line

        return standard_lrc_line(line)

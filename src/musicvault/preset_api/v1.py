from __future__ import annotations

import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum, Flag, IntEnum, auto
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
    "MetadataField",
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

    def set_loading_source(self, source: str | None) -> None:
        """进入/退出脚本加载上下文时设置来源（由 script_loader 调用）。"""
        self._loading_source = source

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


class Quality(IntEnum):
    """下载音质等级（数值越大音质越高）。"""

    STANDARD = 1
    """标准"""

    HIGHER = 2
    """较高"""

    EXHIGH = 3
    """极高"""

    LOSSLESS = 4
    """无损"""

    HIRES = 5
    """Hi-Res"""

    @property
    def level(self) -> str:
        """网易云 SDK 音质档位字符串。"""
        return self.name.lower()

    @classmethod
    def maximum(cls, items: Iterable["Quality"]) -> "Quality":
        """取列表中的最高音质，空输入回退 HIRES。"""
        candidates = [item for item in items if isinstance(item, Quality)]
        if not candidates:
            return cls.HIRES
        return max(candidates)


class AudioFormat(Enum):
    """音频容器格式。"""

    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    OGG = "ogg"
    OPUS = "opus"


class LyricEncoding(Enum):
    """歌词文件编码（value 为 Python codec 名，可直接传给 str.encode）。"""

    UTF_8 = "utf-8"
    UTF_8_BOM = "utf-8-sig"
    GB18030 = "gb18030"
    GBK = "gbk"
    GB2312 = "gb2312"
    BIG5 = "big5"
    BIG5_HKSCS = "big5hkscs"
    SHIFT_JIS = "shift_jis"
    EUC_JP = "euc_jp"
    EUC_KR = "euc_kr"


class MetadataField(Flag):
    """元数据字段开关（位掩码）：成员表示可独立开启的元数据字段。"""

    TITLE = auto()
    ARTIST = auto()
    ALBUM = auto()
    YEAR = auto()
    TRACK_NUMBER = auto()
    DISC_NUMBER = auto()
    GENRE = auto()
    ALBUM_ARTIST = auto()
    COMPOSER = auto()
    LYRICIST = auto()
    COMMENT = auto()

    BASIC = TITLE | ARTIST | ALBUM
    ALL = (
        TITLE
        | ARTIST
        | ALBUM
        | YEAR
        | TRACK_NUMBER
        | DISC_NUMBER
        | GENRE
        | ALBUM_ARTIST
        | COMPOSER
        | LYRICIST
        | COMMENT
    )
    NONE = 0


@dataclass(frozen=True, slots=True)
class MetadataSpec:
    """元数据写入规格：封面嵌入与字段开关（MetadataField 位掩码）。"""

    embed_cover: bool = True
    cover_max_size: int = 0
    fields: MetadataField = MetadataField.NONE

    @classmethod
    def full(cls, **kwargs) -> "MetadataSpec":
        """所有元数据字段 + 嵌入封面；构造函数可覆盖任意项。"""
        return cls(
            embed_cover=kwargs.pop("embed_cover", True),
            cover_max_size=kwargs.pop("cover_max_size", 0),
            fields=kwargs.pop("fields", MetadataField.ALL),
            **kwargs,
        )

    @classmethod
    def basic(cls, **kwargs) -> "MetadataSpec":
        """仅标题/艺术家/专辑 + 嵌入封面；构造函数可覆盖任意项。"""
        return cls(
            embed_cover=kwargs.pop("embed_cover", True),
            cover_max_size=kwargs.pop("cover_max_size", 0),
            fields=kwargs.pop("fields", MetadataField.BASIC),
            **kwargs,
        )

    @classmethod
    def none(cls, **kwargs) -> "MetadataSpec":
        """无任何元数据字段且不嵌入封面；构造函数可覆盖任意项。"""
        return cls(
            embed_cover=kwargs.pop("embed_cover", False),
            cover_max_size=kwargs.pop("cover_max_size", 0),
            fields=kwargs.pop("fields", MetadataField.NONE),
            **kwargs,
        )


class BasePreset:
    """preset 声明基类：音频规格、歌词编码与元数据粒度。"""

    # 注册名：与 PresetRegistration.name 一致；脚本未声明时为空串（运行时仅做键名校验）
    name: str = ""
    quality: Quality = Quality.HIRES
    format: AudioFormat | None = None
    bitrate: str | None = None
    # 歌词文件编码：单编码直写；有限字符集（GBK/GB2312/BIG5/SHIFT_JIS/EUC_KR 等）编码失败时该 preset 歌词文件跳过并告警
    lyrics_encoding: LyricEncoding = LyricEncoding.UTF_8
    metadata: MetadataSpec = MetadataSpec.basic()

    def build_lyric_line(self, line: LyricLine) -> str:
        from musicvault.preset_api.render import standard_lrc_line

        return standard_lrc_line(line)

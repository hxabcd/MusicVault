from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from musicvault.domain.lyrics import LyricLine
from musicvault.domain.models import Track
from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, TargetDescriptor
from musicvault.domain.operations import Operation, OperationResult
from musicvault.ports.media import MediaRequest, MediaResolver
from musicvault.ports.target import TargetOperations
from musicvault.preset_api._executor import OperationExecutor
from musicvault.preset_api._media import SnapshotMediaResolver

API_VERSION = "v1"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
__all__ = [
    "API_VERSION",
    "AudioFormat",
    "BasePreset",
    "LyricEncoding",
    "MetadataSpec",
    "Operation",
    "PresetContext",
    "PresetLoadError",
    "PresetRegistration",
    "PresetRegistry",
    "Quality",
    "TargetRegistration",
    "TargetSynchronizer",
    "audio_spec_key",
]


class PresetLoadError(RuntimeError):
    """preset 发现、校验或初始化失败。"""


class TargetSynchronizer(Protocol):
    """preset 的公开最小生命周期契约。"""

    def prepare(self, context: PresetContext) -> Any: ...

    def sync_item(self, track: Track, context: PresetContext) -> Any: ...

    def finalize(self, context: PresetContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class PresetRegistration:
    name: str
    factory: Any
    api_version: str = API_VERSION
    enabled: bool = True
    source: str = "<runtime>"
    target: TargetDescriptor | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise PresetLoadError(f"preset 名称非法：{self.name}")
        if self.target is None:
            object.__setattr__(self, "target", TargetDescriptor(identifier=self.name))

    def create(self) -> Any:
        if inspect.isclass(self.factory):
            return self.factory()
        if all(hasattr(self.factory, method) for method in ("prepare", "sync_item", "finalize")):
            return self.factory
        if callable(self.factory):
            return self.factory()
        raise PresetLoadError(f"preset '{self.name}' 的 factory 不可调用：{self.source}")


@dataclass(slots=True)
class PresetContext:
    """preset 脚本访问源快照、媒体资产和目标操作的唯一公开上下文。"""

    snapshot: SourceSnapshot
    target: TargetOperations
    dry_run: bool = False
    target_descriptor: TargetDescriptor | None = None
    media_resolver: MediaResolver | None = None
    media_store_root: Path | None = None
    _executor: OperationExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._executor = OperationExecutor(self.target, dry_run=self.dry_run)
        if self.media_resolver is None:
            self.media_resolver = SnapshotMediaResolver(self.snapshot)

    def lyrics_file(self, track_id: int, preset_name: str) -> Path | None:
        """返回 media_store/<tid>/{tid}.{preset_name}.lrc（存在才返回）；root 未配置返回 None。"""
        if self.media_store_root is None:
            return None
        candidate = self.media_store_root / str(track_id) / f"{track_id}.{preset_name}.lrc"
        return candidate if candidate.is_file() else None

    @property
    def tracks(self) -> tuple[Track, ...]:
        return self.snapshot.tracks

    @property
    def playlists(self) -> tuple[Playlist, ...]:
        return self.snapshot.playlists

    @property
    def media_assets(self) -> tuple[MediaAsset, ...]:
        return self.snapshot.media_assets

    @property
    def operations(self) -> tuple[OperationResult, ...]:
        return tuple(self._executor.results)

    def media_asset(self, track_id: int, *, asset_type: str = "audio", spec: str | None = None) -> MediaAsset | None:
        assert self.media_resolver is not None
        return self.media_resolver.resolve(MediaRequest(track_id, asset_type, spec))

    def link(self, source: str | Path, destination: str | Path) -> OperationResult:
        return self._executor.link(Path(source), Path(destination))

    def copy(self, source: str | Path, destination: str | Path) -> OperationResult:
        return self._executor.copy(Path(source), Path(destination))

    def write_text(self, destination: str | Path, content: str, encoding: str = "utf-8") -> OperationResult:
        return self._executor.write_text(Path(destination), content, encoding)

    def custom_operation(
        self,
        name: str,
        callback: Callable[[], Any],
        *,
        input_data: dict[str, Any] | None = None,
        affected: Iterable[str] = (),
        idempotent: bool = False,
        retryable: bool = False,
        supports_dry_run: bool = True,
    ) -> OperationResult:
        return self._executor.execute(
            name,
            callback,
            input_data=input_data,
            affected=affected,
            idempotent=idempotent,
            retryable=retryable,
            supports_dry_run=supports_dry_run,
        )


class PresetRegistry:
    """内置和外部 Python preset 的确定性注册表。"""

    def __init__(self) -> None:
        self._registrations: dict[str, PresetRegistration] = {}
        self._target_registrations: dict[str, TargetRegistration] = {}
        self._loading_source: str | None = None

    def register_preset(self, registration: PresetRegistration) -> PresetRegistration:
        if registration.api_version != API_VERSION:
            raise PresetLoadError(
                f"preset '{registration.name}' 使用不兼容的 API {registration.api_version}，"
                f"当前支持 {API_VERSION}（来源：{registration.source}）"
            )
        previous = self._registrations.get(registration.name) or self._target_registrations.get(registration.name)
        if previous is not None:
            raise PresetLoadError(f"发现同名 preset '{registration.name}'：{previous.source} 与 {registration.source}")
        self._registrations[registration.name] = registration
        return registration

    def register_target(self, registration: TargetRegistration) -> TargetRegistration:
        if registration.api_version != API_VERSION:
            raise PresetLoadError(
                f"sync_target '{registration.name}' 使用不兼容的 API {registration.api_version}，"
                f"当前支持 {API_VERSION}（来源：{registration.source}）"
            )
        previous = self._registrations.get(registration.name) or self._target_registrations.get(registration.name)
        if previous is not None:
            raise PresetLoadError(
                f"发现同名 sync_target '{registration.name}'：{previous.source} 与 {registration.source}"
            )
        self._target_registrations[registration.name] = registration
        return registration

    def preset_registrations(self, *, enabled_only: bool = False) -> tuple[PresetRegistration, ...]:
        values = sorted(self._registrations.values(), key=lambda item: item.name)
        return tuple(item for item in values if item.enabled) if enabled_only else tuple(values)

    def target_registrations(self, *, enabled_only: bool = False) -> tuple[TargetRegistration, ...]:
        values = sorted(self._target_registrations.values(), key=lambda item: item.name)
        return tuple(item for item in values if item.enabled) if enabled_only else tuple(values)

    def create_preset(self, name: str) -> Any:
        registration = self._registrations.get(name)
        if registration is None:
            raise PresetLoadError(f"未找到 preset：{name}")
        return registration.create()

    def create_target(self, name: str) -> Any:
        registration = self._target_registrations.get(name)
        if registration is None:
            raise PresetLoadError(f"未找到 sync_target：{name}")
        missing = [dep for dep in registration.depends_on if dep not in self._registrations]
        if missing:
            raise PresetLoadError(
                f"sync_target '{name}' 依赖的 preset 未注册：{', '.join(missing)}（来源：{registration.source}）"
            )
        presets = {dep: self.create_preset(dep) for dep in registration.depends_on}
        return registration.factory(presets)

    def register(self, registration, factory=None, *, api_version=API_VERSION, enabled=True, source=None, target=None):
        # 兼容现有 TargetSynchronizer 脚本：register() 语义 = register_target
        if isinstance(registration, str):
            registration = TargetRegistration(
                name=registration,
                factory=factory,
                api_version=api_version,
                enabled=enabled,
                source=source or self._loading_source or "<runtime>",
                target=target,
            )
        elif source is not None or target is not None:
            raise TypeError("传入 TargetRegistration 时不能重复指定 source 或 target")
        elif registration.source == "<runtime>" and self._loading_source is not None:
            registration = replace(registration, source=self._loading_source)
        return self.register_target(registration)

    def get(self, name: str) -> PresetRegistration | TargetRegistration:
        registration = self._registrations.get(name) or self._target_registrations.get(name)
        if registration is None:
            raise PresetLoadError(f"未找到 preset：{name}")
        return registration

    def registrations(self, *, enabled_only: bool = False) -> tuple[TargetRegistration, ...]:
        # 兼容现有调用：返回 target 注册列表
        return self.target_registrations(enabled_only=enabled_only)

    def load_directories(self, directories: Iterable[str | Path]) -> tuple[TargetRegistration, ...]:
        for directory in sorted((Path(item) for item in directories), key=lambda item: str(item.resolve())):
            if not directory.is_dir():
                continue
            for script in sorted(directory.glob("*.py"), key=lambda item: item.name):
                if script.name.startswith("_"):
                    continue
                self._load_script(script)
        return self.registrations()

    def _load_script(self, script: Path) -> None:
        module_name = f"musicvault_external_preset_{abs(hash(script.resolve()))}"
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            raise PresetLoadError(f"无法加载 preset 脚本：{script}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self._loading_source = str(script)
        try:
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if register is None or not callable(register):
                raise PresetLoadError(f"preset 脚本缺少 register(registry)：{script}")
            register(self)
        except PresetLoadError:
            raise
        except ImportError as error:
            raise PresetLoadError(f"preset 脚本依赖缺失：{script}；请在当前 Python 环境安装 {error.name}") from error
        except Exception as error:
            raise PresetLoadError(f"preset 脚本加载失败：{script}：{error}") from error
        finally:
            self._loading_source = None


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

    def build_lyrics(self, lines: tuple[LyricLine, ...]) -> str:
        from musicvault.preset_api.render import standard_lrc

        return standard_lrc(lines)


@dataclass(frozen=True, slots=True)
class TargetRegistration:
    """sync_target 分发声明：工厂按 depends_on 注入 preset 实例。"""

    name: str
    factory: Any
    depends_on: tuple[str, ...] = ()
    api_version: str = API_VERSION
    enabled: bool = True
    source: str = "<runtime>"
    target: TargetDescriptor | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise PresetLoadError(f"sync_target 名称非法：{self.name}")
        if self.target is None:
            object.__setattr__(self, "target", TargetDescriptor(identifier=self.name))

    def create(self) -> Any:
        # 兼容旧 TargetSynchronizer 消费路径（SyncEngine/get().create()）：无参创建实例；
        # 依赖注入路径（create_target）直接调用 factory(presets)。
        if inspect.isclass(self.factory):
            return self.factory()
        if all(hasattr(self.factory, method) for method in ("prepare", "sync_item", "finalize")):
            return self.factory
        if callable(self.factory):
            return self.factory()
        raise PresetLoadError(f"sync_target '{self.name}' 的 factory 不可调用：{self.source}")


def audio_spec_key(fmt: AudioFormat | None, bitrate: str | None) -> str:
    """音频规格标识：None → ORIGINAL；枚举按 .value.upper()，bitrate 拼后缀。"""
    if fmt is None:
        return "ORIGINAL"
    fmt_upper = fmt.value.upper()
    if bitrate:
        return f"{fmt_upper}-{bitrate}"
    return fmt_upper

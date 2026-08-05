from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from musicvault.application.media_resolver import SnapshotMediaResolver
from musicvault.application.operation_executor import OperationExecutor
from musicvault.core.models import Track
from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, TargetDescriptor
from musicvault.domain.operations import Operation, OperationResult
from musicvault.ports.media import MediaRequest, MediaResolver
from musicvault.ports.target import TargetOperations

API_VERSION = "v1"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
__all__ = [
    "API_VERSION",
    "Operation",
    "PresetContext",
    "PresetLoadError",
    "PresetRegistry",
    "PresetRegistration",
    "TargetSynchronizer",
]


class PresetLoadError(RuntimeError):
    """preset 发现、校验或初始化失败。"""


class TargetSynchronizer(Protocol):
    """preset 的公开最小生命周期契约。"""

    def prepare(self, context: "PresetContext") -> Any: ...

    def sync_item(self, track: Track, context: "PresetContext") -> Any: ...

    def finalize(self, context: "PresetContext") -> Any: ...


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
    _executor: OperationExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._executor = OperationExecutor(self.target, dry_run=self.dry_run)
        if self.media_resolver is None:
            self.media_resolver = SnapshotMediaResolver(self.snapshot)

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

    def register_custom_operation(self, *args: Any, **kwargs: Any) -> OperationResult:
        """公开别名，兼容把“登记并执行”称为 register 的 preset 写法。"""
        return self.custom_operation(*args, **kwargs)


class PresetRegistry:
    """内置和外部 Python preset 的确定性注册表。"""

    def __init__(self) -> None:
        self._registrations: dict[str, PresetRegistration] = {}
        self._loading_source: str | None = None

    def register(
        self,
        registration: PresetRegistration | str,
        factory: Any = None,
        *,
        api_version: str = API_VERSION,
        enabled: bool = True,
        source: str | None = None,
        target: TargetDescriptor | None = None,
    ) -> PresetRegistration:
        if isinstance(registration, str):
            registration = PresetRegistration(
                name=registration,
                factory=factory,
                api_version=api_version,
                enabled=enabled,
                source=source or self._loading_source or "<runtime>",
                target=target,
            )
        elif source is not None or target is not None:
            raise TypeError("传入 PresetRegistration 时不能重复指定 source 或 target")
        elif registration.source == "<runtime>" and self._loading_source is not None:
            registration = replace(registration, source=self._loading_source)
        if registration.api_version != API_VERSION:
            raise PresetLoadError(
                f"preset '{registration.name}' 使用不兼容的 API {registration.api_version}，"
                f"当前支持 {API_VERSION}（来源：{registration.source}）"
            )
        previous = self._registrations.get(registration.name)
        if previous is not None:
            raise PresetLoadError(f"发现同名 preset '{registration.name}'：{previous.source} 与 {registration.source}")
        self._registrations[registration.name] = registration
        return registration

    def get(self, name: str) -> PresetRegistration:
        try:
            return self._registrations[name]
        except KeyError as error:
            raise PresetLoadError(f"未找到 preset：{name}") from error

    def registrations(self, *, enabled_only: bool = False) -> tuple[PresetRegistration, ...]:
        values = sorted(self._registrations.values(), key=lambda item: item.name)
        if enabled_only:
            values = [item for item in values if item.enabled]
        return tuple(values)

    def load_directories(self, directories: Iterable[str | Path]) -> tuple[PresetRegistration, ...]:
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
        except Exception as error:  # noqa: BLE001 - 脚本错误必须阻止不完整同步
            raise PresetLoadError(f"preset 脚本加载失败：{script}：{error}") from error
        finally:
            self._loading_source = None

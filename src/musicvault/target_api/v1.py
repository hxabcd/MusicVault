from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, TargetDescriptor, Track
from musicvault.domain.operations import Operation, OperationResult
from musicvault.ports.media import MediaRequest, MediaResolver
from musicvault.ports.target import TargetOperations
from musicvault.target_api._executor import OperationExecutor
from musicvault.target_api._media import SnapshotMediaResolver

API_VERSION = "v1"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
__all__ = [
    "API_VERSION",
    "Operation",
    "PresetLoadError",
    "TargetContext",
    "TargetRegistration",
    "TargetRegistry",
    "TargetSynchronizer",
]


class PresetLoadError(RuntimeError):
    """sync_target 发现、校验或初始化失败。"""


class TargetSynchronizer(Protocol):
    """sync_target 的公开最小生命周期契约。"""

    def prepare(self, context: TargetContext) -> Any: ...

    def sync_item(self, track: Track, context: TargetContext) -> Any: ...

    def finalize(self, context: TargetContext) -> Any: ...


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
        # 兼容无依赖注入消费路径（直接实例化）；依赖注入路径（create_target）直接调用 factory(presets)。
        if inspect.isclass(self.factory):
            return self.factory()
        if all(hasattr(self.factory, method) for method in ("prepare", "sync_item", "finalize")):
            return self.factory
        if callable(self.factory):
            return self.factory()
        raise PresetLoadError(f"sync_target '{self.name}' 的 factory 不可调用：{self.source}")


@dataclass(slots=True)
class TargetContext:
    """sync_target 脚本访问源快照、媒体资产和目标操作的唯一公开上下文。"""

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


class TargetRegistry:
    """内置和外部 sync_target 的确定性注册表。"""

    def __init__(self) -> None:
        self._registrations: dict[str, TargetRegistration] = {}
        self._loading_source: str | None = None

    def set_loading_source(self, source: str | None) -> None:
        """进入/退出脚本加载上下文时设置来源（由 script_loader 调用）。"""
        self._loading_source = source

    def register_target(self, registration: TargetRegistration) -> TargetRegistration:
        if registration.api_version != API_VERSION:
            raise PresetLoadError(
                f"sync_target '{registration.name}' 使用不兼容的 API {registration.api_version}，"
                f"当前支持 {API_VERSION}（来源：{registration.source}）"
            )
        if registration.source == "<runtime>" and self._loading_source is not None:
            registration = replace(registration, source=self._loading_source)
        previous = self._registrations.get(registration.name)
        if previous is not None:
            raise PresetLoadError(
                f"发现同名 sync_target '{registration.name}'：{previous.source} 与 {registration.source}"
            )
        self._registrations[registration.name] = registration
        return registration

    def target_registrations(self, *, enabled_only: bool = False) -> tuple[TargetRegistration, ...]:
        values = sorted(self._registrations.values(), key=lambda item: item.name)
        return tuple(item for item in values if item.enabled) if enabled_only else tuple(values)

    def create_target(self, name: str, presets: Mapping[str, object]) -> Any:
        """按名称实例化 sync_target，校验 depends_on 依赖并注入 preset 实例。

        供脚本作者运行时按需实例化；引擎内分发路径（SyncEngine / DistributePipeline）
        直接调用 factory 注入，不经过本方法。
        """
        registration = self._registrations.get(name)
        if registration is None:
            raise PresetLoadError(f"未找到 sync_target：{name}")
        missing = [dep for dep in registration.depends_on if dep not in presets]
        if missing:
            raise PresetLoadError(
                f"sync_target '{name}' 依赖的 preset 未注册：{', '.join(missing)}（来源：{registration.source}）"
            )
        return registration.factory({dep: presets[dep] for dep in registration.depends_on})

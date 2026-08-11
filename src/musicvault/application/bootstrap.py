from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.application.pipeline_use_case import PipelineUseCase
from musicvault.application.playlist_use_case import PlaylistUseCase
from musicvault.application.sync_engine import SyncEngine, SyncRunResult
from musicvault.core.config import Config
from musicvault.ports.source import SourceClient
from musicvault.preset_api.builtins import register_builtin_presets
from musicvault.preset_api.v1 import PresetRegistry


@dataclass(frozen=True, slots=True)
class Runtime:
    """composition root 创建的具体运行时依赖。"""

    paths: WorkspacePaths
    state: SQLiteStateRepository
    presets: PresetRegistry


def build_runtime(config: Config) -> Runtime:
    paths = WorkspacePaths(config.workspace_path)
    paths.ensure()
    state = SQLiteStateRepository(SQLiteState(paths.state_db))
    presets = PresetRegistry()
    if config.builtin_playlist_links_enabled:
        register_builtin_presets(presets, paths.library / "playlist_links")
    directories = [Path(directory) for directory in config.preset_directories]
    presets.load_directories(directories)
    for registration in presets.registrations():
        state.register_preset(
            name=registration.name,
            source=registration.source,
            api_version=registration.api_version,
            enabled=registration.enabled,
            # PresetRegistration 暂无 script_hash 字段，统一写 None。
            script_hash=None,
        )
    return Runtime(paths=paths, state=state, presets=presets)


def build_source_client(config: Config) -> NeteaseClient:
    """创建网易云源端 SDK 适配器（composition root 专属）。"""
    return NeteaseClient(
        text_cleaning_enabled=config.text_cleaning_enabled,
        download_quality=config.download_quality,
        api_download_url_chunk_size=config.api_download_url_chunk_size,
        api_track_detail_chunk_size=config.api_track_detail_chunk_size,
        alias_split_separators=config.alias_split_separators,
    )


def build_pipeline(
    config: Config,
    source: SourceClient | None = None,
    *,
    dry_run: bool = False,
) -> PipelineUseCase:
    """组装旧流水线用例的具体依赖；测试可注入 fake source。"""
    if source is None:
        source = build_source_client(config)
    return PipelineUseCase(
        cfg=config,
        api=source,
        state=SQLiteStateRepository(SQLiteState(config.state_db_file)),
        dry_run=dry_run,
    )


def build_playlist_use_case(config: Config) -> PlaylistUseCase:
    """组装歌单/单曲管理用例（add/remove/list 命令专用）。"""
    return PlaylistUseCase(
        cfg=config,
        state=SQLiteStateRepository(SQLiteState(config.state_db_file)),
    )


@dataclass(frozen=True, slots=True)
class TargetSyncPipeline:
    """target-sync 链路的组装：运行时 + 同步引擎；CLI 只负责参数与输出。"""

    runtime: Runtime
    engine: SyncEngine

    def run(self, selected: set[str] | None = None) -> SyncRunResult:
        """执行目标同步；selected 为空集时运行全部启用 preset。"""
        if selected:
            missing = sorted(selected - {item.name for item in self.runtime.presets.registrations()})
            if missing:
                raise RuntimeError(f"未找到指定 preset：{', '.join(missing)}")
        return self.engine.run(
            self.runtime.state.create_snapshot(),
            self.runtime.presets.registrations(enabled_only=True),
            selected=selected,
        )


def build_target_sync_pipeline(config: Config, *, dry_run: bool = False) -> TargetSyncPipeline:
    """组装 target-sync 链路：运行时 + 目标端 + 同步引擎。"""
    runtime = build_runtime(config)
    engine = SyncEngine(FilesystemTarget(runtime.paths.library), dry_run=dry_run)
    return TargetSyncPipeline(runtime=runtime, engine=engine)

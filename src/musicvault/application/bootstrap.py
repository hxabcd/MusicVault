from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.adapters.state.sqlite import SQLiteProcessStateRepository, SQLiteSourceStateRepository, SQLiteState
from musicvault.adapters.targets.filesystem import FilesystemTarget
from musicvault.application.pipeline_use_case import PipelineUseCase
from musicvault.application.playlist_use_case import PlaylistUseCase
from musicvault.application.script_loader import load_preset_directories, load_target_directories
from musicvault.application.sync_engine import SyncEngine, SyncRunResult
from musicvault.core.config import Config
from musicvault.ports.source import SourceClient
from musicvault.preset_api.builtins import register_builtin_presets
from musicvault.preset_api.v1 import PresetRegistry, Quality
from musicvault.target_api.builtins import register_builtin_targets
from musicvault.target_api.v1 import TargetRegistry


@dataclass(frozen=True, slots=True)
class Runtime:
    """composition root 创建的具体运行时依赖。"""

    paths: WorkspacePaths
    source_state: SQLiteSourceStateRepository
    process_state: SQLiteProcessStateRepository
    presets: PresetRegistry
    targets: TargetRegistry


def build_runtime(config: Config) -> Runtime:
    paths = WorkspacePaths(config.workspace_path)
    paths.ensure()
    database = SQLiteState(paths.state_db)
    source_state = SQLiteSourceStateRepository(database)
    process_state = SQLiteProcessStateRepository(database)
    presets = PresetRegistry()
    targets = TargetRegistry()
    if config.builtin_scripts_enabled:
        register_builtin_presets(presets)
        register_builtin_targets(targets, config.library_dir, config.default_playlist_name)
    load_preset_directories([Path(directory) for directory in config.preset_directories], presets)
    load_target_directories([Path(directory) for directory in config.target_directories], targets)
    return Runtime(
        paths=paths,
        source_state=source_state,
        process_state=process_state,
        presets=presets,
        targets=targets,
    )


def build_source_client(config: Config, download_quality: Quality | None = None) -> NeteaseClient:
    """创建网易云源端 SDK 适配器（composition root 专属）。

    download_quality 为 None 时从 config.download_quality（字符串）转 Quality 枚举，
    非法值回退 Quality.HIRES。
    """
    if download_quality is None:
        try:
            download_quality = Quality(config.download_quality)
        except ValueError:
            download_quality = Quality.HIRES
    return NeteaseClient(
        text_cleaning_enabled=config.text_cleaning_enabled,
        download_quality=download_quality,
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
    """组装旧流水线用例的具体依赖；测试可注入 fake source。

    注册内置与外部 preset → 构造 BasePreset 实例索引注入用例 →
    从 preset 声明推导下载音质（最高档）传给源端客户端。
    """
    registry = PresetRegistry()
    targets = TargetRegistry()
    if config.builtin_scripts_enabled:
        register_builtin_presets(registry)
        register_builtin_targets(targets, config.library_dir, config.default_playlist_name)
    load_preset_directories([Path(directory) for directory in config.preset_directories], registry)
    load_target_directories([Path(directory) for directory in config.target_directories], targets)
    presets = {r.name: registry.create_preset(r.name) for r in registry.preset_registrations(enabled_only=True)}
    download_quality = Quality.maximum(p.quality for p in presets.values())
    if source is None:
        source = build_source_client(config, download_quality)
    database = SQLiteState(config.state_db_file)
    return PipelineUseCase(
        cfg=config,
        api=source,
        state=SQLiteSourceStateRepository(database),
        process_state=SQLiteProcessStateRepository(database),
        dry_run=dry_run,
        presets=presets,
        targets=targets,
        target=FilesystemTarget(WorkspacePaths(config.workspace_path).library),
    )


def build_playlist_use_case(config: Config) -> PlaylistUseCase:
    """组装歌单/单曲管理用例（add/remove/list 命令专用）。"""
    return PlaylistUseCase(
        cfg=config,
        state=SQLiteSourceStateRepository(SQLiteState(config.state_db_file)),
    )


@dataclass(frozen=True, slots=True)
class DistributePipeline:
    """distribute 链路的组装：运行时 + 同步引擎；CLI 只负责参数与输出。"""

    runtime: Runtime
    engine: SyncEngine

    def run(self, selected: set[str] | None = None) -> SyncRunResult:
        """执行分发；selected 为空集时运行全部启用 sync_target。"""
        if selected:
            missing = sorted(selected - {item.name for item in self.runtime.targets.target_registrations()})
            if missing:
                raise RuntimeError(f"未找到指定 sync_target：{', '.join(missing)}")
        presets = {
            r.name: self.runtime.presets.create_preset(r.name)
            for r in self.runtime.presets.preset_registrations(enabled_only=True)
        }
        return self.engine.run(
            self.runtime.source_state.create_snapshot(),
            self.runtime.targets.target_registrations(enabled_only=True),
            selected=selected,
            presets=presets,
        )


def build_distribute_pipeline(config: Config, *, dry_run: bool = False) -> DistributePipeline:
    """组装 distribute 链路：运行时 + 目标端 + 同步引擎。"""
    runtime = build_runtime(config)
    engine = SyncEngine(
        FilesystemTarget(runtime.paths.library),
        dry_run=dry_run,
        media_store_root=runtime.paths.media_store,
    )
    return DistributePipeline(runtime=runtime, engine=engine)

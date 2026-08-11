from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.application.pipeline_use_case import PipelineUseCase
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

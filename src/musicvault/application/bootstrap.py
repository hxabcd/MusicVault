from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository
from musicvault.core.config import Config
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

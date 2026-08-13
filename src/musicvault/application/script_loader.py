"""外部脚本统一加载器：一个目录同时发现 preset 与 sync_target 脚本。

加载器以单参数组合对象 ``ScriptRegistries`` 传入脚本的 ``register(registry)``，
脚本按需调用 ``registry.presets.register_preset`` 或 ``registry.targets.register_target``。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from musicvault.preset_api.v1 import PresetLoadError, PresetRegistry
from musicvault.target_api.v1 import TargetRegistry


@dataclass(frozen=True, slots=True)
class ScriptRegistries:
    """传入外部脚本 register(registry) 的组合注册表视图。"""

    presets: PresetRegistry
    targets: TargetRegistry


def load_script_directories(
    directories: Iterable[str | Path],
    presets: PresetRegistry,
    targets: TargetRegistry,
) -> None:
    """遍历外部脚本目录，把每类脚本分发给对应注册表。"""
    for directory in sorted((Path(item) for item in directories), key=lambda item: str(item.resolve())):
        if not directory.is_dir():
            continue
        for script in sorted(directory.glob("*.py"), key=lambda item: item.name):
            if script.name.startswith("_"):
                continue
            _load_script(script, presets, targets)


def _load_script(script: Path, presets: PresetRegistry, targets: TargetRegistry) -> None:
    module_name = f"musicvault_external_script_{abs(hash(script.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise PresetLoadError(f"无法加载脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    presets._loading_source = str(script)
    targets._loading_source = str(script)
    try:
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if register is None or not callable(register):
            raise PresetLoadError(f"脚本缺少 register(registry)：{script}")
        register(ScriptRegistries(presets, targets))
    except PresetLoadError:
        raise
    except ImportError as error:
        raise PresetLoadError(f"脚本依赖缺失：{script}；请在当前 Python 环境安装 {error.name}") from error
    except Exception as error:
        raise PresetLoadError(f"脚本加载失败：{script}：{error}") from error
    finally:
        presets._loading_source = None
        targets._loading_source = None

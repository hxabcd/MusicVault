"""外部脚本加载器：preset 目录与 sync_target 目录分开加载。

preset 目录中的脚本以 ``register(registry)`` 注册到 ``PresetRegistry``，
sync_target 目录中的脚本以 ``register(registry)`` 注册到 ``TargetRegistry``；
两类脚本互不混用，注册表直接作为单参数传入。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path

from musicvault.preset_api.v1 import PresetLoadError, PresetRegistry
from musicvault.target_api.v1 import PresetLoadError as TargetLoadError, TargetRegistry


def load_preset_directories(
    directories: Iterable[str | Path],
    presets: PresetRegistry,
) -> None:
    """遍历 preset 脚本目录，把每个脚本注册到 preset 注册表。"""
    for script in _iter_scripts(directories):
        _load_script(script, presets, PresetLoadError)


def load_target_directories(
    directories: Iterable[str | Path],
    targets: TargetRegistry,
) -> None:
    """遍历 sync_target 脚本目录，把每个脚本注册到 target 注册表。"""
    for script in _iter_scripts(directories):
        _load_script(script, targets, TargetLoadError)


def _iter_scripts(directories: Iterable[str | Path]) -> Iterable[Path]:
    for directory in sorted((Path(item) for item in directories), key=lambda item: str(item.resolve())):
        if not directory.is_dir():
            continue
        for script in sorted(directory.glob("*.py"), key=lambda item: item.name):
            if script.name.startswith("_"):
                continue
            yield script


def _load_script(script: Path, registry: PresetRegistry | TargetRegistry, error_cls: type[Exception]) -> None:
    module_name = f"musicvault_external_script_{abs(hash(script.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise error_cls(f"无法加载脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    registry.set_loading_source(str(script))
    try:
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if register is None or not callable(register):
            raise error_cls(f"脚本缺少 register(registry)：{script}")
        register(registry)
    except error_cls:
        raise
    except ImportError as error:
        raise error_cls(f"脚本依赖缺失：{script}；请在当前 Python 环境安装 {error.name}") from error
    except Exception as error:
        raise error_cls(f"脚本加载失败：{script}：{error}") from error
    finally:
        registry.set_loading_source(None)

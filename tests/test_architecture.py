"""架构约束检查测试：用 ast 扫描依赖方向与公开 API 面。

覆盖 AGENTS.md 的依赖规则：
- application 不直接 import sqlite3 / rich（具体依赖由 composition root 组装）
- adapters 不依赖 application / ports（依赖方向 adapters → domain）
- preset_api / target_api 是版本化公开 API：顶层不重导出 v1 符号，脚本只能走版本化命名空间；
  adapters 允许消费其枚举（Quality/AudioFormat 等，见 Task 8/12）
- target_api 不得依赖 preset_api（两个平行公开包）
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "musicvault"
APPLICATION = SRC / "application"
ADAPTERS = SRC / "adapters"


def _top_level_imports(path: Path) -> set[str]:
    """提取文件中所有 import 的模块全名（不含相对导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_application_does_not_import_sqlite_or_rich() -> None:
    forbidden = {"sqlite3", "rich"}
    offenders: list[tuple[Path, str]] = []
    for path in _py_files(APPLICATION):
        for module in _top_level_imports(path):
            top = module.split(".")[0]
            if top in forbidden:
                offenders.append((path, module))
    assert not offenders, f"application 违规 import：{offenders}"


def test_adapters_do_not_import_application_or_ports() -> None:
    forbidden_prefixes = ("musicvault.application", "musicvault.ports")
    offenders: list[tuple[Path, str]] = []
    for path in _py_files(ADAPTERS):
        for module in _top_level_imports(path):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes):
                offenders.append((path, module))
    assert not offenders, f"adapters 违规 import：{offenders}"


def test_preset_api_top_level_exposes_only_v1() -> None:
    """顶层包不得重导出 v1 公开符号，脚本只能经版本化命名空间访问。"""
    _assert_top_level_exposes_only_v1("preset_api")


def test_target_api_top_level_exposes_only_v1() -> None:
    """target_api 顶层同构：只暴露 v1 命名空间。"""
    _assert_top_level_exposes_only_v1("target_api")


def _assert_top_level_exposes_only_v1(package: str) -> None:
    init = SRC / package / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(f"musicvault.{package}"):
            imported.extend(alias.name for alias in node.names)
    assert imported == ["v1"], f"{package} 顶层 import 面应为仅 v1，实际：{imported}"

    import importlib

    api = importlib.import_module(f"musicvault.{package}")

    assert not hasattr(api, "PresetRegistry")
    assert not hasattr(api, "TargetContext")
    assert hasattr(api, "v1")


def test_target_api_does_not_import_preset_api() -> None:
    """target_api 是平行公开包：不依赖 preset_api。"""
    offenders: list[tuple[Path, str]] = []
    target_root = SRC / "target_api"
    for path in _py_files(target_root):
        for module in _top_level_imports(path):
            if module == "musicvault.preset_api" or module.startswith("musicvault.preset_api."):
                offenders.append((path, module))
    assert not offenders, f"target_api 违规 import preset_api：{offenders}"


def test_preset_api_has_no_orphan_executor_or_media() -> None:
    """preset_api 不应残留已迁移到 target_api 的 _executor/_media 死代码。"""
    preset_root = SRC / "preset_api"
    orphans = [p.name for p in list(preset_root.glob("_executor.py")) + list(preset_root.glob("_media.py"))]
    assert not orphans, f"preset_api 残留已迁移死代码：{orphans}"

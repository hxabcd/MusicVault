"""架构约束检查测试：用 ast 扫描依赖方向与公开 API 面。

覆盖 AGENTS.md 的依赖规则：
- application 不直接 import sqlite3 / rich（具体依赖由 composition root 组装）
- adapters 不依赖 application / preset_api / ports（依赖方向 adapters → domain）
- preset_api 顶层不重导出 v1 符号（脚本只能走版本化公开 API）
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


def test_adapters_do_not_import_application_preset_api_or_ports() -> None:
    forbidden_prefixes = ("musicvault.application", "musicvault.preset_api", "musicvault.ports")
    offenders: list[tuple[Path, str]] = []
    for path in _py_files(ADAPTERS):
        for module in _top_level_imports(path):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes):
                offenders.append((path, module))
    assert not offenders, f"adapters 违规 import：{offenders}"


def test_preset_api_top_level_exposes_only_v1() -> None:
    """顶层包不得重导出 v1 公开符号，脚本只能经版本化命名空间访问。"""
    init = SRC / "preset_api" / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("musicvault.preset_api"):
            imported.extend(alias.name for alias in node.names)
    assert imported == ["v1"], f"preset_api 顶层 import 面应为仅 v1，实际：{imported}"

    import musicvault.preset_api as preset_api

    assert not hasattr(preset_api, "PresetRegistry")
    assert not hasattr(preset_api, "PresetContext")
    assert hasattr(preset_api, "v1")

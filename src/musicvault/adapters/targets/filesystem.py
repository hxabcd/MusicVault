from __future__ import annotations

import os
import shutil
from pathlib import Path

from musicvault.shared.utils import same_file_content


class FilesystemTarget:
    """本地目录目标适配器；冲突和 dry-run 由上层操作执行器统一处理。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def link(self, source: Path, destination: Path) -> None:
        source = Path(source)
        destination = self._resolve(destination)
        if not source.is_file():
            raise FileNotFoundError(f"链接源文件不存在：{source}")
        if destination.exists():
            if destination.is_file() and same_file_content(source, destination):
                return
            raise FileExistsError(f"目标文件已存在且内容不同：{destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    def copy(self, source: Path, destination: Path) -> None:
        source = Path(source)
        destination = self._resolve(destination)
        if not source.is_file():
            raise FileNotFoundError(f"复制源文件不存在：{source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if same_file_content(source, destination):
                return
            raise FileExistsError(f"目标文件已存在且内容不同：{destination}")
        shutil.copy2(source, destination)

    def write_text(self, destination: Path, content: str, encoding: str = "utf-8") -> None:
        destination = self._resolve(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_text(encoding=encoding) == content:
                return
            raise FileExistsError(f"目标文本已存在且内容不同：{destination}")
        destination.write_text(content, encoding=encoding)

    def _resolve(self, destination: Path) -> Path:
        candidate = Path(destination)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"目标路径超出目标根目录：{destination}") from error
        return resolved

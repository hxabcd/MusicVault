"""FilesystemTarget / WorkspacePaths 补充单测：系统调用回退与剩余边界。

覆盖：link 硬链接失败回退复制、copy 源缺失报错、WorkspacePaths 旧布局路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.filesystem.workspace import WorkspacePaths
from musicvault.adapters.targets.filesystem import FilesystemTarget


def test_link_falls_back_to_copy_when_hardlink_fails(tmp_path: Path, monkeypatch) -> None:
    """os.link 抛 OSError（如跨文件系统）时回退 shutil.copy2。"""
    target = FilesystemTarget(tmp_path / "root")
    source = tmp_path / "source.txt"
    source.write_text("x", encoding="utf-8")

    def fake_link(_src, _dst) -> None:
        raise OSError("不支持硬链接")

    monkeypatch.setattr("musicvault.adapters.targets.filesystem.os.link", fake_link)
    target.link(source, tmp_path / "root" / "out.txt")

    assert (tmp_path / "root" / "out.txt").read_text(encoding="utf-8") == "x"


def test_copy_missing_source_raises(tmp_path: Path) -> None:
    """copy 源文件不存在 → FileNotFoundError。"""
    target = FilesystemTarget(tmp_path / "root")

    with pytest.raises(FileNotFoundError, match="复制源文件不存在"):
        target.copy(tmp_path / "missing.txt", tmp_path / "root" / "out.txt")


def test_workspace_media_asset_path_legacy_layout(tmp_path: Path) -> None:
    """已废弃的 <tid>/<asset_type>/ 旧布局路径仍可构造。"""
    paths = WorkspacePaths(tmp_path)
    expected = tmp_path.resolve() / "media_store" / "1" / "audio" / "1.flac"
    assert paths.media_asset_path(1, "audio", "1.flac") == expected

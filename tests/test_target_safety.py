"""FilesystemTarget 安全语义与幂等断言。

覆盖：越界路径（相对逃逸 / 绝对路径逃逸）抛 ValueError、覆盖冲突抛 FileExistsError、
同内容幂等跳过、多余文件保留（append 语义）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.adapters.targets.filesystem import FilesystemTarget


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# -- 越界路径防护 ---------------------------------------------------------


def test_link_rejects_relative_escape(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "x")

    with pytest.raises(ValueError):
        target.link(source, Path("../outside.txt"))


def test_link_rejects_absolute_path_outside_root(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "x")

    with pytest.raises(ValueError):
        target.link(source, tmp_path / "outside.txt")


def test_copy_rejects_escape(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "x")

    with pytest.raises(ValueError):
        target.copy(source, tmp_path / "outside.txt")


def test_write_text_rejects_escape(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")

    with pytest.raises(ValueError):
        target.write_text(Path("../outside.txt"), "x")


def test_absolute_path_inside_root_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = FilesystemTarget(root)
    source = _write(tmp_path / "source.txt", "x")
    inside = root / "sub" / "out.txt"

    target.copy(source, inside)

    assert inside.read_text(encoding="utf-8") == "x"


# -- 覆盖冲突 -------------------------------------------------------------


def test_link_conflict_raises_when_content_differs(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "新内容")
    destination = tmp_path / "root" / "out.txt"
    _write(destination, "旧内容")

    with pytest.raises(FileExistsError):
        target.link(source, destination)


def test_copy_conflict_raises_when_content_differs(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "新内容")
    destination = tmp_path / "root" / "out.txt"
    _write(destination, "旧内容")

    with pytest.raises(FileExistsError):
        target.copy(source, destination)


def test_write_text_conflict_raises_when_content_differs(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    destination = tmp_path / "root" / "out.txt"
    _write(destination, "旧内容")

    with pytest.raises(FileExistsError):
        target.write_text(destination, "新内容")


# -- 同内容幂等跳过 ---------------------------------------------------------


def test_link_skips_when_same_content(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "相同")
    destination = tmp_path / "root" / "out.txt"
    _write(destination, "相同")

    target.link(source, destination)  # 同内容：跳过，不报错也不覆盖

    assert destination.read_text(encoding="utf-8") == "相同"


def test_copy_skips_when_same_content(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "相同")
    destination = tmp_path / "root" / "out.txt"
    _write(destination, "相同")

    target.copy(source, destination)

    assert destination.read_text(encoding="utf-8") == "相同"


def test_write_text_skips_when_same_content(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    destination = tmp_path / "root" / "out.txt"
    _write(destination, "相同")

    target.write_text(destination, "相同")

    assert destination.read_text(encoding="utf-8") == "相同"


def test_repeated_link_does_not_create_duplicate(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")
    source = _write(tmp_path / "source.txt", "相同")

    target.link(source, tmp_path / "root" / "out.txt")
    target.link(source, tmp_path / "root" / "out.txt")  # 第二次同内容：幂等跳过

    assert (tmp_path / "root" / "out.txt").read_text(encoding="utf-8") == "相同"
    assert len(list((tmp_path / "root").iterdir())) == 1


# -- 其他前置校验 ----------------------------------------------------------


def test_link_missing_source_raises_file_not_found(tmp_path: Path) -> None:
    target = FilesystemTarget(tmp_path / "root")

    with pytest.raises(FileNotFoundError):
        target.link(tmp_path / "missing.txt", tmp_path / "root" / "out.txt")


# -- append 语义：多余文件保留 -------------------------------------------------


def test_extra_files_in_target_root_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = FilesystemTarget(root)
    extra = _write(root / "extra.txt", "keep-me")
    source = _write(tmp_path / "source.txt", "x")

    target.link(source, root / "linked.txt")
    target.copy(source, root / "copied.txt")
    target.write_text(root / "written.txt", "text")

    # append 语义：目标目录中预设之外的「多余文件」不会被删除
    assert extra.read_text(encoding="utf-8") == "keep-me"
    assert sorted(path.name for path in root.iterdir()) == ["copied.txt", "extra.txt", "linked.txt", "written.txt"]

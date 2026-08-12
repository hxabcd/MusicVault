"""shared/utils.py 纯函数测试。

覆盖：文件 SHA-256 摘要与内容比较、硬链接回退单次警告、
文件名清洗、曲目文件名模板、workspace 相对路径、
JSON 读写（含损坏/IO 错误分支）与硬链接/复制辅助函数。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from musicvault.domain.models import Track
from musicvault.shared import utils


def _make_track(**overrides: Any) -> Track:
    """构造带常用字段的 Track，便于按需覆盖。"""
    base: dict[str, Any] = dict(id=123, name="歌曲", artists=["甲", "乙"], album="专辑")
    base.update(overrides)
    return Track(**base)


class TestSha256File:
    def test_known_content_digest(self, tmp_path: Path) -> None:
        path = tmp_path / "a.bin"
        path.write_bytes(b"hello world")
        assert utils.sha256_file(path) == hashlib.sha256(b"hello world").hexdigest()

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        assert utils.sha256_file(path) == hashlib.sha256(b"").hexdigest()

    def test_multi_chunk_content(self, tmp_path: Path) -> None:
        # 超过单次 1MB 分块读取，覆盖迭代读取循环
        path = tmp_path / "big.bin"
        payload = b"x" * (1024 * 1024 + 17)
        path.write_bytes(payload)
        assert utils.sha256_file(path) == hashlib.sha256(payload).hexdigest()


class TestSameFileContent:
    def test_identical_files(self, tmp_path: Path) -> None:
        first = tmp_path / "a.txt"
        second = tmp_path / "b.txt"
        first.write_text("相同内容", encoding="utf-8")
        second.write_text("相同内容", encoding="utf-8")
        assert utils.same_file_content(first, second) is True

    def test_different_sizes(self, tmp_path: Path) -> None:
        first = tmp_path / "a.txt"
        second = tmp_path / "b.txt"
        first.write_text("123", encoding="utf-8")
        second.write_text("1234", encoding="utf-8")
        assert utils.same_file_content(first, second) is False

    def test_same_size_different_content(self, tmp_path: Path) -> None:
        first = tmp_path / "a.txt"
        second = tmp_path / "b.txt"
        first.write_text("aaaa", encoding="utf-8")
        second.write_text("aaab", encoding="utf-8")
        assert utils.same_file_content(first, second) is False

    def test_both_empty(self, tmp_path: Path) -> None:
        first = tmp_path / "a.txt"
        second = tmp_path / "b.txt"
        first.write_text("", encoding="utf-8")
        second.write_text("", encoding="utf-8")
        assert utils.same_file_content(first, second) is True

    def test_chunk_boundary_difference(self, tmp_path: Path) -> None:
        # 首块相同、后续块不同的多块比较
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        first.write_bytes(b"a" * (1024 * 1024) + b"tail")
        second.write_bytes(b"a" * (1024 * 1024) + b"TALL")
        assert utils.same_file_content(first, second) is False

    def test_large_identical_files(self, tmp_path: Path) -> None:
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        payload = b"z" * (1024 * 1024 + 5)
        first.write_bytes(payload)
        second.write_bytes(payload)
        assert utils.same_file_content(first, second) is True


class TestWarnHardlinkFallbackOnce:
    def test_warns_only_first_time(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(utils, "_hardlink_fallback_warned", False)
        with caplog.at_level(logging.WARNING, logger="musicvault.shared.utils"):
            utils._warn_hardlink_fallback_once()
            utils._warn_hardlink_fallback_once()
        assert len(caplog.records) == 1
        assert "硬链接不可用" in caplog.text

    def test_silent_when_already_warned(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(utils, "_hardlink_fallback_warned", True)
        with caplog.at_level(logging.WARNING, logger="musicvault.shared.utils"):
            utils._warn_hardlink_fallback_once()
        assert caplog.records == []


class TestSafeFilename:
    def test_invalid_characters_replaced(self) -> None:
        name = 'a<b>c:d"e/f\\g|h?i*j'
        assert utils.safe_filename(name) == "a_b_c_d_e_f_g_h_i_j"

    def test_control_characters_replaced(self) -> None:
        assert utils.safe_filename("ab\x00cd\x1f") == "ab_cd_"

    def test_multiple_spaces_compacted(self) -> None:
        assert utils.safe_filename("a    b") == "a b"

    def test_strips_spaces_and_dots(self) -> None:
        assert utils.safe_filename("  歌曲  ") == "歌曲"
        assert utils.safe_filename("歌曲. . .") == "歌曲"

    def test_chinese_preserved(self) -> None:
        assert utils.safe_filename("歌曲 - 歌手") == "歌曲 - 歌手"

    def test_empty_uses_default_fallback(self) -> None:
        assert utils.safe_filename("") == "untitled"
        assert utils.safe_filename("   . . ") == "untitled"

    def test_custom_fallback(self) -> None:
        assert utils.safe_filename("...", fallback="未知") == "未知"


class TestFormatTrackName:
    def test_all_placeholders(self) -> None:
        track = _make_track(aliases=["别名A"])
        name = utils.format_track_name("{name} - {artist} - {alias} - {album} - {track_id}", track)
        assert name == "歌曲 - 甲,乙 - 别名A - 专辑 - 123"

    def test_title_placeholder(self) -> None:
        assert utils.format_track_name("{title}", _make_track()) == "歌曲"

    def test_no_alias_yields_empty(self) -> None:
        assert utils.format_track_name("{name}（{alias}）", _make_track()) == "歌曲（）"

    def test_unknown_placeholder_kept_as_is(self) -> None:
        assert utils.format_track_name("前缀 {foo} 后缀", _make_track()) == "前缀 {foo} 后缀"

    def test_invalid_filename_chars_cleaned(self) -> None:
        track = Track(id=1, name="歌:曲", artists=[], album="专:辑")
        assert utils.format_track_name("{name} - {album}", track) == "歌_曲 - 专_辑"

    def test_result_is_stripped(self) -> None:
        assert utils.format_track_name("  {name}  ", _make_track()) == "歌曲"


class TestWorkspaceRelPath:
    def test_inside_workspace(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "file.txt"
        assert utils.workspace_rel_path(target, tmp_path) == os.path.join("sub", "file.txt")

    def test_workspace_itself(self, tmp_path: Path) -> None:
        assert utils.workspace_rel_path(tmp_path, tmp_path) == "."

    def test_outside_workspace_returns_absolute(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "elsewhere.txt"
        result = utils.workspace_rel_path(outside, tmp_path)
        assert Path(result).is_absolute()
        assert result == str(outside.resolve())

    def test_missing_file_still_resolves(self, tmp_path: Path) -> None:
        missing = tmp_path / "sub" / "missing.txt"
        assert utils.workspace_rel_path(missing, tmp_path) == os.path.join("sub", "missing.txt")


class TestLoadJson:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        assert utils.load_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}

    def test_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text('{"a": 1, "中文": "值"}', encoding="utf-8")
        assert utils.load_json(path, {}) == {"a": 1, "中文": "值"}

    def test_corrupted_json_returns_default_with_warning(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not json!!", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="musicvault.shared.utils"):
            assert utils.load_json(path, [1, 2]) == [1, 2]
        assert len(caplog.records) == 1
        assert "状态文件已损坏" in caplog.text

    def test_empty_file_treated_as_corrupted(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "state.json"
        path.write_text("", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="musicvault.shared.utils"):
            assert utils.load_json(path, None) is None
        assert len(caplog.records) == 1

    def test_read_error_returns_default(self) -> None:
        class _BrokenPath:
            """只实现 load_json 所需接口的路径替身，模拟读取 IO 错误。"""

            def __init__(self, exists: bool) -> None:
                self._exists = exists

            def exists(self) -> bool:
                return self._exists

            def read_text(self, *, encoding: str = "utf-8") -> str:
                del encoding
                raise OSError("模拟 IO 错误")

        assert utils.load_json(_BrokenPath(True), "fallback") == "fallback"


class TestSaveJson:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "state.json"
        utils.save_json(path, {"x": 1})
        assert path.read_text(encoding="utf-8") == '{"x": 1}'

    def test_chinese_not_escaped(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        utils.save_json(path, {"名称": "值"})
        assert "名称" in path.read_text(encoding="utf-8")

    def test_indent(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        utils.save_json(path, {"a": [1, 2]}, indent=2)
        assert path.read_text(encoding="utf-8") == json.dumps({"a": [1, 2]}, ensure_ascii=False, indent=2)

    def test_no_tmp_leftover(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        utils.save_json(path, 1)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_overwrite_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("旧内容", encoding="utf-8")
        utils.save_json(path, {"新": True})
        assert json.loads(path.read_text(encoding="utf-8")) == {"新": True}


class TestHardlinkOrCopy:
    def test_dst_exists_is_noop(self, tmp_path: Path, caplog) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("内容", encoding="utf-8")
        dst.write_text("已存在", encoding="utf-8")
        utils.hardlink_or_copy(src, dst)
        assert dst.read_text(encoding="utf-8") == "已存在"
        assert src.stat().st_ino != dst.stat().st_ino
        assert caplog.records == []

    def test_missing_src_is_noop(self, tmp_path: Path, caplog) -> None:
        dst = tmp_path / "dst.txt"
        utils.hardlink_or_copy(tmp_path / "missing.txt", dst)
        assert not dst.exists()
        assert caplog.records == []

    def test_creates_hardlink(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("内容", encoding="utf-8")
        utils.hardlink_or_copy(src, dst)
        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == "内容"
        assert dst.stat().st_ino == src.stat().st_ino

    def test_oserror_falls_back_to_copy_warns_once(self, tmp_path: Path, monkeypatch, caplog) -> None:
        monkeypatch.setattr(utils, "_hardlink_fallback_warned", False)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("模拟链接失败")

        monkeypatch.setattr(utils.os, "link", _boom)
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("内容", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="musicvault.shared.utils"):
            utils.hardlink_or_copy(src, dst)
            utils.hardlink_or_copy(src, dst)
        assert dst.read_text(encoding="utf-8") == "内容"
        assert len(caplog.records) == 1
        assert "硬链接不可用" in caplog.text

    def test_missing_parent_dir_propagates(self, tmp_path: Path, monkeypatch, caplog) -> None:
        # 父目录缺失时 os.link 与 copy2 均失败，异常向上传播
        monkeypatch.setattr(utils, "_hardlink_fallback_warned", False)
        src = tmp_path / "src.txt"
        src.write_text("内容", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="musicvault.shared.utils"):
            with pytest.raises(FileNotFoundError):
                utils.hardlink_or_copy(src, tmp_path / "nested" / "dst.txt")
        assert len(caplog.records) == 1


class TestCreateLink:
    def test_creates_parent_dirs_and_hardlink(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "x" / "y" / "dst.txt"
        src.write_text("内容", encoding="utf-8")
        utils.create_link(src, dst)
        assert dst.exists()
        assert dst.stat().st_ino == src.stat().st_ino
        assert (tmp_path / "x" / "y").is_dir()

    def test_dst_exists_skips(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("新", encoding="utf-8")
        dst.write_text("旧", encoding="utf-8")
        utils.create_link(src, dst)
        assert dst.read_text(encoding="utf-8") == "旧"

    def test_missing_src_skips(self, tmp_path: Path) -> None:
        dst = tmp_path / "x" / "dst.txt"
        utils.create_link(tmp_path / "missing.txt", dst)
        assert not dst.exists()
        assert not dst.parent.exists()

    def test_oserror_falls_back_to_copy(self, tmp_path: Path, monkeypatch, caplog) -> None:
        monkeypatch.setattr(utils, "_hardlink_fallback_warned", False)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("模拟链接失败")

        monkeypatch.setattr(utils.os, "link", _boom)
        src = tmp_path / "src.txt"
        dst = tmp_path / "sub" / "dst.txt"
        src.write_text("内容", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="musicvault.shared.utils"):
            utils.create_link(src, dst)
        assert dst.read_text(encoding="utf-8") == "内容"
        assert len(caplog.records) == 1

"""domain 模型补充单测：构造边界、反序列化容错与快照查询回退。

覆盖：MediaAsset/TargetDescriptor 非法字段、SourceSnapshot 查询未命中、
_json_safe 特殊类型、lyrics_from_json 非数组/非对象行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.domain.lyrics import lyrics_from_json
from musicvault.domain.models import MediaAsset, SourceSnapshot, TargetDescriptor, Track, _json_safe


def test_media_asset_rejects_empty_type() -> None:
    """资产类型为空（含纯空白）→ ValueError。"""
    with pytest.raises(ValueError, match="类型"):
        MediaAsset(track_id=1, asset_type="", spec="FLAC", path=Path("x.flac"))


def test_media_asset_rejects_empty_spec() -> None:
    """资产规格为空 → ValueError。"""
    with pytest.raises(ValueError, match="规格"):
        MediaAsset(track_id=1, asset_type="audio", spec="  ", path=Path("x.flac"))


def test_target_descriptor_rejects_invalid_deletion_policy() -> None:
    """未知删除策略 → ValueError。"""
    with pytest.raises(ValueError, match="删除策略"):
        TargetDescriptor(identifier="t", deletion_policy="nuke")


def test_snapshot_queries_missing_return_none() -> None:
    """快照查询未命中曲目/歌单时返回 None。"""
    track = Track(id=1, name="歌", artists=[], album="")
    snapshot = SourceSnapshot.from_data((track,), (), ())
    assert snapshot.track(1) is not None
    assert snapshot.track(99) is None
    assert snapshot.playlist(1) is None


def test_json_safe_special_values() -> None:
    """_json_safe：Path 转字符串、元组转列表、未知类型走 repr。"""
    assert _json_safe(Path("a/b")) == str(Path("a/b"))
    assert _json_safe((1, "x")) == [1, "x"]
    assert _json_safe(b"bytes") == "b'bytes'"
    # 嵌套在 dict 中同样递归处理
    assert _json_safe({"p": Path("x"), "t": (2, 3)}) == {"p": "x", "t": [2, 3]}


def test_lyrics_from_json_rejects_non_list() -> None:
    """payload 非行数组 → ValueError。"""
    with pytest.raises(ValueError, match="行数组"):
        lyrics_from_json('{"start_ms": 1}')


def test_lyrics_from_json_rejects_non_dict_line() -> None:
    """行元素非 dict → ValueError。"""
    with pytest.raises(ValueError, match="歌词行"):
        lyrics_from_json("[1, 2]")

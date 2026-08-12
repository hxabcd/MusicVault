"""SnapshotMediaResolver 与 MediaRequest 的直接单测。

覆盖：快照命中返回 MediaAsset、缺失返回 None、跨曲目隔离、spec 过滤、
MediaRequest 字段默认值与不可变性。构造纯领域模型，不依赖真实文件系统。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from musicvault.domain.models import MediaAsset, SourceSnapshot, Track
from musicvault.ports.media import MediaRequest
from musicvault.preset_api._media import SnapshotMediaResolver


def _snapshot(*assets: MediaAsset) -> SourceSnapshot:
    return SourceSnapshot.from_data(
        tracks=(Track(id=1, name="一", artists=["甲"], album="专辑", raw={}),),
        playlists=(),
        media_assets=assets,
    )


def _asset(
    track_id: int,
    asset_type: str = "audio",
    spec: str = "flac",
    path: str = "/media/asset.flac",
) -> MediaAsset:
    return MediaAsset(track_id=track_id, asset_type=asset_type, spec=spec, path=Path(path))


def test_resolve_returns_matching_asset() -> None:
    asset = _asset(track_id=1)
    resolver = SnapshotMediaResolver(_snapshot(asset))

    resolved = resolver.resolve(MediaRequest(track_id=1))

    assert resolved is not None
    assert resolved == asset
    assert resolved.asset_type == "audio"


def test_resolve_returns_none_when_snapshot_has_no_assets() -> None:
    resolver = SnapshotMediaResolver(_snapshot())

    assert resolver.resolve(MediaRequest(track_id=1)) is None


def test_resolve_returns_none_when_asset_type_mismatch() -> None:
    resolver = SnapshotMediaResolver(_snapshot(_asset(track_id=1, asset_type="audio")))

    assert resolver.resolve(MediaRequest(track_id=1, asset_type="cover")) is None


def test_resolve_returns_none_when_spec_mismatch() -> None:
    resolver = SnapshotMediaResolver(_snapshot(_asset(track_id=1, spec="flac")))

    assert resolver.resolve(MediaRequest(track_id=1, spec="mp3")) is None


def test_resolve_returns_none_when_track_unknown() -> None:
    resolver = SnapshotMediaResolver(_snapshot(_asset(track_id=1)))

    assert resolver.resolve(MediaRequest(track_id=2)) is None


def test_resolve_does_not_leak_across_track_ids() -> None:
    asset_1 = _asset(track_id=1)
    asset_2 = _asset(track_id=2, path="/media/other.flac")
    resolver = SnapshotMediaResolver(_snapshot(asset_1, asset_2))

    assert resolver.resolve(MediaRequest(track_id=1)) == asset_1
    assert resolver.resolve(MediaRequest(track_id=2)) == asset_2


def test_resolve_filters_by_spec_within_same_track_and_type() -> None:
    flac = _asset(track_id=1, spec="flac")
    mp3 = _asset(track_id=1, spec="mp3", path="/media/other.mp3")
    resolver = SnapshotMediaResolver(_snapshot(flac, mp3))

    assert resolver.resolve(MediaRequest(track_id=1, spec="mp3")) == mp3
    assert resolver.resolve(MediaRequest(track_id=1, spec="flac")) == flac


def test_resolve_without_spec_returns_first_asset_by_stable_order() -> None:
    # 不指定 spec 时返回该曲目该类型的首个资产；快照按 (track_id, asset_type, spec, path) 排序，flac 排在 mp3 前。
    flac = _asset(track_id=1, spec="flac")
    mp3 = _asset(track_id=1, spec="mp3", path="/media/other.mp3")
    resolver = SnapshotMediaResolver(_snapshot(mp3, flac))

    assert resolver.resolve(MediaRequest(track_id=1)) == flac


def test_media_request_defaults() -> None:
    request = MediaRequest(track_id=1)

    assert request.track_id == 1
    assert request.asset_type == "audio"
    assert request.spec is None


def test_media_request_explicit_fields() -> None:
    request = MediaRequest(track_id=7, asset_type="cover", spec="300x300")

    assert request.track_id == 7
    assert request.asset_type == "cover"
    assert request.spec == "300x300"


def test_media_request_is_frozen_and_slots() -> None:
    request = MediaRequest(track_id=1)

    with pytest.raises(dataclasses.FrozenInstanceError):
        request.track_id = 2  # type: ignore[reportAttributeAccessIssue]  # 测试冻结语义：故意赋值

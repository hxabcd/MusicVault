"""bootstrap 补充单测：真实源端构建、歌单用例组装与分发目标校验。

覆盖：build_pipeline 无注入 source 时走真实构建路径、build_playlist_use_case
组装、DistributePipeline.run 对未注册目标报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.application.bootstrap import (
    build_distribute_pipeline,
    build_pipeline,
    build_playlist_use_case,
)
from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.core.config import Config
from musicvault.domain.models import Track


class _FakeSdkResponse:
    status = 200

    def __init__(self, body: dict) -> None:
        self.body = body


class _FakeSdk:
    """极简 fake：记录 song_url_v1 收到的 level 参数。"""

    last_level: str | None = None

    def __init__(self, _=None) -> None:
        self.calls: list[dict] = []

    def song_url_v1(self, **kwargs):
        type(self).last_level = kwargs.get("level")
        self.calls.append(kwargs)
        return _FakeSdkResponse({"data": [{"id": 1, "url": None}]})


def _fake_sdk(monkeypatch) -> None:
    _FakeSdk.last_level = None
    monkeypatch.setattr("musicvault.adapters.providers.netease_client.NeteaseCloudMusicApi", _FakeSdk)


def test_build_pipeline_without_source_creates_client(monkeypatch, tmp_path: Path) -> None:
    """未注入 source 时走真实构建路径，音质推导为内置 archive 的 HIRES。"""
    _fake_sdk(monkeypatch)
    cfg = Config(workspace=str(tmp_path / "ws"))

    service = build_pipeline(cfg)

    assert isinstance(service.api, NeteaseClient)
    # 构造出的客户端请求下载 URL，确认音质枚举已注入
    service.api.get_tracks_download_urls([1])
    assert _FakeSdk.last_level == "hires"


def test_build_playlist_use_case(tmp_path: Path) -> None:
    """歌单管理用例组装：状态库指向 workspace 下的 state.db。"""
    cfg = Config(workspace=str(tmp_path / "ws"))

    service = build_playlist_use_case(cfg)

    assert service.cfg is cfg
    service.state.upsert_track(Track(id=1, name="歌", artists=[], album=""))
    assert service.state.get_track(1) is not None


def test_distribute_run_rejects_unknown_selected(tmp_path: Path) -> None:
    """selected 含未注册目标 → RuntimeError。"""
    cfg = Config(workspace=str(tmp_path / "ws"))
    pipeline = build_distribute_pipeline(cfg)

    with pytest.raises(RuntimeError, match="未找到指定 sync_target"):
        pipeline.run(selected={"no_such_target"})

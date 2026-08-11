from __future__ import annotations

from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.ports.source import SourceClient


def test_netease_client_satisfies_source_client_port() -> None:
    """真实 SDK 适配器必须满足 SourceClient 协议的全部方法签名。"""
    required = {
        "login_with_cookie",
        "get_playlist_info",
        "get_playlist_tracks",
        "get_tracks_detail",
        "get_track_detail",
        "get_tracks_download_urls",
        "get_album_info",
        "get_track_lyrics",
    }
    assert required <= set(dir(NeteaseClient))


def test_fake_satisfies_source_client_port() -> None:
    """用例的测试接缝：鸭子类型 fake 也可满足端口（运行时无强制校验）。"""

    class FakeSource:
        def login_with_cookie(self, cookie: str) -> None:
            return None

        def get_playlist_info(self, playlist_id: int) -> dict:
            return {"name": "x", "track_count": 0}

        def get_playlist_tracks(self, playlist_id: int) -> list:
            return []

        def get_tracks_detail(self, track_ids: list[int]) -> dict:
            return {}

        def get_track_detail(self, track_id: int) -> None:
            return None

        def get_tracks_download_urls(self, track_ids: list[int]) -> dict:
            return {}

        def get_album_info(self, album_id: int) -> dict:
            return {}

        def get_track_lyrics(self, track_id: int) -> dict:
            return {}

    _: SourceClient = FakeSource()

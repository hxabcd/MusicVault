"""NeteaseClient 单元测试：mock 掉真实 SDK（pymusiclibrary）"""

from __future__ import annotations

import threading

import pytest

from musicvault.adapters.providers.netease_client import (
    NcmApiError,
    NeteaseClient,
    _parse_cookie_str,
)


class FakeResponse:
    """模拟 SDK Response：status + body（data 为 body 别名）"""

    def __init__(self, body: dict, status: int = 200):
        self.body = body
        self.status = status

    @property
    def data(self) -> dict:
        return self.body


class FakeNeteaseCloudMusicApi:
    """可编程 fake：按方法名返回预设响应，并记录每次调用参数"""

    responses: dict[str, FakeResponse] = {}
    instances: list["FakeNeteaseCloudMusicApi"] = []

    def __init__(self, env=None):
        self.env = env
        self.cookie: dict | None = None
        self.calls: list[tuple[str, dict]] = []
        type(self).instances.append(self)

    def set_cookie(self, cookie: dict) -> None:
        self.cookie = cookie

    def _respond(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        resp = self.responses.get(name)
        if callable(resp):  # 支持按请求参数动态生成响应
            return resp(**kwargs)
        return resp or FakeResponse({})

    def login_cellphone(self, **kwargs):
        return self._respond("login_cellphone", **kwargs)

    def login(self, **kwargs):
        return self._respond("login", **kwargs)

    def captcha_sent(self, **kwargs):
        return self._respond("captcha_sent", **kwargs)

    def login_qr_key(self, **kwargs):
        return self._respond("login_qr_key", **kwargs)

    def verify_qrcodestatus(self, **kwargs):
        return self._respond("verify_qrcodestatus", **kwargs)

    def login_status(self, **kwargs):
        return self._respond("login_status", **kwargs)

    def user_playlist(self, **kwargs):
        return self._respond("user_playlist", **kwargs)

    def playlist_detail(self, **kwargs):
        return self._respond("playlist_detail", **kwargs)

    def playlist_track_all(self, **kwargs):
        return self._respond("playlist_track_all", **kwargs)

    def song_url_v1(self, **kwargs):
        return self._respond("song_url_v1", **kwargs)

    def song_detail(self, **kwargs):
        return self._respond("song_detail", **kwargs)

    def album(self, **kwargs):
        return self._respond("album", **kwargs)

    def lyric_new(self, **kwargs):
        return self._respond("lyric_new", **kwargs)


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    FakeNeteaseCloudMusicApi.responses = {}
    FakeNeteaseCloudMusicApi.instances = []
    monkeypatch.setattr(
        "musicvault.adapters.providers.netease_client.NeteaseCloudMusicApi",
        FakeNeteaseCloudMusicApi,
    )
    yield


def test_parse_cookie_str():
    assert _parse_cookie_str("MUSIC_U=abc; __csrf=123") == {"MUSIC_U": "abc", "__csrf": "123"}
    # 兼容 ";;" 分隔（SDK 登录响应格式）
    assert _parse_cookie_str("MUSIC_U=abc;; __csrf=123") == {"MUSIC_U": "abc", "__csrf": "123"}
    assert _parse_cookie_str("") == {}


def test_login_with_cookie_injects_cookie():
    FakeNeteaseCloudMusicApi.responses["login_status"] = FakeResponse(
        {"data": {"profile": {"userId": 42, "nickname": "tester"}}}
    )
    client = NeteaseClient()
    result = client.login_with_cookie("MUSIC_U=abc; __csrf=123")
    assert result.user_id == 42
    assert result.nickname == "tester"
    api = FakeNeteaseCloudMusicApi.instances[0]
    assert api.cookie == {"MUSIC_U": "abc", "__csrf": "123"}


def test_login_via_phone_passes_countrycode_and_extracts_cookie():
    FakeNeteaseCloudMusicApi.responses["login_cellphone"] = FakeResponse(
        {"code": 200, "profile": {"userId": 7, "nickname": "u7"}, "cookie": "MUSIC_U=full; __csrf=c"}
    )
    client = NeteaseClient()
    result = client.login_via_phone(phone="13800138000", password="pw")
    assert result.user_id == 7
    assert result.nickname == "u7"
    name, kwargs = FakeNeteaseCloudMusicApi.instances[0].calls[0]
    assert name == "login_cellphone"
    assert kwargs == {"phone": "13800138000", "password": "pw", "captcha": "", "countrycode": 86}
    assert client.extract_cookie() == "MUSIC_U=full; __csrf=c"


def test_get_login_status_account_fallback():
    FakeNeteaseCloudMusicApi.responses["login_status"] = FakeResponse(
        {"data": {"account": {"userId": "99", "nickname": "acc"}}}
    )
    result = NeteaseClient().get_login_status()
    assert result.user_id == 99
    assert result.nickname == "acc"


def test_non_200_raises_ncm_api_error(monkeypatch):
    FakeNeteaseCloudMusicApi.responses["login_status"] = FakeResponse({}, status=500)
    monkeypatch.setattr(
        "musicvault.adapters.providers.netease_client._retry_api",
        lambda f, *a, **k: f(*a, **k),
    )
    with pytest.raises(NcmApiError):
        NeteaseClient().get_login_status()


def test_get_playlist_info_extracts_fields():
    FakeNeteaseCloudMusicApi.responses["playlist_detail"] = FakeResponse(
        {"playlist": {"id": 10, "name": "测试歌单", "trackCount": 3}}
    )
    info = NeteaseClient().get_playlist_info(10)
    assert info == {"id": 10, "name": "测试歌单", "track_count": 3}
    assert FakeNeteaseCloudMusicApi.instances[0].calls[0][1] == {"id": 10}


def test_get_playlist_tracks_falls_back_to_track_all():
    FakeNeteaseCloudMusicApi.responses["playlist_detail"] = FakeResponse({"playlist": {}})
    FakeNeteaseCloudMusicApi.responses["playlist_track_all"] = FakeResponse(
        {"songs": [{"id": 1, "name": "歌1", "ar": [{"id": 1, "name": "艺人A"}]}]}
    )
    tracks = NeteaseClient().get_playlist_tracks(10)
    assert [t.id for t in tracks] == [1]
    call_names = [c[0] for c in FakeNeteaseCloudMusicApi.instances[0].calls]
    assert call_names == ["playlist_detail", "playlist_track_all"]


def test_get_playlist_tracks_large_playlist_completes_via_trackids():
    """大歌单（trackCount > 内联 tracks 数）按 trackIds 分块补全，保持歌单顺序"""
    n = 501
    FakeNeteaseCloudMusicApi.responses["playlist_detail"] = FakeResponse(
        {
            "playlist": {
                "trackCount": n,
                # 服务器仅内联 20 首
                "tracks": [
                    {"id": i, "name": f"t{i}", "ar": [{"id": 1, "name": "A"}], "al": {"id": 2, "name": "Al"}}
                    for i in range(1, 21)
                ],
                "trackIds": [{"id": i} for i in range(1, n + 1)],
            }
        }
    )

    def fake_song_detail(**kwargs):
        ids = [int(x) for x in kwargs["ids"].split(",")]
        return FakeResponse(
            {
                "songs": [
                    {"id": i, "name": f"s{i}", "ar": [{"id": 1, "name": "A"}], "al": {"id": 2, "name": "Al"}}
                    for i in ids
                ]
            }
        )

    FakeNeteaseCloudMusicApi.responses["song_detail"] = fake_song_detail
    tracks = NeteaseClient().get_playlist_tracks(10)
    assert len(tracks) == n
    assert [t.id for t in tracks] == list(range(1, n + 1))
    # 分块调用：501 首按 500 分块 -> 2 次 song_detail
    detail_calls = [c for c in FakeNeteaseCloudMusicApi.instances[0].calls if c[0] == "song_detail"]
    assert len(detail_calls) == 2
    assert detail_calls[0][1] == {"ids": ",".join(str(i) for i in range(1, 501))}


def test_get_tracks_download_urls_chunks_and_joins():
    client = NeteaseClient(api_download_url_chunk_size=2)
    FakeNeteaseCloudMusicApi.responses["song_url_v1"] = FakeResponse(
        {"data": [{"id": 1, "url": "http://x/1.mp3"}, {"id": 2, "url": None}]}
    )
    urls = client.get_tracks_download_urls([1, 2, 3])
    assert urls == {1: "http://x/1.mp3", 2: None, 3: None}
    calls = FakeNeteaseCloudMusicApi.instances[0].calls
    assert calls[0][1] == {"id": "1,2", "level": "hires"}
    assert calls[1][1] == {"id": "3", "level": "hires"}


def test_get_tracks_detail_comma_join_and_track():
    client = NeteaseClient()
    FakeNeteaseCloudMusicApi.responses["song_detail"] = FakeResponse(
        {"songs": [{"id": 5, "name": "歌5", "ar": [{"id": 2, "name": "B"}], "al": {"id": 3, "name": "专辑"}}]}
    )
    details = client.get_tracks_detail([5])
    assert details[5].name == "歌5"
    assert FakeNeteaseCloudMusicApi.instances[0].calls[0][1] == {"ids": "5"}


def test_get_album_info():
    FakeNeteaseCloudMusicApi.responses["album"] = FakeResponse({"album": {"publishTime": 123}})
    body = NeteaseClient().get_album_info(3)
    assert body["album"]["publishTime"] == 123


def test_get_track_lyrics_six_fields():
    body = {key: {"lyric": f"text-{key}"} for key in ("lrc", "tlyric", "romalrc", "yrc", "ytlrc", "yromalrc")}
    FakeNeteaseCloudMusicApi.responses["lyric_new"] = FakeResponse(body)
    lyrics = NeteaseClient().get_track_lyrics(9)
    assert set(lyrics) == {"lrc", "tlyric", "romalrc", "yrc", "ytlrc", "yromalrc"}
    assert lyrics["yrc"] == "text-yrc"
    assert FakeNeteaseCloudMusicApi.instances[0].calls[0][1] == {"id": 9}


def test_get_qrcode_unikey_and_url():
    FakeNeteaseCloudMusicApi.responses["login_qr_key"] = FakeResponse({"data": {"unikey": "abc123"}})
    client = NeteaseClient()
    assert client.get_qrcode_unikey() == "abc123"
    assert client.get_qrcode_url("abc123") == "https://music.163.com/login?codekey=abc123"


def test_check_qrcode_returns_code_and_captures_cookie():
    FakeNeteaseCloudMusicApi.responses["verify_qrcodestatus"] = FakeResponse({"code": 803, "cookie": "MUSIC_U=qr"})
    client = NeteaseClient()
    assert client.check_qrcode("unikey1") == 803
    assert client.extract_cookie() == "MUSIC_U=qr"
    assert FakeNeteaseCloudMusicApi.instances[0].calls[0][1] == {"qr": "unikey1"}


def test_send_sms_code_ok_and_fail():
    client = NeteaseClient()
    FakeNeteaseCloudMusicApi.responses["captcha_sent"] = FakeResponse({"code": 200})
    assert client.send_sms_code("13800138000") is True
    FakeNeteaseCloudMusicApi.responses["captcha_sent"] = FakeResponse({"code": 400})
    assert client.send_sms_code("13800138000") is False


def test_thread_local_instances_are_isolated():
    FakeNeteaseCloudMusicApi.responses["login_status"] = FakeResponse(
        {"data": {"profile": {"userId": 1, "nickname": "n"}}}
    )
    client = NeteaseClient()

    def worker():
        client.get_login_status()
        client.get_login_status()  # 同线程应复用同一实例

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(FakeNeteaseCloudMusicApi.instances) == 2
    assert all(len(i.calls) == 2 for i in FakeNeteaseCloudMusicApi.instances)

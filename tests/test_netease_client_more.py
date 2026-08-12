"""NeteaseClient 补充单测：异常路径、返回值边界与重试语义。

覆盖：_parse_cookie_str 无等号片段、_retry_api 重试/耗尽、登录与二维码登录的
失败分支、歌单与曲目接口的空响应/缺字段容错、下载 URL 响应边界。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from musicvault.adapters.providers import netease_client as client_module
from musicvault.adapters.providers.netease_client import (
    NcmApiError,
    NeteaseClient,
    _parse_cookie_str,
    _retry_api,
)


class FakeResponse:
    """模拟 SDK Response：status + body（data 为 body 别名）"""

    def __init__(self, body: dict, status: int = 200):
        self.body = body
        self.status = status
        self.cookies: str | None = None

    @property
    def data(self) -> dict:
        return self.body


class FakeNeteaseCloudMusicApi:
    """可编程 fake：按方法名返回预设响应，并记录每次调用参数"""

    responses: dict[str, FakeResponse | Callable[..., FakeResponse]] = {}
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


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """重试退避与轮询间隔在测试中全部跳过。"""
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)


# -- _parse_cookie_str 边界 --------------------------------------------------


def test_parse_cookie_str_skips_chunk_without_equals() -> None:
    """无 "=" 的片段被跳过，带空 key 的片段同样被跳过。"""
    assert _parse_cookie_str("MUSIC_U=abc; junk; =orphan") == {"MUSIC_U": "abc"}


# -- _retry_api 重试语义 -----------------------------------------------------


def test_retry_api_succeeds_after_transient_failure(monkeypatch) -> None:
    """首次调用抛 NcmApiError，退避后重试成功。"""
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise NcmApiError("临时失败")
        return "ok"

    result = _retry_api(flaky)
    assert result == "ok"
    assert len(calls) == 2


def test_retry_api_exhausts_retries_and_raises(monkeypatch) -> None:
    """连续失败重试 _API_RETRIES 次后抛出原始异常。"""
    calls: list[int] = []

    def always_fail(*args, **kwargs):
        calls.append(1)
        raise NcmApiError("一直失败")

    with pytest.raises(NcmApiError, match="一直失败"):
        _retry_api(always_fail)
    assert len(calls) == client_module._API_RETRIES


def test_retry_api_retries_oserror_and_timeout_error(monkeypatch) -> None:
    """OSError / TimeoutError 同样进入重试语义。"""
    for error_type in (OSError, TimeoutError):
        calls: list[int] = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise error_type("临时故障")
            return "ok"

        assert _retry_api(flaky) == "ok"
        assert len(calls) == 2


# -- 登录异常路径 ------------------------------------------------------------


def test_login_with_cookie_missing_profile_raises() -> None:
    """登录响应缺少 userId → NcmApiError（覆盖 _apply_login_response 失败分支）。"""
    FakeNeteaseCloudMusicApi.responses["login_cellphone"] = FakeResponse({"code": 200, "profile": {}})

    with pytest.raises(NcmApiError, match="登录失败"):
        NeteaseClient().login_via_phone(phone="13800138000", password="pw")


def test_login_via_phone_reads_data_profile() -> None:
    """profile 位于 data 下的响应也能提取 userId。"""
    FakeNeteaseCloudMusicApi.responses["login_cellphone"] = FakeResponse(
        {"data": {"profile": {"userId": 8, "nickname": "d8"}}, "cookie": "MUSIC_U=inner"}
    )
    result = NeteaseClient().login_via_phone(phone="1", password="2")
    assert result.user_id == 8
    assert result.nickname == "d8"


def test_login_via_email() -> None:
    """邮箱登录参数透传与登录态提取。"""
    FakeNeteaseCloudMusicApi.responses["login"] = FakeResponse({"profile": {"userId": 5, "nickname": "e5"}})
    result = NeteaseClient().login_via_email("a@b.c", "pw")
    assert result.user_id == 5
    name, kwargs = FakeNeteaseCloudMusicApi.instances[0].calls[0]
    assert name == "login"
    assert kwargs == {"email": "a@b.c", "password": "pw"}


def test_send_sms_code_returns_false_on_exception() -> None:
    """SDK 抛异常时 send_sms_code 吞掉错误返回 False。"""

    def boom(**kwargs):
        raise OSError("网络故障")

    FakeNeteaseCloudMusicApi.responses["captcha_sent"] = boom
    assert NeteaseClient().send_sms_code("13800138000") is False


def test_get_qrcode_unikey_missing_raises() -> None:
    """二维码响应缺少 unikey → NcmApiError。"""
    FakeNeteaseCloudMusicApi.responses["login_qr_key"] = FakeResponse({"data": {}})
    with pytest.raises(NcmApiError, match="二维码令牌"):
        NeteaseClient().get_qrcode_unikey()


def test_get_login_status_missing_user_id_raises() -> None:
    """登录态响应无 userId → NcmApiError。"""
    FakeNeteaseCloudMusicApi.responses["login_status"] = FakeResponse({"data": {}})
    with pytest.raises(NcmApiError, match="登录态无效"):
        NeteaseClient().get_login_status()


# -- 二维码轮询 --------------------------------------------------------------


def test_poll_qrcode_success() -> None:
    """803 表示登录成功：读取登录态返回。"""
    FakeNeteaseCloudMusicApi.responses["verify_qrcodestatus"] = FakeResponse({"code": 803, "cookie": "MUSIC_U=q"})
    FakeNeteaseCloudMusicApi.responses["login_status"] = FakeResponse(
        {"data": {"profile": {"userId": 1, "nickname": "n1"}}}
    )
    result = NeteaseClient().poll_qrcode("unikey", timeout=10)
    assert result.user_id == 1
    assert result.nickname == "n1"


def test_poll_qrcode_expired_raises() -> None:
    """800 表示二维码过期 → RuntimeError。"""
    FakeNeteaseCloudMusicApi.responses["verify_qrcodestatus"] = FakeResponse({"code": 800})
    with pytest.raises(RuntimeError, match="过期"):
        NeteaseClient().poll_qrcode("unikey", timeout=10)


def test_poll_qrcode_times_out(monkeypatch) -> None:
    """一直 801（等待扫码）直到超过 deadline → TimeoutError。"""
    FakeNeteaseCloudMusicApi.responses["verify_qrcodestatus"] = FakeResponse({"code": 801})
    ticks = {"n": 0}

    def fake_monotonic() -> float:
        ticks["n"] += 1
        return 0.0 if ticks["n"] < 3 else 11.0

    monkeypatch.setattr(client_module.time, "monotonic", fake_monotonic)
    with pytest.raises(TimeoutError, match="超时"):
        NeteaseClient().poll_qrcode("unikey", timeout=10)


# -- 歌单接口边界 ------------------------------------------------------------


def test_list_user_playlists_empty_body_returns_empty() -> None:
    """空响应回退空列表。"""
    FakeNeteaseCloudMusicApi.responses["user_playlist"] = FakeResponse({})
    assert NeteaseClient().list_user_playlists(1) == []


def test_list_user_playlists_data_fallback() -> None:
    """playlist 位于 data.playlist 时同样返回。"""
    FakeNeteaseCloudMusicApi.responses["user_playlist"] = FakeResponse({"data": {"playlist": [{"id": 1}]}})
    assert NeteaseClient().list_user_playlists(1) == [{"id": 1}]


# -- 曲目下载 URL 边界 -------------------------------------------------------


def test_get_track_download_url_single() -> None:
    """单曲下载 URL 转发到批量接口。"""
    FakeNeteaseCloudMusicApi.responses["song_url_v1"] = FakeResponse({"data": [{"id": 1, "url": "http://x/1.mp3"}]})
    assert NeteaseClient().get_track_download_url(1) == "http://x/1.mp3"


def test_get_tracks_download_urls_empty_returns_empty() -> None:
    """空 ID 列表不发起请求（不创建 SDK 实例）。"""
    client = NeteaseClient()
    assert client.get_tracks_download_urls([]) == {}
    assert FakeNeteaseCloudMusicApi.instances == []


def test_download_urls_data_as_dict() -> None:
    """data 为单个 dict 时兼容包装为列表。"""
    FakeNeteaseCloudMusicApi.responses["song_url_v1"] = FakeResponse({"data": {"id": 1, "url": "http://x/1.mp3"}})
    assert NeteaseClient().get_tracks_download_urls([1]) == {1: "http://x/1.mp3"}


def test_download_urls_skips_item_without_id() -> None:
    """缺 id 的 data 项被跳过。"""
    FakeNeteaseCloudMusicApi.responses["song_url_v1"] = FakeResponse(
        {"data": [{"url": "http://x/no-id.mp3"}, {"id": 2, "url": "http://x/2.mp3"}]}
    )
    urls = NeteaseClient().get_tracks_download_urls([2])
    assert urls == {2: "http://x/2.mp3"}


def test_download_urls_skips_invalid_track_id() -> None:
    """id 无法转 int 的 data 项被跳过（保留 None 占位）。"""
    FakeNeteaseCloudMusicApi.responses["song_url_v1"] = FakeResponse({"data": [{"id": "abc", "url": "http://x/a.mp3"}]})
    assert NeteaseClient().get_tracks_download_urls([1]) == {1: None}


# -- 曲目详情边界 ------------------------------------------------------------


def test_get_track_detail_single() -> None:
    """单曲详情转发到批量接口。"""
    FakeNeteaseCloudMusicApi.responses["song_detail"] = FakeResponse(
        {"songs": [{"id": 5, "name": "歌5", "ar": [{"id": 2, "name": "B"}], "al": {"id": 3, "name": "专辑"}}]}
    )
    track = NeteaseClient().get_track_detail(5)
    assert track is not None
    assert track.name == "歌5"


def test_get_tracks_detail_empty_returns_empty() -> None:
    """空 ID 列表不发起请求（不创建 SDK 实例）。"""
    client = NeteaseClient()
    assert client.get_tracks_detail([]) == {}
    assert FakeNeteaseCloudMusicApi.instances == []


def test_get_tracks_detail_skips_song_without_id() -> None:
    """无 id 的 song 被跳过。"""
    FakeNeteaseCloudMusicApi.responses["song_detail"] = FakeResponse(
        {"songs": [{"name": "无ID"}, {"id": 7, "name": "有ID", "ar": [], "al": {}}]}
    )
    details = NeteaseClient().get_tracks_detail([7])
    assert list(details) == [7]


def test_get_tracks_detail_data_fallback() -> None:
    """songs 位于 data.songs 时同样解析。"""
    FakeNeteaseCloudMusicApi.responses["song_detail"] = FakeResponse(
        {"data": {"songs": [{"id": 8, "name": "歌8", "ar": [], "al": {}}]}}
    )
    details = NeteaseClient().get_tracks_detail([8])
    assert details[8].name == "歌8"

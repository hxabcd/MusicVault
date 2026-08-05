# pyncm → pymusiclibrary 替换 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `pyncm` 依赖替换为 `pymusiclibrary`（NeteaseCloudMusic_PythonSDK 的 Python 绑定），新增 `NeteaseClient` 适配层，保持全部调用方行为不变。

**Architecture:** 新建 `adapters/providers/netease_client.py` 封装 `MusicLibrary.neteaseCloudMusicApi.NeteaseCloudMusicApi`，公开接口与旧 `PyncmClient` 完全一致（20 个方法 + `LoginResult` 类型）。SDK 的 API 对象不能跨线程使用，因此客户端用 `threading.local` 按线程懒创建 SDK 实例并在创建时注入 cookie。调用方（5 个文件）仅改 import。

**Tech Stack:** Python 3.12、pymusiclibrary>=0.0.4（PyPI，含 win_amd64 abi3 预编译 wheel）、pytest、ruff。

## Global Constraints

- 依赖变更：`pyproject.toml` dependencies 中移除 `"pyncm>=1.7.1"`，添加 `"pymusiclibrary>=0.0.4"`。
- `NeteaseClient` 公开方法签名与返回语义与旧 `PyncmClient` 完全一致（含默认参数值）。
- 线程安全：SDK 实例必须每线程独立（`threading.local`），不得跨线程共享。
- SDK 响应校验：`resp.status != 200` 即抛 `NcmApiError`（RuntimeError 子类）；`_retry_api` 捕获 `(NcmApiError, OSError, TimeoutError)` 重试 3 次（退避 0/1/3 秒）。
- 批量接口用逗号拼接：`song_detail(ids="1,2,3")`、`song_url_v1(id="1,2,3", level=...)`。
- 质量词汇 `standard|higher|exhigh|hires|lossless` 直接透传 SDK `level`，不做映射；舍弃 pyncm 的 `encodeType="flac"`。
- 注释与用户向文案用中文；ruff line-length=120。
- **不要**触碰 `.python-version`（用户未提交的 3.11→3.12 改动保持原样）。`pyproject.toml` 中用户未提交的 `requires-python>=3.12` 改动会随本任务的 commit 一起进入版本库（同一文件，可接受，在 commit message 中注明）。
- 提交只 add 本任务相关文件。

---

### Task 1: 切换依赖并安装验证

**Files:**
- Modify: `pyproject.toml`（dependencies 数组第 8 行）
- Modify: `uv.lock`（由 uv 自动更新）

**Interfaces:**
- Produces: 环境可 `from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi, NcmProcessEnv` 导入（engine.dll 加载成功）。

- [ ] **Step 1: 修改 pyproject.toml 依赖**

将 dependencies 数组中的：

```toml
    "pyncm>=1.7.1",
```

替换为：

```toml
    "pymusiclibrary>=0.0.4",
```

（保持缩进与格式一致；不要动其他行。）

- [ ] **Step 2: 更新锁文件并安装**

Run: `uv lock && uv sync`
Expected: 成功；`uv.lock` 中 pyncm 条目消失、新增 pymusiclibrary 条目。

- [ ] **Step 3: 验证新 SDK 可导入（含原生库加载）**

Run: `python -c "from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi, NcmProcessEnv; print('sdk ok')"`
Expected: 输出 `sdk ok`（若报 DLL 加载错误，检查 wheel 是否安装了 win_amd64 版本）。

Run: `python -c "import pyncm"`（可选验证）
Expected: `ModuleNotFoundError`。

- [ ] **Step 4: 现有测试全绿**

Run: `python -m pytest tests/ -q`
Expected: 全部通过（现有 7 个测试文件与客户端无关）。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: 移除 pyncm 改用 pymusiclibrary（含 requires-python>=3.12 变更）"
```

---

### Task 2: 编写单元测试（先红后绿的目标）

**Files:**
- Create: `tests/test_netease_client.py`

**Interfaces:**
- Consumes: 尚未实现的 `musicvault.adapters.providers.netease_client` 模块（本任务运行测试时预期 ModuleNotFoundError）。
- Produces: 对 `NeteaseClient` 全部核心行为的验证（mock 掉真实 SDK）。

- [ ] **Step 1: 写测试文件**

创建 `tests/test_netease_client.py`，内容如下：

```python
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
        return self.responses.get(name) or FakeResponse({})

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
```

- [ ] **Step 2: 运行测试确认失败（红）**

Run: `python -m pytest tests/test_netease_client.py -q`
Expected: 全部报错（collect 阶段 `ModuleNotFoundError: No module named 'musicvault.adapters.providers.netease_client'`）。

- [ ] **Step 3: Commit**

```bash
git add tests/test_netease_client.py
git commit -m "test: NeteaseClient 单元测试（mock SDK，先红）"
```

---

### Task 3: 实现 netease_client.py

**Files:**
- Create: `src/musicvault/adapters/providers/netease_client.py`

**Interfaces:**
- Consumes: `musicvault.core.models.Track`；SDK 的 `NeteaseCloudMusicApi`、`NcmProcessEnv`（`from MusicLibrary.neteaseCloudMusicApi import ...`）。
- Produces: `NeteaseClient`（构造参数与旧 PyncmClient 完全一致）、`LoginResult`、`NcmApiError`、`_parse_cookie_str`、`_retry_api`、`_chunk_ids`。后续 Task 4 只需把 `PyncmClient` 标识符替换为 `NeteaseClient`、`pyncm_client` 替换为 `netease_client`。

- [ ] **Step 1: 写实现文件**

创建 `src/musicvault/adapters/providers/netease_client.py`，内容如下：

```python
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi, NcmProcessEnv

from musicvault.core.models import Track

logger = logging.getLogger(__name__)

_API_RETRIES = 3
_API_RETRY_BACKOFF = (0.0, 1.0, 3.0)
_API_CALL_GAP = 0.3


class NcmApiError(RuntimeError):
    """SDK 调用失败（HTTP 状态异常或响应解析失败）"""


def _parse_cookie_str(cookie: str) -> dict[str, str]:
    """解析 "k=v; k2=v2"（兼容 ";;" 分隔）为 dict，供 SDK set_cookie 使用"""
    result: dict[str, str] = {}
    if not cookie:
        return result
    for item in cookie.replace(";;", ";").split(";"):
        chunk = item.strip()
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key.strip():
            result[key.strip()] = value.strip()
    return result


def _retry_api(func, *args, **kwargs):
    for attempt in range(_API_RETRIES):
        if attempt > 0:
            delay = _API_RETRY_BACKOFF[min(attempt, len(_API_RETRY_BACKOFF) - 1)]
            time.sleep(delay)
        try:
            return func(*args, **kwargs)
        except (NcmApiError, OSError, TimeoutError) as exc:
            if attempt == _API_RETRIES - 1:
                raise
            logger.info("API 调用失败 (第 %s/%s 次)：%s", attempt + 1, _API_RETRIES, exc)


@dataclass(slots=True)
class LoginResult:
    """登录账号的最小信息"""

    user_id: int
    nickname: str


class NeteaseClient:
    """NeteaseCloudMusicApi（pymusiclibrary）API 访问封装

    线程安全：SDK 的 API 对象不能跨线程使用，因此按线程懒创建独立实例。
    """

    def __init__(
        self,
        text_cleaning_enabled: bool = True,
        download_quality: str = "hires",
        api_download_url_chunk_size: int = 200,
        api_track_detail_chunk_size: int = 500,
        alias_split_separators: str = "/、;；",
    ) -> None:
        self.text_cleaning_enabled = text_cleaning_enabled
        self.download_quality = download_quality
        self.api_download_url_chunk_size = api_download_url_chunk_size
        self.api_track_detail_chunk_size = api_track_detail_chunk_size
        sanitized = re.escape(alias_split_separators)
        self.alias_split_re: re.Pattern[str] = re.compile(rf"[{sanitized}]+")
        self._cookie: str = ""
        self._last_cookie: str = ""
        self._local = threading.local()

    # -- SDK 实例（线程本地） -------------------------------------------------

    def _api(self) -> NeteaseCloudMusicApi:
        """获取当前线程的 SDK 实例，懒创建并注入 cookie"""
        api = getattr(self._local, "api", None)
        if api is None:
            api = NeteaseCloudMusicApi(NcmProcessEnv())
            if self._cookie:
                api.set_cookie(_parse_cookie_str(self._cookie))
            self._local.api = api
        return api

    @staticmethod
    def _check(resp: Any) -> dict[str, Any]:
        """校验响应并返回 body dict；HTTP 状态异常抛 NcmApiError"""
        if resp.status != 200:
            raise NcmApiError(f"接口返回异常状态：{resp.status}")
        return resp.body

    # -- 登录方式 -------------------------------------------------------------

    def login_with_cookie(self, cookie: str) -> LoginResult:
        """注入 Cookie 并读取当前登录态"""
        self._cookie = cookie
        self._api().set_cookie(_parse_cookie_str(cookie))
        return self.get_login_status()

    def _apply_login_response(self, resp: Any) -> LoginResult:
        """从登录响应提取 profile 与 cookie，并记录 cookie 供 extract_cookie"""
        body = self._check(resp)
        profile = body.get("profile") or (body.get("data") or {}).get("profile") or {}
        user_id = int(profile.get("userId") or 0)
        if not user_id:
            raise NcmApiError(f"登录失败：{body}")
        self._last_cookie = body.get("cookie") or resp.cookies or ""
        return LoginResult(user_id=user_id, nickname=profile.get("nickname") or str(user_id))

    def login_via_phone(self, phone: str, password: str = "", captcha: str = "", ctcode: int = 86) -> LoginResult:
        """手机号登录（密码或验证码二选一）"""
        resp = _retry_api(
            self._api().login_cellphone,
            phone=phone,
            password=password,
            captcha=captcha,
            countrycode=ctcode,
        )
        return self._apply_login_response(resp)

    def login_via_email(self, email: str, password: str) -> LoginResult:
        """邮箱登录"""
        resp = _retry_api(self._api().login, email=email, password=password)
        return self._apply_login_response(resp)

    def send_sms_code(self, phone: str, ctcode: int = 86) -> bool:
        """发送短信验证码，返回是否发送成功"""
        try:
            resp = _retry_api(self._api().captcha_sent, phone=phone, ctcode=ctcode)
            body = self._check(resp)
            return body.get("code") == 200
        except Exception:
            return False

    def get_qrcode_unikey(self) -> str:
        """获取二维码登录的 unikey"""
        resp = _retry_api(self._api().login_qr_key)
        body = self._check(resp)
        unikey = (body.get("data") or {}).get("unikey") or body.get("unikey") or ""
        if not unikey:
            raise NcmApiError(f"获取二维码令牌失败：{body}")
        return str(unikey)

    def get_qrcode_url(self, unikey: str) -> str:
        """根据 unikey 生成二维码扫描链接"""
        return f"https://music.163.com/login?codekey={unikey}"

    def check_qrcode(self, unikey: str) -> int:
        """检测二维码登录状态：801=等待扫码, 802=已扫码待确认, 803=登录成功, 800=已过期"""
        resp = _retry_api(self._api().verify_qrcodestatus, qr=unikey)
        body = self._check(resp)
        code = int(body.get("code") or 0)
        if code == 803 and body.get("cookie"):
            self._last_cookie = body.get("cookie")
        return code

    def poll_qrcode(self, unikey: str, timeout: int = 120) -> LoginResult:
        """轮询二维码登录直到成功或超时"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.check_qrcode(unikey)
            if code == 803:
                return self.get_login_status()
            if code == 800:
                raise RuntimeError("二维码已过期，请重新获取")
            time.sleep(2)
        raise TimeoutError("二维码登录超时")

    def extract_cookie(self) -> str:
        """返回最近一次登录响应的完整 Cookie 字符串，用于持久化到配置文件"""
        return self._last_cookie

    def get_login_status(self) -> LoginResult:
        """获取当前账号登录信息"""
        resp = _retry_api(self._api().login_status)
        body = self._check(resp)
        data = body.get("data") or {}
        profile = data.get("profile") or data.get("account") or body.get("profile") or {}
        user_id = int(profile.get("userId") or 0)
        if not user_id:
            raise NcmApiError(f"登录态无效：{body}")
        nickname = profile.get("nickname") or str(user_id)
        return LoginResult(user_id=user_id, nickname=nickname)

    # -- 歌单 -----------------------------------------------------------------

    def list_user_playlists(self, user_id: int) -> list[dict[str, Any]]:
        """获取用户歌单列表"""
        resp = _retry_api(self._api().user_playlist, uid=user_id)
        body = self._check(resp)
        return body.get("playlist") or (body.get("data") or {}).get("playlist") or []

    def get_playlist_info(self, playlist_id: int) -> dict[str, Any]:
        """获取歌单基本信息（id/name/track_count）"""
        resp = _retry_api(self._api().playlist_detail, id=playlist_id)
        body = self._check(resp)
        playlist = body.get("playlist") or (body.get("data") or {}).get("playlist") or {}
        return {
            "id": playlist.get("id", playlist_id),
            "name": playlist.get("name", str(playlist_id)),
            "track_count": playlist.get("trackCount", 0),
        }

    def get_playlist_tracks(self, playlist_id: int) -> list[Track]:
        """获取歌单曲目并标准化为 Track 列表"""
        resp = _retry_api(self._api().playlist_detail, id=playlist_id)
        body = self._check(resp)
        playlist = body.get("playlist") or (body.get("data") or {}).get("playlist") or {}
        tracks = playlist.get("tracks") or []
        if not tracks:
            all_resp = _retry_api(self._api().playlist_track_all, id=playlist_id)
            all_body = self._check(all_resp)
            tracks = all_body.get("songs") or all_body.get("tracks") or []
        return [
            Track.from_ncm_payload(item, clean_text=self.text_cleaning_enabled) for item in tracks if item.get("id")
        ]

    # -- 曲目 -----------------------------------------------------------------

    def get_track_download_url(self, track_id: int) -> str | None:
        """获取单曲下载 URL"""
        return self.get_tracks_download_urls([track_id]).get(track_id)

    def get_tracks_download_urls(self, track_ids: list[int]) -> dict[int, str | None]:
        """批量获取歌曲下载 URL，返回 `track_id -> url` 映射。"""
        result: dict[int, str | None] = {int(track_id): None for track_id in track_ids}
        if not track_ids:
            return result

        for chunk in self._chunk_ids(track_ids, chunk_size=self.api_download_url_chunk_size):
            ids_csv = ",".join(str(tid) for tid in chunk)
            resp = _retry_api(self._api().song_url_v1, id=ids_csv, level=self.download_quality)
            body = self._check(resp)
            data = body.get("data") or []
            if isinstance(data, dict):
                data = [data]

            for item in data:
                track_id = item.get("id")
                if track_id is None:
                    continue
                try:
                    result[int(track_id)] = item.get("url")
                except (TypeError, ValueError):
                    continue
        return result

    def get_track_detail(self, track_id: int) -> Track | None:
        """获取单曲详情"""
        return self.get_tracks_detail([track_id]).get(track_id)

    def get_tracks_detail(self, track_ids: list[int]) -> dict[int, Track]:
        """批量获取歌曲详情，返回 `track_id -> Track` 映射。"""
        result: dict[int, Track] = {}
        if not track_ids:
            return result

        for chunk in self._chunk_ids(track_ids, chunk_size=self.api_track_detail_chunk_size):
            ids_csv = ",".join(str(tid) for tid in chunk)
            resp = _retry_api(self._api().song_detail, ids=ids_csv)
            body = self._check(resp)
            songs = body.get("songs") or (body.get("data") or {}).get("songs") or []

            for song in songs:
                if not song.get("id"):
                    continue
                track = Track.from_ncm_payload(
                    song, clean_text=self.text_cleaning_enabled, alias_split_re=self.alias_split_re
                )
                result[track.id] = track
        return result

    def get_album_info(self, album_id: int) -> dict[str, Any]:
        """获取专辑信息"""
        resp = _retry_api(self._api().album, id=album_id)
        return self._check(resp)

    def get_track_lyrics(self, track_id: int) -> dict[str, str]:
        """获取歌词数据（原文/翻译/罗马音/逐字）"""
        time.sleep(_API_CALL_GAP)
        resp = _retry_api(self._api().lyric_new, id=track_id)
        body = self._check(resp)

        def _lyric(key: str) -> str:
            return (body.get(key) or {}).get("lyric", "")

        return {
            "lrc": _lyric("lrc"),
            "tlyric": _lyric("tlyric"),
            "romalrc": _lyric("romalrc"),
            "yrc": _lyric("yrc"),
            "ytlrc": _lyric("ytlrc"),
            "yromalrc": _lyric("yromalrc"),
        }

    @staticmethod
    def _chunk_ids(track_ids: list[int], chunk_size: int) -> list[list[int]]:
        """将 ID 列表切分为固定大小批次。"""
        return [track_ids[i : i + chunk_size] for i in range(0, len(track_ids), chunk_size)]
```

- [ ] **Step 2: 运行测试确认通过（绿）**

Run: `python -m pytest tests/test_netease_client.py -q`
Expected: 全部通过（约 14 个测试）。

- [ ] **Step 3: Lint**

Run: `ruff check src/musicvault/adapters/providers/netease_client.py tests/test_netease_client.py`
Expected: 无告警（line-length=120）。

- [ ] **Step 4: Commit**

```bash
git add src/musicvault/adapters/providers/netease_client.py
git commit -m "feat: NeteaseClient 适配层替换 pyncm（线程本地 SDK 实例 + cookie 注入）"
```

---

### Task 4: 迁移调用方并删除旧文件

**Files:**
- Modify: `src/musicvault/services/run_service.py`（import 行 12、类型注解 24）
- Modify: `src/musicvault/services/sync_service.py`（import 行 9、类型注解 29）
- Modify: `src/musicvault/services/process_service.py`（import 行 16、类型注解 37）
- Modify: `src/musicvault/cli/main.py`（import 行 41 的 `_silence_libs` 元组、import+实例化 199/204、255/260、318/320）
- Modify: `src/musicvault/cli/playlist.py`（import+实例化 114/116、263/265）
- Delete: `src/musicvault/adapters/providers/pyncm_client.py`
- Modify: `CLAUDE.md` 架构注释
- Modify: `README.md` 依赖列表

**Interfaces:**
- Consumes: Task 3 的 `NeteaseClient`（构造参数、方法签名与旧类一致）。
- Produces: 全仓库不再引用 `pyncm`/`PyncmClient`/`pyncm_client`（历史文档 `docs/superpowers/plans/2026-05-03-preset-system-plan.md` 除外，不动）。

- [ ] **Step 1: 替换 5 个调用文件中的标识符**

对以下 5 个文件，把所有 `PyncmClient` 替换为 `NeteaseClient`、所有 `pyncm_client` 替换为 `netease_client`（每个文件中的 import 行、类型注解 `api: PyncmClient`、实例化 `PyncmClient(...)` 均包含在内）：

1. `src/musicvault/services/run_service.py`
2. `src/musicvault/services/sync_service.py`
3. `src/musicvault/services/process_service.py`
4. `src/musicvault/cli/main.py`（3 处 import + 3 处实例化）
5. `src/musicvault/cli/playlist.py`（2 处 import + 2 处实例化）

替换后逐文件核对：`grep -n "Pyncm\|pyncm" src/musicvault/services/run_service.py src/musicvault/services/sync_service.py src/musicvault/services/process_service.py src/musicvault/cli/main.py src/musicvault/cli/playlist.py` 应无输出。

- [ ] **Step 2: 更新 `_silence_libs` 日志静默列表**

`src/musicvault/cli/main.py` 第 40-44 行，将：

```python
def _silence_libs() -> None:
    for name in ("pyncm", "urllib3.connectionpool", "App"):
```

替换为：

```python
def _silence_libs() -> None:
    for name in ("urllib3.connectionpool", "App"):
```

（pyncm 已移除；新 SDK 不走 logging，无需静默。）

- [ ] **Step 3: 删除旧客户端文件**

Run: `git rm src/musicvault/adapters/providers/pyncm_client.py`
Expected: 文件从仓库与磁盘移除。

- [ ] **Step 4: 更新 CLAUDE.md 架构注释**

`CLAUDE.md` 中：

```markdown
adapters/providers/pyncm_client.py → pyncm 封装：登录、歌单、URL、歌词（接受下载质量、批次大小等配置）
```

替换为：

```markdown
adapters/providers/netease_client.py → pymusiclibrary（NeteaseCloudMusicApi 原生绑定）封装：登录、歌单、URL、歌词（接受下载质量、批次大小等配置）
```

- [ ] **Step 5: 更新 README.md 依赖列表**

`README.md` 中 `- \`pyncm\` — 网易云 API 封装` 替换为 `- \`pymusiclibrary\` — 网易云 API 封装（NeteaseCloudMusicApi Python 绑定）`。
（先确认该行原文，再替换。）

- [ ] **Step 6: 全量验证**

Run: `python -m pytest tests/ -q`
Expected: 全部通过（含新增 test_netease_client.py）。

Run: `ruff check src/ tests/`
Expected: 无告警。

- [ ] **Step 7: 提交**

```bash
git add -A src/musicvault CLAUDE.md README.md
git commit -m "refactor: 调用方迁移到 NeteaseClient，删除 pyncm_client"
```

（`git add -A src/musicvault` 会包含 pyncm_client.py 的删除与所有替换；CLAUDE.md/README.md 单独列出。）

---

### Task 5: 只读冒烟测试（真实网易云 API）

**Files:**
- Create（临时，不提交）: `smoke_netease.py`（仓库根目录，与 config.json 同级）
- Delete（验证后删除）

**Interfaces:**
- Consumes: Task 3 的 `NeteaseClient`；`musicvault.core.config.Config.load(path)`（`cfg.cookie` 字段）。
- Produces: 端到端联通性确认。

- [ ] **Step 1: 写临时冒烟脚本**

创建仓库根目录 `smoke_netease.py`：

```python
"""只读冒烟测试：验证 NeteaseClient 与真实网易云 API 联通性（不写任何状态文件）"""
from __future__ import annotations

import sys

from musicvault.adapters.providers.netease_client import NeteaseClient
from musicvault.core.config import Config

cfg = Config.load("config.json")
if not cfg.cookie:
    print("config.json 中没有 cookie，请先执行 msv init 登录")
    sys.exit(1)

api = NeteaseClient(download_quality="hires")

user = api.login_with_cookie(cfg.cookie)
print(f"[1] login_with_cookie OK: id={user.user_id} nickname={user.nickname}")

playlists = api.list_user_playlists(user.user_id)
print(f"[2] list_user_playlists OK: {len(playlists)} 个歌单")
if not playlists:
    print("账号没有歌单，冒烟测试提前结束")
    sys.exit(0)

pid = int(playlists[0]["id"])
info = api.get_playlist_info(pid)
print(f"[3] get_playlist_info OK: {info}")

tracks = api.get_playlist_tracks(pid)
print(f"[4] get_playlist_tracks OK: {len(tracks)} 首 (示例: {tracks[0].name} - {', '.join(tracks[0].artists)})")
if not tracks:
    print("歌单为空，冒烟测试提前结束")
    sys.exit(0)

ids = [t.id for t in tracks[:3]]
urls = api.get_tracks_download_urls(ids)
print(f"[5] get_tracks_download_urls OK: { {k: bool(v) for k, v in urls.items()} }")

details = api.get_tracks_detail(ids)
print(f"[6] get_tracks_detail OK: { {k: v.name for k, v in details.items()} }")

lyrics = api.get_track_lyrics(ids[0])
print(f"[7] get_track_lyrics OK: { {k: bool(v) for k, v in lyrics.items()} }")

print("冒烟测试全部通过")
```

- [ ] **Step 2: 运行冒烟测试**

Run: `python smoke_netease.py`（仓库根目录）
Expected: `[1]`~`[7]` 全部 OK，末尾输出「冒烟测试全部通过」。

若某一步失败，先单独调试该步（常见原因：cookie 过期 → `login_with_cookie` 抛 `NcmApiError("登录态无效...")`，需重新 `msv init` 登录；下载 URL 为空 → 非 VIP 曲目正常返回 None，属预期）。

- [ ] **Step 3: 删除临时脚本并做最终验证**

```bash
rm smoke_netease.py
python -m pytest tests/ -q
ruff check src/ tests/
```

Expected: 测试全绿、ruff 无告警。

- [ ] **Step 4: 最终提交**

若 Task 4 之后有任何额外文件变更（冒烟脚本本身不提交）：

```bash
git status
git add <变更文件>
git commit -m "chore: 冒烟测试后清理"
```

若无变更，跳过本步。

---

## 自检记录（写计划时执行）

- **Spec 覆盖**：设计文档各节 → Task 1（依赖）、Task 2/3（客户端结构与接口映射）、Task 4（调用方迁移+文档）、Task 5（冒烟验证）全部覆盖；`_silence_libs` 中的 `"pyncm"` 移除为额外发现的遗漏点，已纳入 Task 4 Step 2。
- **占位符扫描**：无 TBD/TODO；所有代码步骤含完整代码。
- **类型一致性**：`NeteaseClient` 构造参数与旧类一致；`LoginResult`/`NcmApiError`/`_parse_cookie_str`/`_retry_api`/`_chunk_ids` 在各任务间签名一致；测试中的 fake 方法名与实现调用的 SDK 方法名逐一对应（`login_cellphone`/`login`/`captcha_sent`/`login_qr_key`/`verify_qrcodestatus`/`login_status`/`user_playlist`/`playlist_detail`/`playlist_track_all`/`song_url_v1`/`song_detail`/`album`/`lyric_new`）。

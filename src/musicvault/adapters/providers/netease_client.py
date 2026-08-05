from __future__ import annotations

import logging
import re
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Any

# SDK 源码 docstring 含无效转义序列（\*），导入时会刷屏 SyntaxWarning，提前静默
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"MusicLibrary")

from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi, NcmProcessEnv  # noqa: E402

from musicvault.core.models import Track  # noqa: E402

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

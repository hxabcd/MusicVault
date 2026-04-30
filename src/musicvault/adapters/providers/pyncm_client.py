from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests
import pyncm
import pyncm.apis.login as login_api
import pyncm.apis.playlist as playlist_api
import pyncm.apis.track as track_api
import pyncm.apis.user as user_api

from musicvault.core.models import Track

logger = logging.getLogger(__name__)

_API_RETRIES = 3
_API_RETRY_BACKOFF = (0.0, 1.0, 3.0)
_DOWNLOAD_URL_CHUNK_SIZE = 200
_TRACK_DETAIL_CHUNK_SIZE = 500


@dataclass(slots=True)
class LoginResult:
    """登录账号的最小信息"""

    user_id: int
    nickname: str


def _retry_api(func, *args, **kwargs):
    for attempt in range(_API_RETRIES):
        if attempt > 0:
            delay = _API_RETRY_BACKOFF[min(attempt, len(_API_RETRY_BACKOFF) - 1)]
            time.sleep(delay)
        try:
            return func(*args, **kwargs)
        except (requests.RequestException, OSError, TimeoutError) as exc:
            if attempt == _API_RETRIES - 1:
                raise
            logger.warning("API 调用失败 (第 %s/%s 次)：%s", attempt + 1, _API_RETRIES, exc)


class PyncmClient:
    """pyncm API 访问封装"""

    def __init__(self, text_cleaning_enabled: bool = True) -> None:
        self.login_api = login_api
        self.user_api = user_api
        self.playlist_api = playlist_api
        self.track_api = track_api
        self.text_cleaning_enabled = text_cleaning_enabled

    def login_with_cookie(self, cookie: str) -> LoginResult:
        """注入 Cookie 并读取当前登录态"""
        # pyncm 通过全局会话持有 cookie，直接注入可复用现有登录态。
        session = pyncm.GetCurrentSession()
        if session is None:
            session = getattr(self.login_api, "requests", None)
        if session is None:
            raise RuntimeError("无法定位 pyncm 会话，Cookie 登录不可用")

        for chunk in cookie.split(";"):
            item = chunk.strip()
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            session.cookies.set(key.strip(), value.strip())
        return self.get_login_status()

    def get_login_status(self) -> LoginResult:
        """获取当前账号登录信息"""
        resp = self.login_api.GetCurrentLoginStatus()
        profile = (
            resp.get("profile")
            or (resp.get("data") or {}).get("profile")
            or (resp.get("data") or {}).get("account")
            or {}
        )
        user_id = int(profile.get("userId", 0))
        if not user_id:
            raise RuntimeError(f"登录态无效：{resp}")
        nickname = profile.get("nickname") or str(user_id)
        return LoginResult(user_id=user_id, nickname=nickname)

    def list_user_playlists(self, user_id: int) -> list[dict[str, Any]]:
        """获取用户歌单列表"""
        resp = _retry_api(self.user_api.GetUserPlaylists, user_id)
        return resp.get("playlist") or (resp.get("data") or {}).get("playlist") or []

    def get_playlist_tracks(self, playlist_id: int) -> list[Track]:
        """获取歌单曲目并标准化为 Track 列表"""
        resp = _retry_api(self.playlist_api.GetPlaylistInfo, playlist_id)
        playlist = resp.get("playlist") or (resp.get("data") or {}).get("playlist") or {}
        tracks = playlist.get("tracks") or []
        if not tracks:
            all_resp = _retry_api(self.playlist_api.GetPlaylistAllTracks, playlist_id)
            tracks = all_resp.get("songs") or all_resp.get("tracks") or []
        return [
            Track.from_ncm_payload(item, clean_text=self.text_cleaning_enabled) for item in tracks if item.get("id")
        ]

    def get_track_download_url(self, track_id: int) -> str | None:
        """获取单曲下载 URL"""
        return self.get_tracks_download_urls([track_id]).get(track_id)

    def get_tracks_download_urls(self, track_ids: list[int]) -> dict[int, str | None]:
        """批量获取歌曲下载 URL，返回 `track_id -> url` 映射。"""
        result: dict[int, str | None] = {int(track_id): None for track_id in track_ids}
        if not track_ids:
            return result

        for chunk in self._chunk_ids(track_ids, chunk_size=_DOWNLOAD_URL_CHUNK_SIZE):
            resp = _retry_api(self.track_api.GetTrackAudioV1, chunk, level="hires", encodeType="flac")
            data = resp.get("data") or []
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

        for chunk in self._chunk_ids(track_ids, chunk_size=_TRACK_DETAIL_CHUNK_SIZE):
            resp = _retry_api(self.track_api.GetTrackDetail, chunk)
            songs = resp.get("songs") or (resp.get("data") or {}).get("songs") or []

            for song in songs:
                if not song.get("id"):
                    continue
                track = Track.from_ncm_payload(song, clean_text=self.text_cleaning_enabled)
                result[track.id] = track
        return result

    def get_track_lyrics(self, track_id: int) -> dict[str, str]:
        """获取歌词数据（原文/翻译/逐字）"""
        resp = _retry_api(self.track_api.GetTrackLyricsNew, str(track_id))
        lrc = (resp.get("lrc") or {}).get("lyric", "")
        tlyric = (resp.get("tlyric") or {}).get("lyric", "")
        yrc = (resp.get("yrc") or {}).get("lyric", "")
        return {"lrc": lrc, "tlyric": tlyric, "yrc": yrc}

    @staticmethod
    def _chunk_ids(track_ids: list[int], chunk_size: int) -> list[list[int]]:
        """将 ID 列表切分为固定大小批次。"""
        return [track_ids[i : i + chunk_size] for i in range(0, len(track_ids), chunk_size)]

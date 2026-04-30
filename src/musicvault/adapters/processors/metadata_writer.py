from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.id3 import (
    APIC,
    COMM,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TEXT,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    USLT,
)
from mutagen.mp3 import MP3

from musicvault.core.models import Track


_COVER_FETCH_TIMEOUT = 15


class MetadataWriter:
    """音频标签写入器"""

    def __init__(self) -> None:
        # 同一轮运行内按封面 URL 复用，避免重复 HTTP 请求；失败结果不缓存，避免一次瞬时错误污染整轮。
        self._cover_cache: dict[str, bytes] = {}
        self._cover_cache_lock = threading.Lock()

    def write(
        self,
        audio_file: Path,
        track: Track,
        lyric_text: str | None = None,
        is_lossless: bool = False,
    ) -> None:
        """根据文件格式写入元数据与歌词"""
        if audio_file.suffix.lower() == ".mp3":
            self._write_mp3(audio_file, track, lyric_text, is_lossless=is_lossless)
        elif audio_file.suffix.lower() == ".flac":
            self._write_flac(audio_file, track, lyric_text, is_lossless)

    def _download_cover(self, url: str | None) -> bytes | None:
        # 封面下载失败不阻断主流程；同一 URL 成功后命中缓存。
        if not url:
            return None
        with self._cover_cache_lock:
            cached = self._cover_cache.get(url)
        if cached:
            return cached

        data = self._fetch_cover(url)
        if not data:
            return None

        with self._cover_cache_lock:
            self._cover_cache[url] = data
        return data

    @staticmethod
    def _fetch_cover(url: str) -> bytes | None:
        headers = {
            "User-Agent": "MusicVault/1.0",
            "Accept": "image/*,*/*;q=0.8",
            "Connection": "close",
        }
        # 网络抖动或偶发 5xx 时重试，4xx 直接失败。
        backoff_seconds = (0.0, 0.5, 1.5)
        for sleep_seconds in backoff_seconds:
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            req = Request(url, headers=headers, method="GET")
            try:
                with urlopen(req, timeout=_COVER_FETCH_TIMEOUT) as resp:  # nosec B310 - trusted metadata URL
                    return resp.read()
            except HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                    break
            except (URLError, TimeoutError, socket.timeout, OSError):
                pass
        return None

    def _write_mp3(self, path: Path, track: Track, lyric_text: str | None, is_lossless: bool) -> None:
        # 1. 所有 mp3 均写标题/艺术家/专辑；lossy 仅保留这三项。
        audio = MP3(str(path))
        tags = audio.tags or ID3()
        if not is_lossless:
            tags.clear()
        self._set_id3_text(tags, "TIT2", TIT2, track.name)
        self._set_id3_text(tags, "TPE1", TPE1, track.artist_text)
        self._set_id3_text(tags, "TALB", TALB, track.album)

        if is_lossless:
            # 2. lossless 额外补充扩展字段（年份、轨道号、作曲等）。
            extras = self._build_extra_metadata(track)
            self._set_id3_text(tags, "TDRC", TDRC, extras.get("year"))
            self._set_id3_text(tags, "TRCK", TRCK, extras.get("track_number"))
            self._set_id3_text(tags, "TPOS", TPOS, extras.get("disc_number"))
            self._set_id3_text(tags, "TCON", TCON, extras.get("genre"))
            self._set_id3_text(tags, "TPE2", TPE2, extras.get("album_artist"))
            self._set_id3_text(tags, "TCOM", TCOM, extras.get("composer"))
            self._set_id3_text(tags, "TEXT", TEXT, extras.get("lyricist"))
            self._set_id3_comment(tags, extras.get("comment"))

            # 3. 仅在 lossless 写嵌入歌词和封面。
            tags.delall("USLT")
            if lyric_text:
                tags.add(USLT(encoding=3, lang="eng", desc="", text=lyric_text))

            cover = self._download_cover(track.cover_url)
            if cover:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))

        # 4. 回写 ID3 标签。
        audio.tags = tags
        audio.save(v2_version=3)

    def _write_flac(self, path: Path, track: Track, lyric_text: str | None, is_lossless: bool) -> None:
        # 1. 基础字段始终写入。
        audio = FLAC(str(path))
        audio["title"] = track.name
        audio["artist"] = track.artist_text
        audio["album"] = track.album

        if is_lossless:
            # 2. lossless 写扩展字段。
            extras = self._build_extra_metadata(track)
            self._set_vorbis_text(audio, "date", extras.get("year"))
            self._set_vorbis_text(audio, "tracknumber", extras.get("track_number"))
            self._set_vorbis_text(audio, "discnumber", extras.get("disc_number"))
            self._set_vorbis_text(audio, "genre", extras.get("genre"))
            self._set_vorbis_text(audio, "albumartist", extras.get("album_artist"))
            self._set_vorbis_text(audio, "composer", extras.get("composer"))
            self._set_vorbis_text(audio, "lyricist", extras.get("lyricist"))
            self._set_vorbis_text(audio, "comment", extras.get("comment"))
        else:
            # 2. lossy 模式下清理扩展字段，避免历史脏标签残留。
            for key in (
                "date",
                "tracknumber",
                "discnumber",
                "genre",
                "albumartist",
                "composer",
                "lyricist",
                "comment",
                "description",
                "lyrics",
            ):
                if key in audio:
                    del audio[key]

        # 3. 仅在有歌词时写 lyrics，并在 lossless 增加 description。
        if lyric_text:
            # 无损路径默认保留更完整歌词内容。
            audio["lyrics"] = lyric_text
            if is_lossless:
                audio["description"] = "Synced by MusicVault"

        # 4. 按需覆盖封面并保存。
        cover = self._download_cover(track.cover_url)
        if cover:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()

    @staticmethod
    def _set_id3_text(tags: ID3, frame_id: str, frame_cls: type, value: str | None) -> None:
        tags.delall(frame_id)
        if value:
            tags.add(frame_cls(encoding=3, text=str(value)))

    @staticmethod
    def _set_id3_comment(tags: ID3, comment: str | None) -> None:
        tags.delall("COMM")
        if comment:
            tags.add(COMM(encoding=3, lang="eng", desc="", text=comment))

    @staticmethod
    def _set_vorbis_text(audio: FLAC, key: str, value: str | None) -> None:
        if value:
            audio[key] = str(value)
        elif key in audio:
            del audio[key]

    def _build_extra_metadata(self, track: Track) -> dict[str, str | None]:
        # 按网易云歌曲详情结构提取扩展元数据。
        raw = track.raw or {}
        return {
            "year": self._extract_year(raw),
            "track_number": self._extract_track_number(raw.get("no")),
            "disc_number": self._extract_disc(raw.get("cd")),
            "genre": self._extract_genre(raw.get("genre")),
            "album_artist": self._extract_album_artist(raw) or track.artist_text,
            "composer": self._extract_named_people(raw.get("composer")),
            "lyricist": self._extract_named_people(raw.get("lyricist")),
            "comment": self._extract_comment(raw, track),
        }

    def _extract_year(self, raw: dict[str, object]) -> str | None:
        ts = raw.get("publishTime")
        if ts is None:
            return None
        try:
            value = int(str(ts))
            if value > 1_000_000_000_000:
                value //= 1000
            return str(datetime.fromtimestamp(value, tz=timezone.utc).year)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _extract_track_number(no: object) -> str | None:
        if no is None:
            return None
        try:
            value = int(str(no))
        except (TypeError, ValueError):
            return None
        return str(value) if value > 0 else None

    @staticmethod
    def _extract_disc(cd: object) -> str | None:
        if cd is None:
            return None
        if isinstance(cd, int):
            return str(cd) if cd > 0 else None
        text = str(cd).strip()
        if not text:
            return None
        if "/" in text:
            text = text.split("/", 1)[0].strip()
        if text.isdigit():
            return str(int(text))
        return text

    @staticmethod
    def _extract_genre(value: object) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return "/".join(items) if items else None
        return None

    @staticmethod
    def _extract_album_artist(raw: dict[str, object]) -> str | None:
        artists = raw.get("ar")
        if not isinstance(artists, list):
            return None
        names: list[str] = []
        for item in artists:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                stripped = name.strip()
                if stripped:
                    names.append(stripped)
        return "/".join(names) if names else None

    @staticmethod
    def _extract_named_people(value: object) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str):
                text = name.strip()
                return text or None
            return None
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        stripped = name.strip()
                        if stripped:
                            names.append(stripped)
                elif isinstance(item, str):
                    stripped = item.strip()
                    if stripped:
                        names.append(stripped)
            return "/".join(names) if names else None
        return None

    @staticmethod
    def _extract_comment(raw: dict[str, object], track: Track) -> str | None:
        # 网易云优先取 tns（翻译名），再回退 alia（别名）与 Track aliases。
        tns = raw.get("tns")
        if isinstance(tns, list):
            names = [str(item).strip() for item in tns if str(item).strip()]
            if names:
                return "/".join(names)

        alia = raw.get("alia")
        if isinstance(alia, list):
            names = [str(item).strip() for item in alia if str(item).strip()]
            if names:
                return "/".join(names)

        if track.aliases:
            return "/".join(alias for alias in track.aliases if alias)
        return None

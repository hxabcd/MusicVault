from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image

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


class MetadataWriter:
    def __init__(self) -> None:
        self._cover_cache: dict[str, bytes] = {}
        self._cover_cache_lock = threading.Lock()

    def write(
        self,
        audio_file: Path,
        track: Track,
        *,
        lyric_text: str | None = None,
        embed_cover: bool = True,
        embed_lyrics: bool = True,
        cover_timeout: int = 15,
        cover_max_size: int = 0,
        metadata_fields: frozenset[str] = frozenset(),
    ) -> None:
        """写入元数据到音频文件。policy 由 caller 决定。"""
        cover_data: bytes | None = None
        if embed_cover:
            cover_data = self._download_cover(track.cover_url, cover_timeout, cover_max_size)

        if audio_file.suffix.lower() == ".mp3":
            self._write_mp3(audio_file, track, lyric_text, cover_data, embed_lyrics, metadata_fields)
        elif audio_file.suffix.lower() == ".flac":
            self._write_flac(audio_file, track, lyric_text, cover_data, embed_lyrics, metadata_fields)

    # ------------------------------------------------------------------
    # MP3
    # ------------------------------------------------------------------

    def _write_mp3(
        self, path: Path, track: Track, lyric_text: str | None,
        cover_data: bytes | None, embed_lyrics: bool, metadata_fields: frozenset[str],
    ) -> None:
        audio = MP3(str(path))
        tags = audio.tags or ID3()
        tags.clear()

        self._set_id3_text(tags, "TIT2", TIT2, track.name)
        self._set_id3_text(tags, "TPE1", TPE1, track.artist_text)
        self._set_id3_text(tags, "TALB", TALB, track.album)

        extras = self._build_extra_metadata(track, metadata_fields)
        self._set_id3_text(tags, "TDRC", TDRC, extras.get("year"))
        self._set_id3_text(tags, "TRCK", TRCK, extras.get("track_number"))
        self._set_id3_text(tags, "TPOS", TPOS, extras.get("disc_number"))
        self._set_id3_text(tags, "TCON", TCON, extras.get("genre"))
        self._set_id3_text(tags, "TPE2", TPE2, extras.get("album_artist"))
        self._set_id3_text(tags, "TCOM", TCOM, extras.get("composer"))
        self._set_id3_text(tags, "TEXT", TEXT, extras.get("lyricist"))
        self._set_id3_comment(tags, extras.get("comment"))

        if embed_lyrics and lyric_text:
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="", text=lyric_text))

        if cover_data:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))

        audio.tags = tags
        audio.save(v2_version=3)

    # ------------------------------------------------------------------
    # FLAC
    # ------------------------------------------------------------------

    def _write_flac(
        self, path: Path, track: Track, lyric_text: str | None,
        cover_data: bytes | None, embed_lyrics: bool, metadata_fields: frozenset[str],
    ) -> None:
        audio = FLAC(str(path))
        audio["title"] = track.name
        audio["artist"] = track.artist_text
        audio["album"] = track.album

        extras = self._build_extra_metadata(track, metadata_fields)
        self._set_vorbis_text(audio, "date", extras.get("year"))
        self._set_vorbis_text(audio, "tracknumber", extras.get("track_number"))
        self._set_vorbis_text(audio, "discnumber", extras.get("disc_number"))
        self._set_vorbis_text(audio, "genre", extras.get("genre"))
        self._set_vorbis_text(audio, "albumartist", extras.get("album_artist"))
        self._set_vorbis_text(audio, "composer", extras.get("composer"))
        self._set_vorbis_text(audio, "lyricist", extras.get("lyricist"))
        self._set_vorbis_text(audio, "comment", extras.get("comment"))

        if embed_lyrics and lyric_text:
            audio["lyrics"] = lyric_text
            audio["description"] = "Synced by MusicVault"

        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)

        audio.save()

    # ------------------------------------------------------------------
    # Cover download (internal, cached)
    # ------------------------------------------------------------------

    def _download_cover(self, url: str | None, timeout: int, max_size: int) -> bytes | None:
        if not url:
            return None
        with self._cover_cache_lock:
            cached = self._cover_cache.get(url)
        if cached:
            return cached

        data = self._fetch_cover(url, timeout)
        if not data:
            return None

        if max_size > 0:
            data = self._resize_cover(data, max_size)

        with self._cover_cache_lock:
            self._cover_cache[url] = data
        return data

    def _resize_cover(self, data: bytes, max_size: int) -> bytes:
        try:
            img = Image.open(BytesIO(data))
        except Exception:
            return data

        width, height = img.size
        max_dim = max(width, height)
        if max_dim <= max_size:
            return data

        ratio = max_size / max_dim
        new_size = (int(width * ratio), int(height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def _fetch_cover(self, url: str, timeout: int) -> bytes | None:
        headers = {
            "User-Agent": "MusicVault/1.0",
            "Accept": "image/*,*/*;q=0.8",
            "Connection": "close",
        }
        backoff_seconds = (0.0, 0.5, 1.5)
        for sleep_seconds in backoff_seconds:
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            req = Request(url, headers=headers, method="GET")
            try:
                with urlopen(req, timeout=timeout) as resp:  # nosec B310
                    return resp.read()
            except HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                    break
            except (URLError, TimeoutError, socket.timeout, OSError):
                pass
        return None

    # ------------------------------------------------------------------
    # Extra metadata builders
    # ------------------------------------------------------------------

    def _build_extra_metadata(self, track: Track, fields: frozenset[str]) -> dict[str, str | None]:
        raw = track.raw or {}
        extras = {
            "year": self._extract_year(raw),
            "track_number": self._extract_track_number(raw.get("no")),
            "disc_number": self._extract_disc(raw.get("cd")),
            "genre": self._extract_genre(raw.get("genre")),
            "album_artist": self._extract_album_artist(raw) or track.artist_text,
            "composer": self._extract_named_people(raw.get("composer")),
            "lyricist": self._extract_named_people(raw.get("lyricist")),
            "comment": self._extract_comment(raw, track),
        }
        if fields:
            return {k: v for k, v in extras.items() if k in fields}
        return extras

    def _extract_year(self, raw: dict[str, object]) -> str | None:
        ts = raw.get("publishTime")
        if not ts:
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

    # ------------------------------------------------------------------
    # Tag helpers
    # ------------------------------------------------------------------

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

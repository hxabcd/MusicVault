from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.domain.models import Track
from musicvault.preset_api.v1 import MetadataSpec


def _make_mp3(path: Path) -> None:
    """写入最小合法 MP3：4 个连续 MPEG1 Layer3 帧（mutagen 同步需要 ≥4 帧）。"""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame * 4)


def _make_flac(path: Path) -> None:
    """写入最小合法 FLAC：fLaC 标记 + 合法 STREAMINFO 块。"""
    streaminfo = struct.pack(">HH", 4096, 4096)  # 最小/最大 block size
    streaminfo += b"\x00" * 6  # 最小/最大 frame size
    streaminfo += ((44100 << 44) | (2 << 41) | (16 << 36)).to_bytes(8, "big")  # 采样率/声道/位深
    streaminfo += b"\x00" * 16  # MD5
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)


def _id3_key_starts(tags, prefix: str) -> list[str]:
    return [key for key in tags if key.startswith(prefix)]


@pytest.fixture
def track() -> Track:
    return Track(
        id=1,
        name="测试歌曲",
        artists=["歌手A", "歌手B"],
        album="测试专辑",
        cover_url="https://example.invalid/cover.jpg",
        raw={
            "publishTime": 1_600_000_000_000,
            "no": 3,
            "cd": 2,
            "genre": ["摇滚", "流行"],
            "ar": [{"name": "专辑艺术家"}],
            "composer": "作曲者",
            "lyricist": "作词者",
            "tns": ["别名"],
        },
    )


@pytest.fixture
def track_without_cover() -> Track:
    return Track(id=2, name="无封面歌曲", artists=["歌手"], album="专辑", cover_url=None, raw={})


@pytest.fixture
def writer() -> MetadataWriter:
    return MetadataWriter()


@pytest.fixture
def cover_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 128


class TestWriteMp3:
    def test_full_spec_writes_tags_and_cover(self, tmp_path, track, writer, cover_bytes, monkeypatch) -> None:
        audio = tmp_path / "1.mp3"
        _make_mp3(audio)
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=cover_bytes))

        writer.write(audio, track, metadata=MetadataSpec.full(), cover_timeout=15)

        tags = MP3(str(audio)).tags
        assert tags["TIT2"].text == ["测试歌曲"]
        assert tags["TPE1"].text == ["歌手A/歌手B"]
        assert tags["TALB"].text == ["测试专辑"]
        assert str(tags["TDRC"].text[0]) == "2020"
        assert tags["TRCK"].text == ["3"]
        assert tags["TPOS"].text == ["2"]
        assert tags["TCON"].text == ["摇滚/流行"]
        assert tags["TPE2"].text == ["专辑艺术家"]
        assert tags["TCOM"].text == ["作曲者"]
        assert tags["TEXT"].text == ["作词者"]
        assert tags["APIC:Cover"]
        assert not _id3_key_starts(tags, "USLT")
        # 注意：full() 的字段白名单不含 "comment"，故 COMM 不写（fields 白名单语义）

    def test_none_spec_writes_no_cover_no_extras(self, tmp_path, track_without_cover, writer) -> None:
        """MetadataSpec.none() → 无 APIC/无 year 等 extra；基础标题/艺术家/专辑仍写。

        注意：none() 的 fields=() 沿用"空集返回全部 extra"的保留语义，
        裸曲目（raw={}）下 year/曲号/流派等均为 None 不写，
        仅 album_artist 回退（TPE2=artist_text）会写出。
        """
        audio = tmp_path / "2.mp3"
        _make_mp3(audio)

        writer.write(audio, track_without_cover, metadata=MetadataSpec.none())

        tags = MP3(str(audio)).tags
        assert tags["TIT2"].text == ["无封面歌曲"]
        assert tags["TPE1"].text == ["歌手"]
        assert tags["TALB"].text == ["专辑"]
        assert tags["TPE2"].text == ["歌手"]  # album_artist 回退 = artist_text（空集=全写语义）
        assert "TDRC" not in tags
        assert "TRCK" not in tags
        assert "TPOS" not in tags
        assert "TCON" not in tags
        assert "TCOM" not in tags
        assert "TEXT" not in tags
        assert not _id3_key_starts(tags, "COMM")
        assert not _id3_key_starts(tags, "APIC")
        assert not _id3_key_starts(tags, "USLT")

    def test_none_spec_skips_cover_download(self, tmp_path, track, writer, cover_bytes, monkeypatch) -> None:
        """embed_cover=False 时即使存在封面 URL 也不下载。"""
        audio = tmp_path / "3.mp3"
        _make_mp3(audio)
        spy = Mock(return_value=cover_bytes)
        monkeypatch.setattr(writer, "_download_cover", spy)

        writer.write(audio, track, metadata=MetadataSpec.none())

        spy.assert_not_called()

    def test_basic_spec_writes_full_extras(self, tmp_path, track, writer, cover_bytes, monkeypatch) -> None:
        """MetadataSpec.basic()（fields=()）→ 现有语义：extra 全写（年份/曲号等）。"""
        audio = tmp_path / "4.mp3"
        _make_mp3(audio)
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=cover_bytes))

        writer.write(audio, track, metadata=MetadataSpec.basic())

        tags = MP3(str(audio)).tags
        assert str(tags["TDRC"].text[0]) == "2020"
        assert tags["TRCK"].text == ["3"]
        assert tags["TPOS"].text == ["2"]
        assert tags["TCON"].text == ["摇滚/流行"]
        assert tags["TPE2"].text == ["专辑艺术家"]
        assert tags["TCOM"].text == ["作曲者"]
        assert tags["TEXT"].text == ["作词者"]
        assert _id3_key_starts(tags, "COMM")
        assert "APIC:Cover" in tags

    def test_fields_subset_filters_extras(self, tmp_path, track, writer, cover_bytes, monkeypatch) -> None:
        """fields 白名单只写指定 extra。"""
        audio = tmp_path / "5.mp3"
        _make_mp3(audio)
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=cover_bytes))

        writer.write(audio, track, metadata=MetadataSpec(fields=("year", "track_number")))

        tags = MP3(str(audio)).tags
        assert str(tags["TDRC"].text[0]) == "2020"
        assert tags["TRCK"].text == ["3"]
        assert "TPOS" not in tags
        assert "TCON" not in tags


class TestWriteFlac:
    def test_full_spec_writes_tags_and_cover(self, tmp_path, track, writer, cover_bytes, monkeypatch) -> None:
        audio = tmp_path / "1.flac"
        _make_flac(audio)
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=cover_bytes))

        writer.write(audio, track, metadata=MetadataSpec.full())

        flac = FLAC(str(audio))
        assert flac["title"] == ["测试歌曲"]
        assert flac["artist"] == ["歌手A/歌手B"]
        assert flac["album"] == ["测试专辑"]
        assert flac["date"] == ["2020"]
        assert flac["tracknumber"] == ["3"]
        assert flac["discnumber"] == ["2"]
        assert flac["genre"] == ["摇滚/流行"]
        assert flac["albumartist"] == ["专辑艺术家"]
        assert flac["composer"] == ["作曲者"]
        assert flac["lyricist"] == ["作词者"]
        assert "lyrics" not in flac
        assert "description" not in flac
        assert len(flac.pictures) == 1
        # 注意：full() 的字段白名单不含 "comment"，故不写（fields 白名单语义）

    def test_none_spec_writes_no_cover_no_extras(self, tmp_path, track_without_cover, writer) -> None:
        audio = tmp_path / "2.flac"
        _make_flac(audio)

        writer.write(audio, track_without_cover, metadata=MetadataSpec.none())

        flac = FLAC(str(audio))
        assert flac["title"] == ["无封面歌曲"]
        assert flac["artist"] == ["歌手"]
        assert flac["album"] == ["专辑"]
        assert flac["albumartist"] == ["歌手"]  # album_artist 回退 = artist_text（空集=全写语义）
        assert "date" not in flac
        assert "tracknumber" not in flac
        assert "discnumber" not in flac
        assert "genre" not in flac
        assert "lyrics" not in flac
        assert "description" not in flac
        assert not flac.pictures

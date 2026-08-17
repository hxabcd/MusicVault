from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from musicvault.adapters.processors.metadata_writer import MetadataWriter
from musicvault.domain.models import Track
from musicvault.preset_api.v1 import MetadataField, MetadataSpec


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


def _id3_key_starts(tags: ID3, prefix: str) -> list[str]:
    return [key for key in tags if key.startswith(prefix)]


def _read_tags(path: Path) -> ID3:
    """读取 MP3 的 ID3 标签；无标签时失败并给出清晰断言信息。"""
    tags = MP3(str(path)).tags
    assert tags is not None, f"MP3 缺少 ID3 标签：{path}"
    return tags


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

        tags = _read_tags(audio)
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
        # full() 含 comment（所有元数据），COMM 写别名/译名
        assert tags.getall("COMM")[0].text == ["别名"]

    def test_none_spec_writes_no_cover_no_fields(self, tmp_path, track_without_cover, writer) -> None:
        """MetadataSpec.none() → 无封面、无任何字段（连标题/艺术家/专辑也不写）。"""
        audio = tmp_path / "2.mp3"
        _make_mp3(audio)

        writer.write(audio, track_without_cover, metadata=MetadataSpec.none())

        tags = _read_tags(audio)
        assert "TIT2" not in tags
        assert "TPE1" not in tags
        assert "TALB" not in tags
        assert "TPE2" not in tags
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

    def test_basic_spec_writes_only_basic_fields(self, tmp_path, track, writer, cover_bytes, monkeypatch) -> None:
        """MetadataSpec.basic() → 只写标题/艺术家/专辑，不写额外字段。"""
        audio = tmp_path / "4.mp3"
        _make_mp3(audio)
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=cover_bytes))

        writer.write(audio, track, metadata=MetadataSpec.basic())

        tags = _read_tags(audio)
        assert tags["TIT2"].text == ["测试歌曲"]
        assert tags["TPE1"].text == ["歌手A/歌手B"]
        assert tags["TALB"].text == ["测试专辑"]
        assert "TDRC" not in tags
        assert "TRCK" not in tags
        assert "TPOS" not in tags
        assert "TCON" not in tags
        assert "TPE2" not in tags
        assert "TCOM" not in tags
        assert "TEXT" not in tags
        assert not _id3_key_starts(tags, "COMM")
        assert "APIC:Cover" in tags

    def test_fields_subset_filters_extras(self, tmp_path, track, writer, cover_bytes, monkeypatch) -> None:
        """fields 位掩码只写指定字段。"""
        audio = tmp_path / "5.mp3"
        _make_mp3(audio)
        monkeypatch.setattr(writer, "_download_cover", Mock(return_value=cover_bytes))

        writer.write(audio, track, metadata=MetadataSpec(fields=MetadataField.YEAR | MetadataField.TRACK_NUMBER))

        tags = _read_tags(audio)
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
        # full() 含 comment（所有元数据），comment 写别名/译名
        assert flac["comment"] == ["别名"]

    def test_none_spec_writes_no_cover_no_fields(self, tmp_path, track_without_cover, writer) -> None:
        audio = tmp_path / "2.flac"
        _make_flac(audio)

        writer.write(audio, track_without_cover, metadata=MetadataSpec.none())

        flac = FLAC(str(audio))
        assert "title" not in flac
        assert "artist" not in flac
        assert "album" not in flac
        assert "albumartist" not in flac
        assert "date" not in flac
        assert "tracknumber" not in flac
        assert "discnumber" not in flac
        assert "genre" not in flac
        assert "lyrics" not in flac
        assert "description" not in flac
        assert not flac.pictures


class TestWriteLyrics:
    def test_mp3_writes_uslt_frame(self, tmp_path, writer) -> None:
        audio = tmp_path / "l.mp3"
        _make_mp3(audio)

        writer.write_lyrics(audio, "[00:01.00]hello\n[00:02.00]world")

        tags = _read_tags(audio)
        uslt = tags.getall("USLT")
        assert len(uslt) == 1
        assert uslt[0].text == "[00:01.00]hello\n[00:02.00]world"

    def test_flac_writes_lyrics_vorbis_comment(self, tmp_path, writer) -> None:
        audio = tmp_path / "l.flac"
        _make_flac(audio)

        writer.write_lyrics(audio, "[00:01.00]hello")

        flac = FLAC(str(audio))
        assert flac["lyrics"] == ["[00:01.00]hello"]

    def test_mp3_replaces_existing_uslt(self, tmp_path, writer) -> None:
        audio = tmp_path / "l2.mp3"
        _make_mp3(audio)
        writer.write_lyrics(audio, "old lyrics")

        writer.write_lyrics(audio, "new lyrics")

        tags = _read_tags(audio)
        uslt = tags.getall("USLT")
        assert len(uslt) == 1
        assert uslt[0].text == "new lyrics"

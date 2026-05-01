from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from musicvault.adapters.processors.lyrics import write_gb18030_lrc


def _decode_lrc_bytes(data: bytes) -> str:
    for encoding in ("gb18030", "utf-8-sig"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError("Unable to decode lrc bytes with expected fallback encodings")


def test_write_gb18030_when_supported() -> None:
    lyric = "[00:01.000]中文歌词"
    with TemporaryDirectory() as tmp:
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"")
        lrc = write_gb18030_lrc(audio, lyric)
        data = lrc.read_bytes()

    assert data.decode("gb18030") == lyric


def test_fallback_preserves_non_gb18030_characters() -> None:
    lyric = "[00:01.000]愛頼気綺麗傷準備裏時間奪噛"
    with TemporaryDirectory() as tmp:
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"")
        lrc = write_gb18030_lrc(audio, lyric)
        data = lrc.read_bytes()

    assert _decode_lrc_bytes(data) == lyric


def test_custom_encoding_order_is_respected() -> None:
    lyric = "[00:01.000]愛頼気綺麗傷準備裏時間奪噛"
    with TemporaryDirectory() as tmp:
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"")
        lrc = write_gb18030_lrc(audio, lyric, encodings=("utf-8-sig", "gb18030"))
        data = lrc.read_bytes()

    assert data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8-sig") == lyric


def test_empty_encodings_falls_back_to_defaults() -> None:
    """encodings 为空元组时触发内置回退 (覆盖 line 248)"""
    lyric = "[00:01.000]中文"
    with TemporaryDirectory() as tmp:
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"")
        lrc = write_gb18030_lrc(audio, lyric, encodings=())
        data = lrc.read_bytes()
    assert data.decode("gb18030") == lyric


def test_ascii_first_triggers_encoding_fallback() -> None:
    """首个编码失败时切换 encoding (覆盖 line 254)"""
    lyric = "[00:01.000]中文歌词"
    with TemporaryDirectory() as tmp:
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"")
        lrc = write_gb18030_lrc(audio, lyric, encodings=("ascii", "utf-8-sig"))
        data = lrc.read_bytes()
    # ASCII 失败，回退到 utf-8-sig，带 BOM
    assert data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8-sig") == lyric


def test_empty_lyric_text() -> None:
    """空歌词写入不应报错"""
    with TemporaryDirectory() as tmp:
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"")
        lrc = write_gb18030_lrc(audio, "")
        data = lrc.read_bytes()
    assert data == b""

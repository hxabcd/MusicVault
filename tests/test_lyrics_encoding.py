from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from musicvault.adapters.processors.lyrics import write_gb2312_lrc


def _decode_lrc_bytes(data: bytes) -> str:
    for encoding in ("gb2312", "gb18030", "utf-8-sig"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError("Unable to decode lrc bytes with expected fallback encodings")


class TestLyricsEncoding(unittest.TestCase):
    def test_write_gb2312_when_supported(self) -> None:
        lyric = "[00:01.000]中文歌词"
        with TemporaryDirectory() as tmp:
            audio = Path(tmp) / "song.mp3"
            audio.write_bytes(b"")
            lrc = write_gb2312_lrc(audio, lyric)
            data = lrc.read_bytes()

        self.assertEqual(data.decode("gb2312"), lyric)

    def test_fallback_preserves_non_gb2312_characters(self) -> None:
        lyric = "[00:01.000]中文ABCあいう𠮷"
        with TemporaryDirectory() as tmp:
            audio = Path(tmp) / "song.mp3"
            audio.write_bytes(b"")
            lrc = write_gb2312_lrc(audio, lyric)
            data = lrc.read_bytes()

        self.assertEqual(_decode_lrc_bytes(data), lyric)

    def test_custom_encoding_order_is_respected(self) -> None:
        lyric = "[00:01.000]中文ABCあいう𠮷"
        with TemporaryDirectory() as tmp:
            audio = Path(tmp) / "song.mp3"
            audio.write_bytes(b"")
            lrc = write_gb2312_lrc(audio, lyric, encodings=("utf-8-sig", "gb18030"))
            data = lrc.read_bytes()

        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data.decode("utf-8-sig"), lyric)


if __name__ == "__main__":
    unittest.main()

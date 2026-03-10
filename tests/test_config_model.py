from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from musicvault.core.config import FileConfig


class TestConfigModel(unittest.TestCase):
    def test_load_creates_default_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = FileConfig.load(path)
            self.assertTrue(path.exists())
            self.assertEqual(cfg.workspace, "./workspace")
            self.assertEqual(cfg.lyrics.lossy_lrc_encodings, ("gb2312", "gb18030", "utf-8-sig"))

    def test_load_and_save_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "cookie": "abc",
                        "workspace": "./workspace2",
                        "playlist_id": 123,
                        "force": True,
                        "include_translation": False,
                        "text_cleaning": {"enabled": False},
                        "workers": {"download": 3, "process": 2, "ffmpeg_threads": 5},
                        "lyrics": {"lossy_lrc_encodings": ["utf-8-sig", "gb18030"]},
                    }
                ),
                encoding="utf-8",
            )
            cfg = FileConfig.load(path)
            self.assertEqual(cfg.cookie, "abc")
            self.assertEqual(cfg.workspace, "./workspace2")
            self.assertEqual(cfg.playlist_id, 123)
            self.assertFalse(cfg.include_translation)
            self.assertFalse(cfg.text_cleaning.enabled)
            self.assertEqual(cfg.workers.download, 3)
            self.assertEqual(cfg.lyrics.lossy_lrc_encodings, ("utf-8-sig", "gb18030"))

            cfg.cookie = "xyz"
            cfg.save()
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["cookie"], "xyz")


if __name__ == "__main__":
    unittest.main()

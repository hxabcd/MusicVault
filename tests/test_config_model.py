from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from musicvault.core.config import Config


def test_load_creates_default_file() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        assert path.exists()
        assert cfg.workspace == "./workspace"
        assert cfg.lossy_lrc_encodings == ("gb18030", "utf-8-sig")


def test_load_and_save_roundtrip() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "cookie": "abc",
                    "workspace": "./workspace2",
                    "playlist_ids": [123, 456],
                    "force": True,
                    "include_translation": False,
                    "text_cleaning": {"enabled": False},
                    "workers": {"download": 3, "process": 2, "ffmpeg_threads": 5},
                    "lyrics": {"lossy_lrc_encodings": ["utf-8-sig", "gb18030"]},
                }
            ),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.cookie == "abc"
        assert cfg.workspace == "./workspace2"
        assert cfg.playlist_ids == [123, 456]
        assert not cfg.include_translation
        assert not cfg.text_cleaning_enabled
        assert cfg.download_workers == 3
        assert cfg.lossy_lrc_encodings == ("utf-8-sig", "gb18030")

        cfg.cookie = "xyz"
        cfg.save()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["cookie"] == "xyz"

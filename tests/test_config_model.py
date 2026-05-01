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
        assert cfg.lossy_lrc_encodings == ("utf-8",)


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
                    "translation_format": "inline",
                    "text_cleaning": {"enabled": False},
                    "workers": {"download": 3, "process": 2, "ffmpeg_threads": 5},
                    "lyrics": {"lossy_lrc_encodings": ["utf-8-sig", "gb18030"]},
                    "lossy": {"bitrate": "256k"},
                }
            ),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.cookie == "abc"
        assert cfg.workspace == "./workspace2"
        assert not cfg.include_translation
        assert cfg.translation_format == "inline"
        assert cfg.lossy_bitrate == "256k"
        assert not cfg.text_cleaning_enabled
        assert cfg.download_workers == 3
        assert cfg.lossy_lrc_encodings == ("utf-8-sig", "gb18030")

        # 旧 playlist_ids 已迁移到 playlists.json
        assert cfg.get_playlist_ids() == [123, 456]

        cfg.cookie = "xyz"
        cfg.save()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["cookie"] == "xyz"
        # 保存后 config 中不应再有 playlist_ids
        assert "playlist_ids" not in loaded
        assert loaded["translation_format"] == "inline"
        assert loaded["lossy"]["bitrate"] == "256k"


def test_new_fields_have_defaults() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        assert cfg.lossy_bitrate == "192k"
        assert cfg.translation_format == "separate"


def test_translation_format_validation() -> None:
    from musicvault.core.config import Config as Cfg

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({"translation_format": "invalid"}),
            encoding="utf-8",
        )
        import pytest

        with pytest.raises(RuntimeError, match="translation_format"):
            Cfg.load(path)


def test_download_quality_validation() -> None:
    from musicvault.core.config import Config as Cfg

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"download": {"quality": "ultra_hd"}}), encoding="utf-8")
        import pytest

        with pytest.raises(RuntimeError, match="download.quality"):
            Cfg.load(path)


def test_lossy_format_validation() -> None:
    from musicvault.core.config import Config as Cfg

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"lossy": {"format": "wma"}}), encoding="utf-8")
        import pytest

        with pytest.raises(RuntimeError, match="lossy.format"):
            Cfg.load(path)


def test_all_new_fields_defaults() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        assert cfg.download_quality == "hires"
        assert cfg.embed_cover is True
        assert cfg.cover_max_size_kb == 0
        assert cfg.lyrics_embed_in_metadata is True
        assert cfg.lyrics_write_lrc_file is True
        assert cfg.filename_lossless == "{artist} - {name}"
        assert cfg.filename_lossy == "{alias} {name} - {artist}"
        assert cfg.network_download_timeout == 30
        assert cfg.network_max_retries == 3
        assert cfg.lossy_format == "mp3"
        assert cfg.keep_downloads is False
        assert cfg.default_playlist_name == "未分类"
        assert cfg.ffmpeg_path == ""
        assert cfg.api_download_url_chunk_size == 200
        assert cfg.api_track_detail_chunk_size == 500
        assert cfg.alias_split_separators == "/、;；"
        assert cfg.metadata_fields == ()
        assert cfg.build_alias_split_re().pattern == r"[/、;；]+"


def test_new_fields_in_roundtrip() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "download": {"quality": "lossless"},
                    "cover": {"embed": False, "max_size_kb": 100},
                    "lyrics": {"embed_in_metadata": False, "write_lrc_file": False},
                    "filenames": {"lossless": "{track_id} - {name}", "lossy": "{name}"},
                    "network": {"download_timeout": 60, "max_retries": 5},
                    "lossy": {"format": "opus", "bitrate": "128k"},
                    "metadata": {"fields": ["year", "genre"]},
                    "process": {"keep_downloads": True},
                    "playlist": {"default_name": "其他"},
                    "ffmpeg": {"path": "/usr/bin/ffmpeg"},
                    "alias": {"split_separators": "|;"},
                }
            ),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.download_quality == "lossless"
        assert cfg.embed_cover is False
        assert cfg.cover_max_size_kb == 100
        assert cfg.lyrics_embed_in_metadata is False
        assert cfg.lyrics_write_lrc_file is False
        assert cfg.filename_lossless == "{track_id} - {name}"
        assert cfg.filename_lossy == "{name}"
        assert cfg.network_download_timeout == 60
        assert cfg.network_max_retries == 5
        assert cfg.lossy_format == "opus"
        assert cfg.metadata_fields == ("year", "genre")
        assert cfg.keep_downloads is True
        assert cfg.default_playlist_name == "其他"
        assert cfg.ffmpeg_path == "/usr/bin/ffmpeg"
        assert cfg.alias_split_separators == "|;"

        cfg.save()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["download"]["quality"] == "lossless"
        assert loaded["cover"] == {"embed": False, "max_size_kb": 100}
        assert loaded["lyrics"]["embed_in_metadata"] is False
        assert loaded["lyrics"]["write_lrc_file"] is False
        assert loaded["lossy"]["format"] == "opus"
        assert loaded["metadata"]["fields"] == ["year", "genre"]
        assert loaded["process"]["keep_downloads"] is True
        assert loaded["playlist"]["default_name"] == "其他"


class TestSongManagement:
    def test_add_and_get_songs(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws))
            cfg.ensure_dirs()
            assert cfg.get_song_ids() == []

            cfg.add_song(123)
            cfg.add_song(456)
            assert cfg.get_song_ids() == [123, 456]

    def test_add_duplicate(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws))
            cfg.add_song(100)
            cfg.add_song(100)
            assert cfg.get_song_ids() == [100]

    def test_has_song(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws))
            cfg.add_song(42)
            assert cfg.has_song(42) is True
            assert cfg.has_song(99) is False

    def test_remove_song(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws))
            cfg.add_song(1)
            cfg.add_song(2)
            cfg.remove_song(1)
            assert cfg.get_song_ids() == [2]

    def test_remove_last_song_deletes_file(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws))
            cfg.add_song(1)
            assert cfg._songs_path.exists()
            cfg.remove_song(1)
            assert not cfg._songs_path.exists()

    def test_songs_survive_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg1 = Config(workspace=str(ws))
            cfg1.add_song(10)
            cfg1.add_song(20)

            cfg2 = Config(workspace=str(ws))
            assert cfg2.get_song_ids() == [10, 20]

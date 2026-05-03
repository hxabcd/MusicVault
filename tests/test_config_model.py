from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from musicvault.core.config import Config
from musicvault.core.preset import Preset


def test_load_creates_default_file() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        assert path.exists()
        assert cfg.workspace == "./workspace"
        assert len(cfg.presets) == 2
        assert cfg.presets[0].name == "archive"
        assert cfg.presets[1].name == "portable"


def test_load_with_presets() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "cookie": "abc",
                    "workspace": "./ws",
                    "presets": [
                        {
                            "name": "archive",
                            "quality": "hires",
                            "format": "flac",
                            "filename_template": "{artist} - {name}",
                            "embed_cover": True,
                            "use_karaoke": True,
                            "translation_format": "separate",
                            "write_lrc_file": False,
                        },
                        {
                            "name": "portable",
                            "quality": "hires",
                            "format": "mp3",
                            "bitrate": "192k",
                            "filename_template": "{alias} {name} - {artist}",
                            "embed_cover": False,
                            "write_lrc_file": True,
                            "lrc_encodings": ["utf-8", "gb18030"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.cookie == "abc"
        assert cfg.workspace == "./ws"
        assert len(cfg.presets) == 2
        assert cfg.presets[0].name == "archive"
        assert cfg.presets[0].format == "flac"
        assert cfg.presets[0].use_karaoke is True
        assert cfg.presets[1].name == "portable"
        assert cfg.presets[1].format == "mp3"
        assert cfg.presets[1].bitrate == "192k"
        assert cfg.presets[1].lrc_encodings == ("utf-8", "gb18030")


def test_roundtrip_presets() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        cfg.cookie = "xyz"
        cfg.presets[0].format = "flac"
        cfg.save()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["cookie"] == "xyz"
        assert len(loaded["presets"]) == 2
        assert loaded["presets"][0]["name"] == "archive"
        assert loaded["presets"][1]["name"] == "portable"


def test_old_format_raises() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({
                "lossy": {"bitrate": "192k", "format": "mp3"},
                "filenames": {"lossless": "{artist} - {name}", "lossy": "{name}"},
            }),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="旧版配置"):
            Config.load(path)


def test_ensure_dirs_creates_preset_dirs() -> None:
    with TemporaryDirectory() as tmp:
        cfg = Config(workspace=tmp, presets=[
            Preset(name="archive"),
            Preset(name="portable"),
        ])
        cfg.ensure_dirs()
        assert (Path(tmp) / "library" / "archive").is_dir()
        assert (Path(tmp) / "library" / "portable").is_dir()
        assert (Path(tmp) / "downloads").is_dir()
        assert (Path(tmp) / "state").is_dir()


def test_preset_dir_property() -> None:
    cfg = Config(workspace="./ws", presets=[Preset(name="archive")])
    assert cfg.preset_dir("archive") == Path("./ws").resolve() / "library" / "archive"


def test_all_global_fields_retained() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({
                "workers": {"download": 3, "process": 2, "ffmpeg_threads": 4},
                "network": {"download_timeout": 60},
                "process": {"keep_downloads": True},
                "playlist": {"default_name": "其他"},
                "ffmpeg": {"path": "/usr/bin/ffmpeg"},
                "api": {"download_url_chunk_size": 100},
                "alias": {"split_separators": "|"},
                "presets": [{"name": "test"}],
            }),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.download_workers == 3
        assert cfg.process_workers == 2
        assert cfg.ffmpeg_threads == 4
        assert cfg.network_download_timeout == 60
        assert cfg.keep_downloads is True
        assert cfg.default_playlist_name == "其他"
        assert cfg.ffmpeg_path == "/usr/bin/ffmpeg"
        assert cfg.api_download_url_chunk_size == 100
        assert cfg.alias_split_separators == "|"


class TestSongManagement:
    def test_add_and_get_songs(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.ensure_dirs()
            assert cfg.get_song_ids() == []

            cfg.add_song(123)
            cfg.add_song(456)
            assert cfg.get_song_ids() == [123, 456]

    def test_add_duplicate(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(100)
            cfg.add_song(100)
            assert cfg.get_song_ids() == [100]

    def test_has_song(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(42)
            assert cfg.has_song(42) is True
            assert cfg.has_song(99) is False

    def test_remove_song(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(1)
            cfg.add_song(2)
            cfg.remove_song(1)
            assert cfg.get_song_ids() == [2]

    def test_remove_last_song_deletes_file(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg.add_song(1)
            assert cfg._songs_path.exists()
            cfg.remove_song(1)
            assert not cfg._songs_path.exists()

    def test_songs_survive_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cfg1 = Config(workspace=str(ws), presets=[Preset(name="archive")])
            cfg1.add_song(10)
            cfg1.add_song(20)

            cfg2 = Config(workspace=str(ws), presets=[Preset(name="archive")])
            assert cfg2.get_song_ids() == [10, 20]

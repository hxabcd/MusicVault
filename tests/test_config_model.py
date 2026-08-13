from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from musicvault.core.config import Config


def test_load_creates_default_file() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        assert path.exists()
        assert cfg.workspace == "./workspace"
        assert cfg.builtin_scripts_enabled is True
        assert cfg.script_directories == ()
        assert not hasattr(cfg, "presets")
        assert not hasattr(cfg, "metadata_fields")


def test_legacy_presets_array_is_ignored() -> None:
    """旧声明式 presets 数组宽容忽略：可加载、不解析、不报错。"""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "cookie": "abc",
                    "workspace": "./ws",
                    "presets": [
                        {"name": "archive", "quality": "hires", "format": "flac"},
                        {"name": "portable", "quality": "hires", "format": "mp3", "bitrate": "192k"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.cookie == "abc"
        assert cfg.workspace == "./ws"
        assert cfg.builtin_scripts_enabled is True
        # presets 数组被忽略：不再解析为字段，也不参与任何行为
        assert not hasattr(cfg, "presets")


def test_metadata_fields_is_ignored() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({"metadata": {"fields": ["year", "genre"]}}),
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert not hasattr(cfg, "metadata_fields")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert "metadata" not in loaded


def test_script_system_builtin_false() -> None:
    """script_system.builtin=false 解析为 builtin_scripts_enabled=False。"""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"script_system": {"builtin": False}}), encoding="utf-8")
        cfg = Config.load(path)
        assert cfg.builtin_scripts_enabled is False


def test_legacy_preset_system_builtin_false() -> None:
    """旧 preset_system.builtin=false 兼容读取。"""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"preset_system": {"builtin": False}}), encoding="utf-8")
        cfg = Config.load(path)
        assert cfg.builtin_scripts_enabled is False


def test_script_system_playlist_links_migrates_to_builtin() -> None:
    """旧 script_system.playlist_links 迁移为 builtin。"""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"script_system": {"playlist_links": False}}), encoding="utf-8")
        cfg = Config.load(path)
        assert cfg.builtin_scripts_enabled is False


def test_to_dict_uses_builtin_key() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        cfg.builtin_scripts_enabled = False
        cfg.save()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["script_system"]["builtin"] is False
        assert "playlist_links" not in loaded["script_system"]
        # 声明式 presets 与 metadata.fields 不再序列化
        assert "presets" not in loaded
        assert "metadata" not in loaded


def test_preset_dir_method_removed() -> None:
    assert not hasattr(Config, "preset_dir")


def test_ensure_dirs_creates_five_areas_only() -> None:
    """ensure_dirs 只确保五区域，不再创建任何 preset 目录。"""
    with TemporaryDirectory() as tmp:
        cfg = Config(workspace=tmp)
        cfg.ensure_dirs()
        for name in ("cache", "media_store", "library", "logs"):
            assert (Path(tmp) / name).is_dir()
        assert not (Path(tmp) / "library" / "archive").exists()
        assert not (Path(tmp) / "library" / "portable").exists()


def test_roundtrip_global_fields() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config.load(path)
        cfg.cookie = "xyz"
        cfg.builtin_scripts_enabled = False
        cfg.save()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["cookie"] == "xyz"
        assert loaded["script_system"]["builtin"] is False


def test_old_format_raises() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "lossy": {"bitrate": "192k", "format": "mp3"},
                    "filenames": {"lossless": "{artist} - {name}", "lossy": "{name}"},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="旧版配置"):
            Config.load(path)


def test_all_global_fields_retained() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "workers": {"download": 3, "process": 2, "ffmpeg_threads": 4},
                    "network": {"download_timeout": 60},
                    "process": {"keep_downloads": True},
                    "playlist": {"default_name": "其他"},
                    "ffmpeg": {"path": "/usr/bin/ffmpeg"},
                    "api": {"download_url_chunk_size": 100},
                    "alias": {"split_separators": "|"},
                    "preset_system": {"directories": ["./my_presets"]},
                }
            ),
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
        assert cfg.script_directories == ("./my_presets",)

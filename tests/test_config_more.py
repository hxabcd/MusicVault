"""Config 补充单测：加载边界、非法值容错与序列化往返。

覆盖：from_dict 非对象输入、各段配置非 dict 容错、workers 非法值、
network 非法值回退默认、save 无路径、路径属性与 alias 正则构建。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicvault.core.config import Config


def test_from_dict_rejects_non_dict() -> None:
    """非 dict 顶层输入直接报错。"""
    with pytest.raises(RuntimeError, match="JSON 对象"):
        Config.from_dict([1, 2, 3])


def test_from_dict_tolerates_non_dict_sections() -> None:
    """各配置段为非 dict 时宽容回退默认值。"""
    raw = {
        "workers": "x",
        "network": "net",
        "text_cleaning": 1,
        "process": "proc",
        "playlist": "p",
        "ffmpeg": "f",
        "api": "a",
        "alias": "al",
        "preset_system": "ps",
        "preset_directories": "not-a-list",
    }
    cfg = Config.from_dict(raw)
    assert cfg.download_workers is None
    assert cfg.process_workers is None
    assert cfg.network_download_timeout == 30
    assert cfg.network_api_timeout == 15
    assert cfg.network_max_retries == 3
    assert cfg.text_cleaning_enabled is True
    assert cfg.text_cleaning_allowlist == ""
    assert cfg.keep_downloads is False
    assert cfg.default_playlist_name == "未分类"
    assert cfg.ffmpeg_path == ""
    assert cfg.api_download_url_chunk_size == 200
    assert cfg.api_track_detail_chunk_size == 500
    assert cfg.alias_split_separators == "/、;；"
    assert cfg.preset_directories == ()
    assert cfg.builtin_scripts_enabled is True


def test_from_dict_workers_invalid_value_raises() -> None:
    """workers 值无法转 int → 报错。"""
    with pytest.raises(RuntimeError, match="格式错误"):
        Config.from_dict({"workers": {"download": "abc"}})


def test_from_dict_workers_non_positive_raises() -> None:
    """workers 值不大于 0 → 报错。"""
    with pytest.raises(RuntimeError, match="必须大于 0"):
        Config.from_dict({"workers": {"process": 0}})


def test_from_dict_network_invalid_value_uses_default() -> None:
    """network 数值非法时回退默认值。"""
    cfg = Config.from_dict({"network": {"download_timeout": "abc", "max_retries": "x", "api_timeout": -5}})
    assert cfg.network_download_timeout == 30
    assert cfg.network_max_retries == 3
    # 合法值下限保护：min(10, 5) / max(5, ...) 夹取
    assert cfg.network_api_timeout == 5


def test_save_without_path_raises() -> None:
    """未指定路径且无 _file 时保存报错。"""
    cfg = Config()
    with pytest.raises(RuntimeError, match="路径为空"):
        cfg.save()


def test_load_existing_file_roundtrip(tmp_path: Path) -> None:
    """已存在文件加载后保存，往返字段一致。"""
    path = tmp_path / "config.json"
    path.write_text(
        '{"workspace": "./ws", "preset_system": {"directories": ["./p1", " ./p2 "], "builtin": false}}',
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.workspace == "./ws"
    assert cfg.builtin_scripts_enabled is False
    assert cfg.preset_directories == ("./p1", "./p2")
    cfg.cookie = "MUSIC_U=xyz"
    cfg.save()
    loaded = Config.load(path)
    assert loaded.cookie == "MUSIC_U=xyz"


def test_path_properties(tmp_path: Path) -> None:
    """workspace 派生路径属性。"""
    cfg = Config(workspace=str(tmp_path / "ws"))
    resolved = (tmp_path / "ws").resolve()
    assert cfg.workspace_path == resolved
    assert cfg.cache_dir == resolved / "cache"
    assert cfg.media_store_dir == resolved / "media_store"
    assert cfg.state_db_file == resolved / "state.db"
    assert cfg.logs_dir == resolved / "logs"
    assert cfg.library_dir == resolved / "library"


def test_build_alias_split_re() -> None:
    """alias 分隔符转义后构建切分正则。"""
    cfg = Config(alias_split_separators="|;")
    pattern = cfg.build_alias_split_re()
    assert pattern.split("A|B;C") == ["A", "B", "C"]
    # 正则字符被转义：`.` 不充当通配符
    cfg2 = Config(alias_split_separators=".")
    assert cfg2.build_alias_split_re().split("a.b") == ["a", "b"]

"""CLI 歌单/单曲管理命令（add/remove/list）非交互路径测试。

覆盖 handle_playlist_mgmt 的 add（--input/--song）、remove（--playlist-id/--song）、
list 分支，以及 _parse_playlist_id / _add_playlist_by_id / _parse_selection /
_add_songs / _remove_songs / _list_songs 内部函数；交互式选择流程（input 提示）不测。
"""

from __future__ import annotations

import argparse
import logging

import pytest

from musicvault.cli.playlist import (
    _add_songs,
    _list_songs,
    _parse_playlist_id,
    _parse_selection,
    _remove_songs,
    handle_playlist_mgmt,
)
from musicvault.core.config import Config
from musicvault.domain.models import Playlist


class _FakePlaylistUseCase:
    """鸭子类型 fake PlaylistUseCase：内存歌单/单曲状态并记录调用。"""

    def __init__(self) -> None:
        self.playlists: dict[int, Playlist] = {}
        self.songs: set[int] = set()
        self.added: list[tuple[int, str]] = []
        self.removed: list[int] = []

    def list_playlists(self) -> list[Playlist]:
        return list(self.playlists.values())

    def get_playlist(self, playlist_id: int) -> Playlist | None:
        return self.playlists.get(playlist_id)

    def has_playlist(self, playlist_id: int) -> bool:
        return playlist_id in self.playlists

    def add_playlist(self, playlist_id: int, name: str = "") -> None:
        self.added.append((playlist_id, name))
        self.playlists[playlist_id] = Playlist(playlist_id, name, ())

    def remove_playlist(self, playlist_id: int) -> None:
        self.removed.append(playlist_id)
        self.playlists.pop(playlist_id, None)

    def list_songs(self) -> list[int]:
        return sorted(self.songs)

    def has_song(self, song_id: int) -> bool:
        return song_id in self.songs

    def add_song(self, song_id: int) -> None:
        self.songs.add(song_id)

    def remove_song(self, song_id: int) -> None:
        self.songs.discard(song_id)


class _FakeApi:
    """鸭子类型 fake SourceClient：记录登录并按配置返回歌单信息或抛异常。"""

    def __init__(self, info: dict | None = None, error: Exception | None = None) -> None:
        self.info = info
        self.error = error
        self.login_cookies: list[str] = []

    def login_with_cookie(self, cookie: str) -> None:
        self.login_cookies.append(cookie)

    def get_playlist_info(self, playlist_id: int) -> dict:
        if self.error is not None:
            raise self.error
        return self.info or {}


@pytest.fixture()
def env(monkeypatch) -> dict:
    """替换 bootstrap 构建函数，返回 fake 用例与可换装的 fake API。"""
    use_case = _FakePlaylistUseCase()
    api_holder: dict = {"api": _FakeApi()}
    monkeypatch.setattr("musicvault.application.bootstrap.build_playlist_use_case", lambda _cfg: use_case)
    monkeypatch.setattr("musicvault.application.bootstrap.build_source_client", lambda _cfg: api_holder["api"])
    return {"use_case": use_case, "api_holder": api_holder}


def _args(command: str, **kwargs: object) -> argparse.Namespace:
    ns = argparse.Namespace(command=command)
    for key, value in kwargs.items():
        setattr(ns, key, value)
    return ns


def _all_output(capfd) -> str:
    captured = capfd.readouterr()
    return captured.out + captured.err


# -- handle_playlist_mgmt：add -------------------------------------------------


def test_handle_add_with_link_and_cookie(env, capfd) -> None:
    """add <链接> + cookie：解析链接 ID、API 验证并带名称登记。"""
    env["api_holder"]["api"] = _FakeApi(info={"name": "歌单A"})
    args = _args("add", input=["https://music.163.com/#/playlist?id=777"], song=None, cookie="ck")

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].playlists[777].name == "歌单A"
    out = _all_output(capfd)
    assert "已添加歌单" in out and "歌单A" in out


def test_handle_add_with_invalid_input_returns_1(env, capfd) -> None:
    """add 传入无法识别的标识：输出错误并返回 1，不登记任何歌单。"""
    args = _args("add", input=["not-a-number"], song=None, cookie="ck")

    assert handle_playlist_mgmt(args, Config()) == 1
    assert "无法识别的歌单标识" in _all_output(capfd)
    assert env["use_case"].playlists == {}


def test_handle_add_mixed_valid_and_invalid_returns_1(env, capfd) -> None:
    """add 混入非法输入：合法 ID 正常登记，整体仍返回 1。"""
    env["api_holder"]["api"] = _FakeApi(info={"name": "好歌单"})
    args = _args("add", input=["bad", "123"], song=None, cookie="ck")

    assert handle_playlist_mgmt(args, Config()) == 1
    assert env["use_case"].playlists[123].name == "好歌单"
    assert "无法识别的歌单标识" in _all_output(capfd)


def test_handle_add_playlist_already_exists_returns_1(env, caplog) -> None:
    """add 重复歌单：警告跳过，返回 1。"""
    env["use_case"].playlists[111] = Playlist(111, "旧歌单", ())
    args = _args("add", input=["111"], song=None, cookie="ck")

    with caplog.at_level(logging.WARNING, logger="musicvault.cli.playlist"):
        assert handle_playlist_mgmt(args, Config()) == 1
    assert any("111" in record.message and "已存在" in record.message for record in caplog.records)


def test_handle_add_without_cookie_skips_api(env, caplog, capfd) -> None:
    """add 未提供 cookie：跳过 API 验证，仅保存 ID。"""
    args = _args("add", input=["222"], song=None, cookie=None)

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].playlists[222].name == ""
    assert "已添加歌单：222" in _all_output(capfd)
    assert any("未提供 cookie" in record.message for record in caplog.records)


def test_handle_add_api_failure_degrades_to_id_only(env, caplog) -> None:
    """add 带 cookie 但 API 失败：降级为仅保存 ID，不阻塞添加。"""
    env["api_holder"]["api"] = _FakeApi(error=RuntimeError("网络错误"))
    args = _args("add", input=["333"], song=None, cookie="ck")

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].playlists[333].name == ""
    assert any("无法获取歌单信息" in record.message for record in caplog.records)


def test_handle_add_song_only(env, capfd) -> None:
    """add --song：新增单曲并跳过已存在项，整体返回 0。"""
    env["use_case"].songs = {6}
    args = _args("add", input=[], song=[5, 6], cookie=None)

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].songs == {5, 6}
    out = _all_output(capfd)
    assert "已添加单曲：5" in out
    assert "单曲 6 已存在" in out


def test_handle_add_song_and_input_combined(env, capfd) -> None:
    """add 同时提供 --song 与 input：两条路径都执行。"""
    env["api_holder"]["api"] = _FakeApi(info={"name": "双通道"})
    args = _args("add", input=["123"], song=[5], cookie="ck")

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].songs == {5}
    assert env["use_case"].playlists[123].name == "双通道"
    out = _all_output(capfd)
    assert "已添加单曲：5" in out and "已添加歌单" in out


# -- handle_playlist_mgmt：remove ----------------------------------------------


def test_handle_remove_by_id_success(env, capfd) -> None:
    """remove <ID>：歌单存在时移除并输出成功。"""
    env["use_case"].playlists[100] = Playlist(100, "我的歌单", ())
    args = _args("remove", playlist_id=100, song=None)

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].removed == [100]
    assert env["use_case"].playlists == {}
    assert "已移除歌单" in _all_output(capfd)


def test_handle_remove_by_id_missing_returns_1(env, capfd) -> None:
    """remove <ID>：歌单不存在时警告并返回 1。"""
    args = _args("remove", playlist_id=999, song=None)

    assert handle_playlist_mgmt(args, Config()) == 1
    assert "999 不存在" in _all_output(capfd)


def test_handle_rm_alias_removes_playlist(env) -> None:
    """rm 别名与 remove 等价：按 ID 移除。"""
    env["use_case"].playlists[50] = Playlist(50, "别名歌单", ())
    args = _args("rm", playlist_id=50, song=None)

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].removed == [50]


def test_handle_remove_song_only(env, capfd) -> None:
    """remove --song：移除存在的单曲，跳过不存在的，返回 0。"""
    env["use_case"].songs = {7}
    args = _args("remove", playlist_id=None, song=[7, 8])

    assert handle_playlist_mgmt(args, Config()) == 0
    assert env["use_case"].songs == set()
    out = _all_output(capfd)
    assert "已移除单曲：7" in out
    assert "单曲 8 不存在" in out


def test_handle_remove_song_all_missing(env, capfd) -> None:
    """remove --song 全部不存在：输出汇总提示，返回 0。"""
    args = _args("remove", playlist_id=None, song=[99])

    assert handle_playlist_mgmt(args, Config()) == 0
    assert "未移除任何单曲" in _all_output(capfd)


# -- handle_playlist_mgmt：list ------------------------------------------------


def test_handle_list_empty_shows_hint(env, capfd) -> None:
    """list 无歌单：输出引导提示。"""
    args = _args("list", song=False)

    assert handle_playlist_mgmt(args, Config()) == 0
    assert "尚未添加任何歌单" in _all_output(capfd)


def test_handle_list_with_playlists_renders_table(env, capfd) -> None:
    """list 有歌单：表格展示 ID、名称与曲目数。"""
    env["use_case"].playlists[1] = Playlist(1, "收藏夹", (11, 12, 13))
    env["use_case"].playlists[2] = Playlist(2, "日推", ())
    args = _args("list", song=False)

    assert handle_playlist_mgmt(args, Config()) == 0
    out = _all_output(capfd)
    assert "当前管理的歌单" in out
    assert "收藏夹" in out and "3 首" in out
    assert "日推" in out and "0 首" in out


def test_handle_list_song_empty_shows_hint(env, capfd) -> None:
    """list --song 无单曲：输出引导提示。"""
    args = _args("list", song=True)

    assert handle_playlist_mgmt(args, Config()) == 0
    assert "尚未添加任何单曲" in _all_output(capfd)


def test_handle_list_song_with_items_renders_table(env, capfd) -> None:
    """list --song 有单曲：表格展示单曲 ID。"""
    env["use_case"].songs = {1, 2}
    args = _args("list", song=True)

    assert handle_playlist_mgmt(args, Config()) == 0
    out = _all_output(capfd)
    assert "当前管理的单曲" in out
    assert "1" in out and "2" in out


# -- _parse_playlist_id ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("123", 123),
        ("  456  ", 456),
        ("https://music.163.com/playlist?id=42&userid=1", 42),
        ("https://music.163.com/#/playlist?id=999", 999),
        ("http://music.163.com/playlist?id=7", 7),
    ],
)
def test_parse_playlist_id_valid(raw: str, expected: int) -> None:
    """数字 ID 与 music.163.com 链接（query/fragment）均可解析。"""
    assert _parse_playlist_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "",
        "12.5",
        "https://other.com/playlist?id=5",
        "https://music.163.com/playlist",
        "https://music.163.com/playlist?id=abc",
        "https://music.163.com/#/playlist?name=x",
    ],
)
def test_parse_playlist_id_invalid_raises(raw: str) -> None:
    """无法识别的标识抛出 RuntimeError。"""
    with pytest.raises(RuntimeError, match="无法识别的歌单标识"):
        _parse_playlist_id(raw)


# -- _parse_selection ------------------------------------------------------------


def test_parse_selection_all() -> None:
    """all 展开为完整编号序列。"""
    assert _parse_selection("all", 5) == [1, 2, 3, 4, 5]
    assert _parse_selection(" ALL ", 3) == [1, 2, 3]


def test_parse_selection_comma_list_and_range() -> None:
    """逗号列表、范围与反转范围均可解析且去重排序。"""
    assert _parse_selection("1,3,5", 5) == [1, 3, 5]
    assert _parse_selection("1-3", 5) == [1, 2, 3]
    assert _parse_selection("3-1", 5) == [1, 2, 3]
    assert _parse_selection(" 2 , 2 ,4 ", 5) == [2, 4]
    assert _parse_selection("1,,3", 5) == [1, 3]


def test_parse_selection_out_of_bounds_trimmed() -> None:
    """越界编号被裁剪，只保留有效范围。"""
    assert _parse_selection("0,9,1-10", 5) == [1, 2, 3, 4, 5]
    assert _parse_selection("0", 5) == []


def test_parse_selection_invalid_values_warn_and_skip(capfd) -> None:
    """非法编号/范围输出警告并跳过，不影响其他合法项。"""
    assert _parse_selection("x,2,a-b", 5) == [2]
    out = _all_output(capfd)
    assert "无效编号：x" in out
    assert "无效范围：a-b" in out


# -- _add_songs / _remove_songs / _list_songs ------------------------------------


def test_add_songs_skips_existing(capfd) -> None:
    """新增单曲：已存在项警告跳过，其余正常登记。"""
    use_case = _FakePlaylistUseCase()
    use_case.songs = {5}

    _add_songs([4, 5], use_case)

    assert use_case.songs == {4, 5}
    out = _all_output(capfd)
    assert "已添加单曲：4" in out
    assert "单曲 5 已存在" in out


def test_add_songs_all_existing_prints_hint(capfd) -> None:
    """全部已存在：输出「未添加任何新单曲」。"""
    use_case = _FakePlaylistUseCase()
    use_case.songs = {5}

    _add_songs([5], use_case)

    assert "未添加任何新单曲" in _all_output(capfd)


def test_remove_songs_skips_missing(capfd) -> None:
    """移除单曲：不存在的项警告跳过，其余正常移除。"""
    use_case = _FakePlaylistUseCase()
    use_case.songs = {7}

    _remove_songs([7, 8], use_case)

    assert use_case.songs == set()
    out = _all_output(capfd)
    assert "已移除单曲：7" in out
    assert "单曲 8 不存在" in out


def test_remove_songs_all_missing_prints_hint(capfd) -> None:
    """全部不存在：输出「未移除任何单曲」。"""
    use_case = _FakePlaylistUseCase()

    _remove_songs([99], use_case)

    assert "未移除任何单曲" in _all_output(capfd)


def test_list_songs_empty_returns_0(capfd) -> None:
    """空单曲列表：返回 0 并输出引导。"""
    assert _list_songs(_FakePlaylistUseCase()) == 0
    assert "尚未添加任何单曲" in _all_output(capfd)


def test_list_songs_with_items_returns_0(capfd) -> None:
    """非空单曲列表：渲染表格并返回 0。"""
    use_case = _FakePlaylistUseCase()
    use_case.songs = {1, 2}

    assert _list_songs(use_case) == 0
    out = _all_output(capfd)
    assert "当前管理的单曲" in out
    assert "1" in out and "2" in out

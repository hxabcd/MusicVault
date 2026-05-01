from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from musicvault.core.models import Track

INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
_FILENAME_TEMPLATE_RE = re.compile(r"\{(\w+)\}")

logger = logging.getLogger(__name__)

_hardlink_fallback_warned = False


def _warn_hardlink_fallback_once() -> None:
    """仅在首次触发硬链接回退时输出一次警告。"""
    global _hardlink_fallback_warned
    if not _hardlink_fallback_warned:
        _hardlink_fallback_warned = True
        logger.warning("硬链接不可用，回退为文件复制模式（跨盘符或文件系统不支持硬链接）")


def safe_filename(name: str, fallback: str = "untitled") -> str:
    """将文本转成可安全落盘的文件名"""
    compacted = re.sub(r" {2,}", " ", name)
    clean = INVALID_FILENAME_RE.sub("_", compacted).strip(" .")
    return clean or fallback


def format_track_name(template: str, track: "Track") -> str:
    """用模板格式化曲目文件名。

    支持的占位符：
        {name} / {title}  -- 歌曲名
        {artist}          -- 歌手（多个以 , 分隔）
        {alias}           -- 第一个别名（无别名时为空）
        {album}           -- 专辑名
        {track_id}        -- 曲目 ID
    """
    alias_text = track.alias or ""

    def _replacer(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in ("name", "title"):
            return track.name
        if key == "artist":
            return track.artist_text.replace("/", ",")
        if key == "alias":
            return alias_text
        if key == "album":
            return track.album
        if key == "track_id":
            return str(track.id)
        return m.group(0)

    raw = _FILENAME_TEMPLATE_RE.sub(_replacer, template).strip()
    return safe_filename(raw)


def workspace_rel_path(path: Path, workspace: Path) -> str:
    """将绝对路径转为 workspace 下的相对路径；跨盘符时回退为绝对路径字符串"""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(workspace))
    except ValueError:
        return str(resolved)


def load_json(path: Path, default: Any) -> Any:
    """读取 JSON 文件，不存在时返回默认值"""
    # 状态文件缺失或损坏时返回默认值，避免首次运行/中断写入后报错。
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("状态文件已损坏，将使用默认值：%s", path)
        return default
    except OSError:
        return default


def save_json(path: Path, value: Any, indent: int | None = None) -> None:
    """写入 JSON 文件并自动创建父目录"""
    # 先写临时文件再替换，降低中断导致的 JSON 损坏概率。
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(value, ensure_ascii=False, indent=indent), encoding="utf-8")
    tmp_path.replace(path)


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """如果可能则创建硬链接，否则回退到复制"""
    if dst.exists() or not src.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        _warn_hardlink_fallback_once()
        shutil.copy2(src, dst)


def create_link(src: Path, dst: Path) -> None:
    """创建硬链接（自动创建父目录），目标已存在时跳过，源不存在时跳过"""
    if dst.exists() or not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        _warn_hardlink_fallback_once()
        shutil.copy2(src, dst)


def remove_link(path: Path) -> None:
    """删除硬链接/文件，不存在时静默跳过"""
    path.unlink(missing_ok=True)

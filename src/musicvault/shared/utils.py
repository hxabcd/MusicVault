from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def safe_filename(name: str, fallback: str = "untitled") -> str:
    """将文本转成可安全落盘的文件名"""
    # 过滤 Windows 非法字符，避免落盘失败。
    clean = INVALID_FILENAME_RE.sub("_", name).strip(" .")
    return clean or fallback


def load_json(path: Path, default: Any) -> Any:
    """读取 JSON 文件，不存在时返回默认值"""
    # 状态文件缺失或损坏时返回默认值，避免首次运行/中断写入后报错。
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: Any) -> None:
    """写入 JSON 文件并自动创建父目录"""
    # 先写临时文件再替换，降低中断导致的 JSON 损坏概率。
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


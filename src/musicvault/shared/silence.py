"""第三方库输出静默的统一出口。

库向终端输出的噪音分两种通道，集中在此管理，便于一处理解全局：

- `silence_loggers`：**logging 层**。依赖（如 urllib3）经 logging 输出
  DEBUG/INFO 日志，verbose 模式开启时刷屏。进程级配置，CLI 日志初始化时调用。
- `silence_engine_stdout`：**fd 层**。SDK 原生引擎（ncm_music_api.dll）
  用 C 层 printf 直写进程 stdout（fd 1），Python 层 sys.stdout 替换无效，
  需在 SDK 调用点临时重定向到 devnull。

依赖方向：shared 为通用工具层，CLI 与 adapters 均可依赖。
"""

from __future__ import annotations

import contextlib
import logging
import os

# SDK 引擎 printf 直写 fd 1，仅需打开一次 devnull，各调用点复用
_DEVNULL_FD = os.open(os.devnull, os.O_WRONLY)


def silence_loggers(*names: str) -> None:
    """将指定 logger 提升到 WARNING 级别并阻断传播，静默其 INFO/DEBUG 日志。"""
    for name in names:
        muted = logging.getLogger(name)
        muted.setLevel(logging.WARNING)
        muted.propagate = False


@contextlib.contextmanager
def silence_engine_stdout():
    """临时将进程 stdout（fd 1）重定向到 devnull，静默引擎日志。

    多线程下重定向窗口极小（仅 SDK 调用期间），窗口内其他线程写
    fd 1 的内容同样被丢弃，可接受。
    """
    saved_fd = os.dup(1)
    try:
        os.dup2(_DEVNULL_FD, 1)
        yield
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)

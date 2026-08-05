"""MusicVault 的纯领域对象。

本包不依赖 CLI、Rich、SQLite 或第三方 SDK。外部适配器通过 ports 与这些对象交互。
"""

from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, TargetDescriptor
from musicvault.domain.operations import Operation, OperationResult, OperationStatus

__all__ = [
    "MediaAsset",
    "Operation",
    "OperationResult",
    "OperationStatus",
    "Playlist",
    "SourceSnapshot",
    "TargetDescriptor",
]

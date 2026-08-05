"""应用层依赖的端口定义。"""

from musicvault.ports.media import MediaRequest, MediaResolver
from musicvault.ports.state import StateRepository
from musicvault.ports.target import TargetOperations

__all__ = ["MediaRequest", "MediaResolver", "StateRepository", "TargetOperations"]

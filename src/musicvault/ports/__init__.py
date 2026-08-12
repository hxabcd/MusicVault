"""应用层依赖的端口定义。"""

from musicvault.ports.media import MediaRequest, MediaResolver
from musicvault.ports.process_state import ProcessStateRepository
from musicvault.ports.source_state import SourceStateRepository
from musicvault.ports.target import TargetOperations

__all__ = ["MediaRequest", "MediaResolver", "SourceStateRepository", "ProcessStateRepository", "TargetOperations"]

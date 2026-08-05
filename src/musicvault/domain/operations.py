from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OperationStatus(str, Enum):
    PLANNED = "planned"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Operation:
    """操作声明，供 dry-run、重试和审计语义使用。"""

    name: str
    input_data: dict[str, Any] = field(default_factory=dict)
    idempotent: bool = True
    retryable: bool = False
    supports_dry_run: bool = True


@dataclass(frozen=True, slots=True)
class OperationResult:
    """一次标准或自定义操作的结构化结果。"""

    name: str
    status: OperationStatus
    affected: tuple[str, ...] = ()
    error: str | None = None
    retryable: bool = False
    idempotent: bool = True
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: Any = None

    @property
    def ok(self) -> bool:
        return self.status in {OperationStatus.PLANNED, OperationStatus.SUCCEEDED}

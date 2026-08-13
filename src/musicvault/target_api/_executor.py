from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from musicvault.domain.operations import OperationResult, OperationStatus
from musicvault.ports.target import TargetOperations


@dataclass(slots=True)
class OperationExecutor:
    """统一执行标准和自定义操作，集中 dry-run 与结果记录。"""

    target: TargetOperations
    dry_run: bool = False
    results: list[OperationResult] = field(default_factory=list)

    def execute(
        self,
        name: str,
        callback: Callable[[], Any],
        *,
        input_data: dict[str, Any] | None = None,
        affected: Iterable[str] = (),
        idempotent: bool = True,
        retryable: bool = False,
        supports_dry_run: bool = True,
    ) -> OperationResult:
        inputs = dict(input_data or {})
        affected_items = tuple(str(item) for item in affected)
        if self.dry_run:
            status = OperationStatus.PLANNED if supports_dry_run else OperationStatus.SKIPPED
            result = OperationResult(
                name=name,
                status=status,
                affected=affected_items,
                error=None if supports_dry_run else "该操作未声明支持 dry-run，已跳过实际执行",
                idempotent=idempotent,
                retryable=retryable,
                input_data=inputs,
            )
            self.results.append(result)
            return result
        try:
            output = callback()
        except Exception as error:  # noqa: BLE001 - 将基础设施异常转换为结构化结果
            result = OperationResult(
                name=name,
                status=OperationStatus.FAILED,
                affected=affected_items,
                error=str(error),
                idempotent=idempotent,
                retryable=retryable,
                input_data=inputs,
            )
        else:
            result = OperationResult(
                name=name,
                status=OperationStatus.SUCCEEDED,
                affected=affected_items,
                idempotent=idempotent,
                retryable=retryable,
                input_data=inputs,
                output_data=output,
            )
        self.results.append(result)
        return result

    def link(self, source: Path, destination: Path) -> OperationResult:
        return self.execute(
            "link",
            lambda: self.target.link(source, destination),
            input_data={"source": str(source), "destination": str(destination)},
            affected=(str(destination),),
        )

    def copy(self, source: Path, destination: Path) -> OperationResult:
        return self.execute(
            "copy",
            lambda: self.target.copy(source, destination),
            input_data={"source": str(source), "destination": str(destination)},
            affected=(str(destination),),
        )

    def write_text(self, destination: Path, content: str, encoding: str = "utf-8") -> OperationResult:
        return self.execute(
            "write_text",
            lambda: self.target.write_text(destination, content, encoding),
            input_data={"destination": str(destination), "encoding": encoding},
            affected=(str(destination),),
        )

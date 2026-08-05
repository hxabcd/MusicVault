from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from musicvault.domain.models import MediaAsset


@dataclass(frozen=True, slots=True)
class MediaRequest:
    """preset 对媒体规格的最小需求声明。"""

    track_id: int
    asset_type: str = "audio"
    spec: str | None = None


class MediaResolver(Protocol):
    def resolve(self, request: MediaRequest) -> MediaAsset | None: ...

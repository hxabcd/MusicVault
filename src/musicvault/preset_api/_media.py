from __future__ import annotations

from musicvault.domain.models import MediaAsset, SourceSnapshot
from musicvault.ports.media import MediaRequest


class SnapshotMediaResolver:
    """当前阶段只解析快照中已有资产；按需生成延后到后续迭代。"""

    def __init__(self, snapshot: SourceSnapshot) -> None:
        self.snapshot = snapshot

    def resolve(self, request: MediaRequest) -> MediaAsset | None:
        assets = self.snapshot.assets_for(request.track_id, request.asset_type)
        if request.spec is not None:
            assets = tuple(asset for asset in assets if asset.spec == request.spec)
        return assets[0] if assets else None

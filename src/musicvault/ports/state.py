from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from musicvault.core.models import Track
from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot


class StateRepository(Protocol):
    """结构化状态的最小公开查询与写入能力。"""

    def create_snapshot(self) -> SourceSnapshot: ...

    def upsert_track(self, track: Track, *, connection: Any = None) -> None: ...

    def upsert_playlist(self, playlist: Playlist, *, connection: Any = None) -> None: ...

    def upsert_media_asset(self, asset: MediaAsset, *, connection: Any = None) -> None: ...

    def add_managed_song(self, track_id: int, *, connection: Any = None) -> None: ...

    def transaction(self) -> AbstractContextManager[Any]: ...

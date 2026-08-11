from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.application.bootstrap import build_pipeline
from musicvault.core.config import Config


def test_build_pipeline_with_fake_source(tmp_path: Path) -> None:
    """composition root 用注入的 fake source 即可组装完整流水线。"""
    cfg = Config(workspace=str(tmp_path / "ws"))
    service = build_pipeline(cfg, source=MagicMock(), dry_run=True)
    assert service.cfg is cfg
    assert service.dry_run is True
    # 用例持有的状态仓储已指向 workspace 下的 SQLite
    assert service.recorder.state is not None

"""PipelineUseCase 四阶段编排测试：fetch → pull → process → distribute。

同步选项：only_distribute 跳过前三阶段直接分发；distribute=False 跳过分发。
dry-run 语义：fetch 不执行（写 SQLite 有副作用）、pull 只算计划、
process 不跑、distribute 沿用 SyncEngine 的 dry-run。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from musicvault.adapters.state.sqlite import SQLiteProcessStateRepository, SQLiteSourceStateRepository, SQLiteState
from musicvault.application.pipeline_use_case import PipelineUseCase
from musicvault.core.config import Config
from musicvault.domain.models import DownloadedTrack, Playlist, Track
from musicvault.domain.operations import OperationResult, OperationStatus
from musicvault.preset_api.v1 import AudioFormat, BasePreset
from musicvault.target_api.v1 import TargetContext, TargetRegistry, TargetRegistration


def _make_cfg(tmp_path: Path) -> Config:
    return Config(workspace=str(tmp_path / "ws"))


def _make_track(track_id: int) -> Track:
    return Track(id=track_id, name=f"Song {track_id}", artists=["Artist"], album="Album", raw={})


def _repository(cfg: Config) -> SQLiteSourceStateRepository:
    return SQLiteSourceStateRepository(SQLiteState(cfg.state_db_file))


def _process_repository(cfg: Config) -> SQLiteProcessStateRepository:
    return SQLiteProcessStateRepository(SQLiteState(cfg.state_db_file))


class _Mp3Preset(BasePreset):
    """共享 MP3-192k 规格的最小 preset。"""

    format = AudioFormat.MP3
    bitrate = "192k"


class _RecordingSync:
    """记录收到 context 的同步器：所有生命周期操作直接成功。"""

    def __init__(self) -> None:
        self.contexts: list[TargetContext] = []

    def __call__(self, deps: dict[str, object]) -> _RecordingSync:
        """SyncEngine 以 factory(deps) 方式构造同步器；直接返回自身。"""
        del deps
        return self

    def prepare(self, context: TargetContext) -> OperationResult:
        self.contexts.append(context)
        return OperationResult(name="prepare", status=OperationStatus.SUCCEEDED)

    def sync_item(self, track: Track, context: object) -> OperationResult:
        del track, context
        return OperationResult(name="sync_item", status=OperationStatus.SUCCEEDED)

    def finalize(self, context: object) -> OperationResult:
        del context
        return OperationResult(name="finalize", status=OperationStatus.SUCCEEDED)


def _api() -> MagicMock:
    api = MagicMock()
    api.get_playlist_info.return_value = {"name": "歌单A", "track_count": 1}
    api.get_playlist_tracks.return_value = [_make_track(111)]
    api.get_tracks_download_urls.return_value = {111: "http://example.com/111.mp3"}
    api.get_track_lyrics.return_value = None
    return api


def _real_downloader(_: Config) -> MagicMock:
    """下载真实落盘到 cache/，供 process 阶段消费。"""
    downloader = MagicMock()

    def _download(track: Track, _: str, dest: Path) -> DownloadedTrack:
        file = dest / f"{track.id}.mp3"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"fake mp3")
        return DownloadedTrack(track=track, source_file=str(file), is_ncm=False, playlist_ids=[10])

    downloader.download_track.side_effect = _download
    return downloader


def _registry_with(recording: _RecordingSync) -> TargetRegistry:
    registry = TargetRegistry()
    registry.register_target(TargetRegistration(name="links", factory=recording, source="test"))
    return registry


def _patch_processors(monkeypatch, cfg: Config, *, downloader=None, organizer=None) -> MagicMock:
    """替换 PipelineUseCase 内部硬编码的处理器类，隔离真实下载/转码/写标签。"""
    real_downloader = downloader or _real_downloader(cfg)
    monkeypatch.setattr("musicvault.application.pipeline_use_case.Downloader", lambda: real_downloader)
    monkeypatch.setattr("musicvault.application.pipeline_use_case.Decryptor", MagicMock)
    monkeypatch.setattr("musicvault.application.pipeline_use_case.MetadataWriter", MagicMock)

    fake_organizer = organizer or MagicMock()
    if organizer is None:
        canonical = cfg.media_store_dir / "111" / "111_192k.mp3"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"fake mp3")
        fake_organizer.route_audio.return_value = {(AudioFormat.MP3, "192k"): canonical}
    monkeypatch.setattr("musicvault.application.pipeline_use_case.Organizer", lambda **_: fake_organizer)
    return real_downloader


def _pipeline(
    cfg: Config,
    *,
    api: MagicMock | None = None,
    dry_run: bool = False,
    targets: TargetRegistry | None = None,
    target=None,
) -> PipelineUseCase:
    repo = _repository(cfg)
    # 预登记歌单 10：pipeline 从 SQLite 读取 playlist_ids
    repo.upsert_playlist(Playlist(10, "歌单A", ()))
    return PipelineUseCase(
        cfg=cfg,
        api=api or _api(),
        state=repo,
        process_state=_process_repository(cfg),
        dry_run=dry_run,
        presets={"mp3": _Mp3Preset()},
        targets=targets,
        target=target if target is not None else MagicMock(),
    )


def test_run_pipeline_full_flow(tmp_path: Path, monkeypatch) -> None:
    """非 dry-run：fetch → pull → process → distribute 依次执行。"""
    cfg = _make_cfg(tmp_path)
    _patch_processors(monkeypatch, cfg)
    recording = _RecordingSync()
    svc = _pipeline(cfg, targets=_registry_with(recording))

    result = svc.run_pipeline("cookie")

    assert result.downloaded == 1
    assert result.processed == 1
    assert result.track_count == 1
    assert result.playlist_count == 1
    # 曲目与歌单已写入 SQLite（fetch 阶段）
    snapshot = svc.recorder.state.create_snapshot()
    assert [track.id for track in snapshot.tracks] == [111]
    assert snapshot.playlists[0].name == "歌单A"
    # distribute 阶段已执行（SyncEngine 驱动了 registration 的同步器），结果随字段携带
    assert recording.contexts != []
    assert result.distribute is not None
    assert result.distribute.status == OperationStatus.SUCCEEDED


def test_run_pipeline_only_distribute_skips_download(tmp_path: Path, monkeypatch) -> None:
    """only_distribute：跳过 fetch/pull/process，直接分发。"""
    cfg = _make_cfg(tmp_path)
    _patch_processors(monkeypatch, cfg)
    recording = _RecordingSync()
    api = _api()
    svc = _pipeline(cfg, api=api, targets=_registry_with(recording))

    result = svc.run_pipeline("cookie", only_distribute=True)

    # 前三阶段未被触碰：不查询歌单、不查直链、不下载
    api.get_playlist_info.assert_not_called()
    api.get_playlist_tracks.assert_not_called()
    api.get_tracks_download_urls.assert_not_called()
    # 结果只含分发信息（distribute 字段携带 SyncRunResult）
    assert result.downloaded == 0
    assert result.processed == 0
    assert result.distribute is not None
    assert result.distribute.status == OperationStatus.SUCCEEDED
    assert [preset.name for preset in result.distribute.presets] == ["links"]
    assert recording.contexts != []


def test_run_pipeline_no_distribute_skips_distribute(tmp_path: Path, monkeypatch) -> None:
    """distribute=False：fetch/pull/process 照常，分发阶段跳过。"""
    cfg = _make_cfg(tmp_path)
    _patch_processors(monkeypatch, cfg)
    recording = _RecordingSync()
    svc = _pipeline(cfg, targets=_registry_with(recording))

    result = svc.run_pipeline("cookie", distribute=False)

    assert result.downloaded == 1
    assert result.processed == 1
    assert recording.contexts == []
    assert result.distribute is None


def test_run_pipeline_dry_run_skips_fetch_and_process(tmp_path: Path, monkeypatch) -> None:
    """dry-run：fetch 不执行（不写 SQLite）、pull 只算计划、process 不跑、distribute 走 dry-run。"""
    cfg = _make_cfg(tmp_path)
    _patch_processors(monkeypatch, cfg)
    recording = _RecordingSync()
    svc = _pipeline(cfg, dry_run=True, targets=_registry_with(recording))

    result = svc.run_pipeline("cookie")

    assert result.downloaded == 0
    assert result.processed == 0
    assert result.dry_run_plan is not None
    assert result.dry_run_plan["track_count"] == 1
    # fetch 未写库
    assert svc.recorder.state.create_snapshot().tracks == ()
    # distribute 仍执行，且 SyncEngine 收到 dry_run 语义
    assert recording.contexts != []
    assert recording.contexts[0].dry_run is True
    assert result.distribute is not None


def test_run_pipeline_without_registry_skips_distribute(tmp_path: Path, monkeypatch) -> None:
    """targets/target 为 None 时 distribute 阶段静默跳过。"""
    cfg = _make_cfg(tmp_path)
    _patch_processors(monkeypatch, cfg)
    svc = _pipeline(cfg, targets=None, target=None)

    result = svc.run_pipeline("cookie")

    assert result.downloaded == 1
    assert result.processed == 1
    assert result.distribute is None
    # 正常结束，无 distribute 相关异常


def test_run_pipeline_only_distribute_dry_run(tmp_path: Path, monkeypatch) -> None:
    """dry-run + only_distribute：直接以 dry-run 语义分发，不产生下载副作用。"""
    cfg = _make_cfg(tmp_path)
    _patch_processors(monkeypatch, cfg)
    recording = _RecordingSync()
    svc = _pipeline(cfg, dry_run=True, targets=_registry_with(recording))

    result = svc.run_pipeline("cookie", only_distribute=True)

    assert result.downloaded == 0
    assert result.distribute is not None
    assert recording.contexts != []
    assert recording.contexts[0].dry_run is True

from musicvault.adapters.state.sqlite import SQLiteSourceStateRepository, SQLiteState
from musicvault.domain.models import Track


def _track(track_id: int) -> Track:
    return Track(id=track_id, name=f"曲目 {track_id}", artists=[], album="", raw={})


def test_lyrics_upsert_and_read(tmp_path):
    state = SQLiteSourceStateRepository(SQLiteState(tmp_path / "test.db"))
    state.upsert_track(_track(42))
    assert state.get_lyrics(42) is None
    state.save_lyrics(42, '[{"start_ms":1,"duration_ms":0,"text":"x"}]', 123.0)
    state.save_lyrics(42, '[{"start_ms":2,"duration_ms":0,"text":"y"}]', 456.0)  # upsert 覆盖
    assert state.get_lyrics(42) == '[{"start_ms":2,"duration_ms":0,"text":"y"}]'


def test_lyrics_row_hidden_from_media_assets_and_snapshot(tmp_path):
    """歌词原稿行不进入媒体资产列表与源快照。"""
    state = SQLiteSourceStateRepository(SQLiteState(tmp_path / "test.db"))
    state.upsert_track(_track(42))
    state.save_lyrics(42, "[]", 0.0)

    assert state.list_media_assets(track_id=42) == []
    assert state.create_snapshot().media_assets == ()

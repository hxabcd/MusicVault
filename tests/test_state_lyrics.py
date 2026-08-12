from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository


def test_lyrics_upsert_and_read(tmp_path):
    state = SQLiteStateRepository(SQLiteState(tmp_path / "test.db"))
    assert state.get_lyrics(42) is None
    state.save_lyrics(42, '[{"start_ms":1,"duration_ms":0,"text":"x"}]', 123.0)
    state.save_lyrics(42, '[{"start_ms":2,"duration_ms":0,"text":"y"}]', 456.0)  # upsert 覆盖
    assert state.get_lyrics(42) == '[{"start_ms":2,"duration_ms":0,"text":"y"}]'

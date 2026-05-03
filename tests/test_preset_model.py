from __future__ import annotations

import pytest
from musicvault.core.preset import Preset, validate_presets, default_presets, audio_spec_key, build_audio_specs


class TestPresetDefaults:
    def test_minimal_preset(self):
        p = Preset(name="test")
        assert p.name == "test"
        assert p.quality == "hires"
        assert p.format is None
        assert p.bitrate is None
        assert p.filename_template == "{artist} - {name}"
        assert p.embed_cover is True
        assert p.cover_max_size == 0
        assert p.embed_lyrics is True
        assert p.metadata_fields == ()
        assert p.use_karaoke is False
        assert p.include_translation is True
        assert p.translation_format == "separate"
        assert p.include_romaji is False
        assert p.write_lrc_file is True
        assert p.lrc_encodings == ("utf-8",)


class TestPresetValidation:
    def test_unique_names_pass(self):
        presets = [Preset(name="a"), Preset(name="b")]
        validate_presets(presets)

    def test_duplicate_names_raise(self):
        presets = [Preset(name="a"), Preset(name="a")]
        with pytest.raises(ValueError, match="duplicate"):
            validate_presets(presets)

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="at least one preset"):
            validate_presets([])

    def test_invalid_name_characters(self):
        with pytest.raises(ValueError, match="Invalid preset name"):
            Preset(name="my preset")

    def test_invalid_quality(self):
        with pytest.raises(ValueError, match="quality"):
            Preset(name="test", quality="ultra_hd")

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="format"):
            Preset(name="test", format="wma")

    def test_invalid_translation_format(self):
        with pytest.raises(ValueError, match="translation_format"):
            Preset(name="test", translation_format="bad")


class TestAudioSpecKey:
    def test_flac_spec(self):
        assert audio_spec_key("flac", None) == "FLAC"

    def test_mp3_spec(self):
        assert audio_spec_key("mp3", "320k") == "MP3-320k"

    def test_none_format(self):
        assert audio_spec_key(None, None) == "ORIGINAL"

    def test_opus_spec(self):
        assert audio_spec_key("opus", "160k") == "OPUS-160k"


class TestBuildAudioSpecs:
    def test_unique_specs(self):
        presets = [
            Preset(name="a", format="flac"),
            Preset(name="b", format="mp3", bitrate="192k"),
            Preset(name="c", format="mp3", bitrate="192k"),
        ]
        specs = build_audio_specs(presets)
        assert specs == {("flac", None), ("mp3", "192k")}

    def test_none_format_preserved(self):
        presets = [Preset(name="a", format=None)]
        specs = build_audio_specs(presets)
        assert specs == {(None, None)}

    def test_multiple_none_same(self):
        presets = [Preset(name="a", format=None), Preset(name="b", format=None)]
        specs = build_audio_specs(presets)
        assert specs == {(None, None)}


class TestDefaultPresets:
    def test_defaults_have_two_presets(self):
        presets = default_presets()
        assert len(presets) == 2

    def test_archive_preset(self):
        presets = default_presets()
        archive = next(p for p in presets if p.name == "archive")
        assert archive.format == "flac"
        assert archive.use_karaoke is True
        assert archive.translation_format == "separate"
        assert archive.write_lrc_file is False

    def test_portable_preset(self):
        presets = default_presets()
        portable = next(p for p in presets if p.name == "portable")
        assert portable.format == "mp3"
        assert portable.bitrate == "192k"
        assert portable.use_karaoke is False
        assert portable.translation_format == "inline"
        assert portable.write_lrc_file is True

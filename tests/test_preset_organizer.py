from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from musicvault.adapters.processors.organizer import Organizer
from musicvault.core.models import Track


def _make_track(track_id: int) -> Track:
    return Track(id=track_id, name="Test", artists=["A"], album="B", cover_url=None, raw={})


class TestRouteAudioSingleSpec:
    def test_flac_source_to_flac(self):
        """FLAC source -> single FLAC spec -> one canonical file."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.flac"
            src.write_bytes(b"fake-flac-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(src, _make_track(1), output, {("flac", None)})

            assert len(result) == 1
            assert ("flac", None) in result
            assert result[("flac", None)].name == "1.flac"

    def test_mp3_source_no_transcode(self):
        """MP3 source, format=None -> copy original."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.mp3"
            src.write_bytes(b"fake-mp3-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(src, _make_track(2), output, {(None, None)})

            assert len(result) == 1
            assert result[(None, None)].name == "2.mp3"

    def test_spec_filename_with_bitrate(self):
        """Multiple mp3 specs -> all get bitrate suffix (but these tests use copy since no ffmpeg)."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.mp3"
            src.write_bytes(b"fake-mp3-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(
                src, _make_track(3), output,
                {("mp3", "320k"), ("mp3", "192k"), ("mp3", "128k")}
            )

            assert ("mp3", "320k") in result
            assert ("mp3", "192k") in result
            assert ("mp3", "128k") in result
            # All get bitrate suffix since multiple mp3 specs
            assert result[("mp3", "320k")].name == "3_320k.mp3"
            assert result[("mp3", "192k")].name == "3_192k.mp3"
            assert result[("mp3", "128k")].name == "3_128k.mp3"

    def test_mixed_specs(self):
        """Multiple mp3 specs from one source (both copy since source is mp3)."""
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.mp3"
            src.write_bytes(b"fake-mp3-data")
            output = Path(tmp) / "out"

            org = Organizer(ffmpeg_threads=1, ffmpeg_path="")
            result = org.route_audio(
                src, _make_track(4), output,
                {("mp3", "320k"), ("mp3", "192k")}
            )

            assert len(result) == 2
            assert result[("mp3", "320k")].name == "4_320k.mp3"
            assert result[("mp3", "192k")].name == "4_192k.mp3"

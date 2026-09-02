"""End-to-end pipeline through the REAL matching, library, tagging, and
path code. Only the network boundary is faked: yt-dlp downloads produce
real ffmpeg audio, and a FakeMB serves canned MusicBrainz JSON in the
exact shape the live client returns. This catches wiring/attribute bugs
the unit tests mock away.
"""

import subprocess
from pathlib import Path

import mutagen
import pytest

import beetdrop.grab as grab_module
from beetdrop.config import Config
from beetdrop.grab import run_album_grab, run_grab
from beetdrop.search import AlbumLookup, AlbumResult, Result


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")


def make_audio(path: Path, fmt: str):
    codec = {"opus": "libopus", "mp3": "libmp3lame", "m4a": "aac"}[fmt]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", codec, str(path), "-y", "-loglevel", "error"],
        check=True, timeout=60)


# -- canned MusicBrainz payloads, matching the live JSON shape ----------


def recording_search_payload(title, artist, rec_id="rec-1", length_ms=200000):
    """Shape of GET /recording?query=... (recordings carry embedded
    releases, as the live search endpoint returns)."""
    return [{
        "id": rec_id, "title": title, "length": length_ms, "score": 100,
        "artist-credit": [{"name": artist, "artist": {"id": "art-1", "name": artist}}],
        "releases": [{
            "id": "rel-1", "status": "Official", "title": "The Album",
            "release-group": {"id": "rg-1", "primary-type": "Album",
                              "first-release-date": "1999-05-01"},
        }],
    }]


def full_release_payload(track_titles, artist="The Artist", length_ms=200000):
    tracks = [{
        "id": "trk-%d" % i, "position": i, "title": t,
        "length": length_ms,
        "recording": {"id": "rec-%d" % i, "title": t, "length": length_ms},
    } for i, t in enumerate(track_titles, start=1)]
    return {
        "id": "rel-1", "title": "The Album", "date": "1999-05-01",
        "status": "Official",
        "artist-credit": [{"name": artist, "artist": {"id": "art-1", "name": artist}}],
        "release-group": {"id": "rg-1", "primary-type": "Album",
                          "first-release-date": "1999-05-01"},
        "media": [{"position": 1, "track-count": len(tracks), "tracks": tracks}],
    }


class FakeMB:
    def __init__(self, recordings=None, releases=None, release=None, cover=None):
        self._recordings = recordings if recordings is not None else []
        self._releases = releases if releases is not None else []
        self._release = release or {}
        self._cover = cover

    def search_recordings(self, title, artist, limit=10):
        return self._recordings

    def search_releases(self, album, artist, limit=10):
        return self._releases

    def get_release(self, mbid):
        return self._release

    def fetch_cover(self, release_mbid="", release_group_mbid="", thumbnail_url=""):
        return self._cover


@pytest.fixture
def config(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    return Config(music_root=music, scratch_root=tmp_path / "scratch",
                  config_dir=tmp_path / "config", track_delay="0")


def install_fakes(monkeypatch, mb, fmt="opus"):
    def fake_download(video_id, scratch_dir, fmt=fmt, bitrate="192", **kwargs):
        path = Path(scratch_dir) / ("%s.%s" % (video_id, fmt))
        make_audio(path, fmt)
        return path

    monkeypatch.setattr(grab_module, "download_audio", fake_download)
    monkeypatch.setattr(grab_module, "get_mb_client", lambda c: mb)


class TestSingleGrabIntegration:
    def test_matched_single_files_with_full_tags(self, config, monkeypatch):
        # The matched recording is track 2 in the release ("rec-2").
        mb = FakeMB(
            recordings=recording_search_payload("Real Song", "The Artist", rec_id="rec-2"),
            release=full_release_payload(["Other", "Real Song"]),
            cover=(b"\xff\xd8jpgdata", "image/jpeg", "caa-release"),
        )
        install_fakes(monkeypatch, mb)
        monkeypatch.setattr(grab_module, "lookup_video",
                            lambda v: Result(video_id=v, title="Real Song (Official Video)",
                                             raw_title="Real Song (Official Video)",
                                             artists=["The Artist"], duration_seconds=200,
                                             thumbnail_url="http://x/t.jpg"))

        outcome = run_grab("vid1", config)
        assert outcome.verified
        # Filed under the real MusicBrainz identity, not the YT title.
        expected = config.music_root / "The Artist" / "The Album (1999)" / "02 - Real Song.opus"
        assert outcome.inbox_path == expected
        assert expected.is_file()
        assert (expected.parent / "cover.jpg").read_bytes() == b"\xff\xd8jpgdata"

        tags = mutagen.File(str(expected), easy=True)
        assert tags["title"] == ["Real Song"]
        assert tags["album"] == ["The Album"]
        assert tags["tracknumber"] == ["2"]
        raw = mutagen.File(str(expected))
        assert "rec-2" in str(raw.tags.get("MUSICBRAINZ_TRACKID"))

    def test_unmatched_single_goes_to_review(self, config, monkeypatch):
        mb = FakeMB(recordings=[], cover=None)
        install_fakes(monkeypatch, mb)
        monkeypatch.setattr(grab_module, "lookup_video",
                            lambda v: Result(video_id=v, title="Nobody Knows This",
                                             raw_title="Nobody Knows This",
                                             artists=["Obscure"], duration_seconds=200))
        outcome = run_grab("vid2", config)
        assert not outcome.verified
        assert "_review" in str(outcome.inbox_path)
        assert outcome.inbox_path.is_file()
        raw = mutagen.File(str(outcome.inbox_path))
        # Unverified marker present, no MBID.
        assert not raw.tags.get("MUSICBRAINZ_TRACKID")


class TestAlbumGrabIntegration:
    def test_matched_album_files_all_tracks(self, config, monkeypatch):
        titles = ["One", "Two", "Three"]
        mb = FakeMB(
            releases=[{"id": "rel-1", "title": "The Album", "track-count": 3,
                       "status": "Official",
                       "artist-credit": [{"name": "The Artist",
                                          "artist": {"id": "art-1", "name": "The Artist"}}]}],
            release=full_release_payload(titles),
            cover=(b"\xff\xd8jpg", "image/jpeg", "caa-release"),
        )
        install_fakes(monkeypatch, mb)
        album = AlbumResult(browse_id="b1", title="The Album",
                            artists=["The Artist"], year="1999", track_count=3)
        tracks = [Result(video_id="v%d" % i, title=t, raw_title=t,
                         artists=["The Artist"], album="The Album",
                         duration_seconds=200, track_number=i)
                  for i, t in enumerate(titles, start=1)]
        monkeypatch.setattr(grab_module, "lookup_album",
                            lambda b: AlbumLookup(album=album, tracks=tracks))

        outcome = run_album_grab("b1", config)
        assert outcome.verified
        assert outcome.delivered == 3
        folder = config.music_root / "The Artist" / "The Album (1999)"
        names = sorted(p.name for p in folder.iterdir())
        assert names == ["01 - One.opus", "02 - Two.opus", "03 - Three.opus", "cover.jpg"]
        # Each track carries its MB recording id.
        for i, t in enumerate(titles, start=1):
            f = folder / ("%02d - %s.opus" % (i, t))
            raw = mutagen.File(str(f))
            assert "rec-%d" % i in str(raw.tags.get("MUSICBRAINZ_TRACKID"))

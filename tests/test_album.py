"""Album support: lookup parsing and library-mode album grabs."""

from pathlib import Path

import pytest

import beetdrop.grab as grab_module
import beetdrop.search as search_module
from beetdrop.config import Config
from beetdrop.download import DownloadError
from beetdrop.grab import run_album_grab
from beetdrop.library import AlbumResolution, _tags_from_release_track
from beetdrop.search import AlbumLookup, AlbumResult, Result, _duration_to_seconds


class TestDurationParse:
    def test_minutes_seconds(self):
        assert _duration_to_seconds("3:45") == 225

    def test_hours(self):
        assert _duration_to_seconds("1:02:03") == 3723

    def test_garbage(self):
        assert _duration_to_seconds("") is None
        assert _duration_to_seconds(None) is None


class TestLookupAlbumParsing:
    def test_tracks_numbered_and_unavailable_reported(self, monkeypatch):
        class FakeYT:
            def get_album(self, browse_id):
                return {
                    "title": "The Album",
                    "type": "Album",
                    "year": 1999,
                    "trackCount": 3,
                    "artists": [{"name": "Artist A"}],
                    "thumbnails": [{"url": "http://x/t.jpg"}],
                    "tracks": [
                        {"videoId": "v1", "title": "One (Official Video)",
                         "artists": [{"name": "Artist A"}], "duration_seconds": 200},
                        {"videoId": None, "title": "Region Locked"},
                        {"videoId": "v3", "title": "Three", "artists": [],
                         "duration": "4:05"},
                    ],
                }

        monkeypatch.setattr(search_module, "YTMusic", FakeYT)
        lookup = search_module.lookup_album("MPREb_x")
        assert lookup.album.title == "The Album"
        assert lookup.album.year == "1999"
        assert [t.track_number for t in lookup.tracks] == [1, 3]  # gap preserved
        assert lookup.tracks[0].title == "One"  # cleaned
        assert lookup.tracks[1].duration_seconds == 245  # parsed from "4:05"
        assert lookup.tracks[1].artists == ["Artist A"]  # falls back to album artist
        assert lookup.unavailable == [{"n": 2, "title": "Region Locked"}]


def make_lookup(n=3):
    album = AlbumResult(browse_id="b1", title="The Album", artists=["Artist"],
                        year="1999", track_count=n)
    tracks = [
        Result(video_id="v%d" % i, title="Track %d" % i, raw_title="Track %d" % i,
               artists=["Artist"], album="The Album", duration_seconds=200,
               track_number=i)
        for i in range(1, n + 1)
    ]
    return AlbumLookup(album=album, tracks=tracks)


class FakeMB:
    def fetch_cover(self, **kwargs):
        return (b"\xff\xd8img", "image/jpeg", "test")


def matched_resolution(release_title="The Album", n=3):
    release = {
        "id": "rel-1", "title": release_title, "date": "1999-05-01",
        "artist-credit": [{"name": "Artist", "artist": {"id": "a-1", "name": "Artist"}}],
        "release-group": {"id": "rg-1"},
    }
    resolution = AlbumResolution(release=release)
    for i in range(1, n + 1):
        entry = {
            "track": {"id": "t-%d" % i, "position": i, "title": "Track %d" % i,
                      "recording": {"id": "rec-%d" % i, "title": "Track %d" % i}},
            "disc_number": 1, "disc_total": 1, "track_total": n,
        }
        resolution.track_tags[i] = _tags_from_release_track(
            release, entry, "Track %d" % i, "Artist")
    return resolution


@pytest.fixture
def album_env(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    config = Config(music_root=music, scratch_root=tmp_path / "scratch",
                    config_dir=tmp_path / "config", track_delay="0")

    def fake_download(video_id, scratch_dir, fmt="opus", **kwargs):
        scratch_dir.mkdir(parents=True, exist_ok=True)
        path = scratch_dir / ("%s.%s" % (video_id, fmt))
        path.write_bytes(b"audio-" + video_id.encode())
        return path

    written = []
    monkeypatch.setattr(grab_module, "download_audio", fake_download)
    monkeypatch.setattr(grab_module, "verify_audio", lambda p: None)
    monkeypatch.setattr(grab_module, "write_full_tags",
                        lambda p, t, c=None, m="image/jpeg": written.append(t))
    monkeypatch.setattr(grab_module, "lookup_album", lambda b: make_lookup())
    monkeypatch.setattr(grab_module, "get_mb_client", lambda c: FakeMB())
    monkeypatch.setattr(grab_module, "resolve_album",
                        lambda mb, t, a, tracks: matched_resolution())
    return config, written


class TestRunAlbumGrab:
    def test_matched_album_files_into_library(self, album_env):
        config, written = album_env
        outcome = run_album_grab("b1", config)
        assert outcome.delivered == 3
        assert outcome.failed == []
        assert outcome.verified
        folder = config.music_root / "Artist" / "The Album (1999)"
        assert outcome.inbox_path == folder
        names = sorted(p.name for p in folder.iterdir())
        assert names == ["01 - Track 1.opus", "02 - Track 2.opus",
                         "03 - Track 3.opus", "cover.jpg"]
        assert [t.track_number for t in written] == [1, 2, 3]
        assert all(t.recording_mbid for t in written)

    def test_unmatched_album_goes_to_review(self, album_env, monkeypatch):
        config, written = album_env
        monkeypatch.setattr(grab_module, "resolve_album",
                            lambda mb, t, a, tracks: None)
        outcome = run_album_grab("b1", config)
        assert not outcome.verified
        assert outcome.inbox_path == config.music_root / "_review" / "Artist - The Album"
        assert (outcome.inbox_path / "01 - Track 1.opus").exists()
        assert all(t.unverified for t in written)

    def test_partial_failure_still_delivers(self, album_env, monkeypatch):
        config, written = album_env
        real_download = grab_module.download_audio

        def flaky(video_id, scratch_dir, **kwargs):
            if video_id == "v2":
                raise DownloadError("video unavailable")
            return real_download(video_id, scratch_dir, **kwargs)

        monkeypatch.setattr(grab_module, "download_audio", flaky)
        outcome = run_album_grab("b1", config)
        assert outcome.delivered == 2
        assert outcome.failed == [{"n": 2, "title": "Track 2",
                                   "reason": "video unavailable"}]
        names = sorted(p.name for p in outcome.inbox_path.iterdir())
        assert "02 - Track 2.opus" not in names

    def test_total_failure_raises(self, album_env, monkeypatch):
        config, written = album_env
        monkeypatch.setattr(grab_module, "download_audio",
                            lambda *a, **k: (_ for _ in ()).throw(DownloadError("nope")))
        with pytest.raises(DownloadError):
            run_album_grab("b1", config)

    def test_no_playable_tracks_raises(self, album_env, monkeypatch):
        config, written = album_env
        empty = AlbumLookup(album=AlbumResult(browse_id="b1", title="X"),
                            tracks=[], unavailable=[{"n": 1, "title": "a"}])
        monkeypatch.setattr(grab_module, "lookup_album", lambda b: empty)
        with pytest.raises(DownloadError):
            run_album_grab("b1", config)

    def test_stage_and_detail_callbacks(self, album_env):
        config, written = album_env
        stages, details = [], []
        run_album_grab("b1", config, on_stage=stages.append, on_detail=details.append)
        assert stages == ["searching", "matching", "downloading", "moving"]
        assert details[0] == "track 1/3: Track 1"


class TestAlbumRetry:
    def test_only_tracks_limits_run_and_joins_folder(self, album_env):
        config, written = album_env
        folder = config.music_root / "Artist" / "The Album (1999)"
        folder.mkdir(parents=True)
        (folder / "01 - Track 1.opus").write_bytes(b"already")
        outcome = run_album_grab("b1", config, only_tracks={2, 3})
        assert outcome.delivered == 2
        names = sorted(p.name for p in folder.iterdir())
        assert names == ["01 - Track 1.opus", "02 - Track 2.opus",
                         "03 - Track 3.opus", "cover.jpg"]
        assert (folder / "01 - Track 1.opus").read_bytes() == b"already"

    def test_zero_recoveries_is_a_result_not_an_error(self, album_env, monkeypatch):
        config, written = album_env
        monkeypatch.setattr(grab_module, "download_audio",
                            lambda *a, **k: (_ for _ in ()).throw(DownloadError("still broken")))
        outcome = run_album_grab("b1", config, only_tracks={2})
        assert outcome.delivered == 0
        assert outcome.failed[0]["n"] == 2
        assert outcome.verified

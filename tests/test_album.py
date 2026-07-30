"""Album support: lookup parsing, grab orchestration, and API round-trip."""

from pathlib import Path

import pytest

import trackpull.grab as grab_module
import trackpull.search as search_module
from trackpull.config import Config
from trackpull.download import DownloadError
from trackpull.grab import run_album_grab
from trackpull.search import AlbumLookup, AlbumResult, Result, _duration_to_seconds


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
    album = AlbumResult(browse_id="b1", title="The Album", artists=["Artist"], year="1999")
    tracks = [
        Result(video_id="v%d" % i, title="Track %d" % i, raw_title="Track %d" % i,
               artists=["Artist"], album="The Album", duration_seconds=200,
               track_number=i)
        for i in range(1, n + 1)
    ]
    return AlbumLookup(album=album, tracks=tracks)


@pytest.fixture
def album_env(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config = Config(inbox=inbox, scratch_root=tmp_path / "scratch",
                    config_dir=tmp_path / "config")

    def fake_download(video_id, scratch_dir, fmt="opus", **kwargs):
        scratch_dir.mkdir(parents=True, exist_ok=True)
        path = scratch_dir / ("%s.%s" % (video_id, fmt))
        path.write_bytes(b"audio-" + video_id.encode())
        return path

    written_seeds = []
    monkeypatch.setattr(grab_module, "download_audio", fake_download)
    monkeypatch.setattr(grab_module, "verify_audio", lambda p: None)
    monkeypatch.setattr(grab_module, "write_seed_tags",
                        lambda p, s: written_seeds.append(s))
    monkeypatch.setattr(grab_module, "lookup_album", lambda b: make_lookup())
    return config, written_seeds


class TestRunAlbumGrab:
    def test_one_album_one_folder_all_tracks(self, album_env):
        config, seeds = album_env
        outcome = run_album_grab("b1", config)
        assert outcome.delivered == 3
        assert outcome.failed == []
        folder = config.inbox / "Artist - The Album"
        assert outcome.inbox_path == folder
        names = sorted(p.name for p in folder.iterdir())
        assert names == ["01 - Track 1.opus", "02 - Track 2.opus", "03 - Track 3.opus"]

    def test_seed_tags_carry_track_numbers(self, album_env):
        config, seeds = album_env
        run_album_grab("b1", config)
        assert [s.tracknumber for s in seeds] == [1, 2, 3]
        assert all(s.album == "The Album" for s in seeds)
        assert all(s.albumartist == "Artist" for s in seeds)

    def test_partial_failure_still_delivers(self, album_env, monkeypatch):
        config, seeds = album_env

        real_download = grab_module.download_audio

        def flaky_download(video_id, scratch_dir, **kwargs):
            if video_id == "v2":
                raise DownloadError("video unavailable")
            return real_download(video_id, scratch_dir, **kwargs)

        monkeypatch.setattr(grab_module, "download_audio", flaky_download)
        outcome = run_album_grab("b1", config)
        assert outcome.delivered == 2
        assert outcome.failed == [{"n": 2, "title": "Track 2", "reason": "video unavailable"}]
        names = sorted(p.name for p in outcome.inbox_path.iterdir())
        assert names == ["01 - Track 1.opus", "03 - Track 3.opus"]

    def test_total_failure_raises_and_delivers_nothing(self, album_env, monkeypatch):
        config, seeds = album_env

        def dead_download(video_id, scratch_dir, **kwargs):
            raise DownloadError("nope")

        monkeypatch.setattr(grab_module, "download_audio", dead_download)
        with pytest.raises(DownloadError):
            run_album_grab("b1", config)
        assert list(config.inbox.iterdir()) == []

    def test_no_playable_tracks_raises(self, album_env, monkeypatch):
        config, seeds = album_env
        empty = AlbumLookup(album=AlbumResult(browse_id="b1", title="X"),
                            tracks=[], unavailable=["a", "b"])
        monkeypatch.setattr(grab_module, "lookup_album", lambda b: empty)
        with pytest.raises(DownloadError):
            run_album_grab("b1", config)

    def test_stage_and_detail_callbacks(self, album_env):
        config, seeds = album_env
        stages, details = [], []
        run_album_grab("b1", config, on_stage=stages.append, on_detail=details.append)
        assert stages == ["searching", "downloading", "moving"]
        assert details[0] == "track 1/3: Track 1"
        assert details[-1] == "track 3/3: Track 3"


class TestAlbumRetry:
    def test_only_tracks_limits_the_run(self, album_env):
        config, seeds = album_env
        outcome = run_album_grab("b1", config, only_tracks={2})
        assert outcome.delivered == 1
        names = [p.name for p in outcome.inbox_path.iterdir()]
        assert names == ["02 - Track 2.opus"]
        assert [s.tracknumber for s in seeds] == [2]

    def test_patch_into_existing_folder(self, album_env):
        config, seeds = album_env
        existing = config.inbox / "Artist - The Album"
        existing.mkdir()
        (existing / "01 - Track 1.opus").write_bytes(b"already-there")
        outcome = run_album_grab("b1", config, only_tracks={2, 3},
                                 patch_into=existing)
        assert outcome.inbox_path == existing
        names = sorted(p.name for p in existing.iterdir())
        assert names == ["01 - Track 1.opus", "02 - Track 2.opus", "03 - Track 3.opus"]
        # Existing files are never overwritten.
        assert (existing / "01 - Track 1.opus").read_bytes() == b"already-there"
        # No temp files left behind.
        assert not [p for p in existing.iterdir() if p.name.startswith(".")]

    def test_patch_target_gone_delivers_new_folder(self, album_env):
        config, seeds = album_env
        gone = config.inbox / "Artist - The Album"  # never created
        outcome = run_album_grab("b1", config, only_tracks={2},
                                 patch_into=gone)
        assert outcome.inbox_path == config.inbox / "Artist - The Album"
        assert [p.name for p in outcome.inbox_path.iterdir()] == ["02 - Track 2.opus"]

    def test_zero_recoveries_is_a_result_not_an_error(self, album_env, monkeypatch):
        config, seeds = album_env

        def dead_download(video_id, scratch_dir, **kwargs):
            raise DownloadError("still broken")

        monkeypatch.setattr(grab_module, "download_audio", dead_download)
        existing = config.inbox / "Artist - The Album"
        existing.mkdir()
        outcome = run_album_grab("b1", config, only_tracks={2},
                                 patch_into=existing)
        assert outcome.delivered == 0
        assert outcome.failed[0]["n"] == 2
        assert outcome.inbox_path == existing

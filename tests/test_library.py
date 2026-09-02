"""Library mode: resolution against a fake MusicBrainz, filing layout,
and the full-tag roundtrip on real audio."""

import subprocess
from pathlib import Path

import pytest

from beetdrop.fulltags import FullTags, write_full_tags
from beetdrop.library import (
    album_dir,
    place_file,
    resolve_album,
    resolve_track,
    track_filename,
    write_cover_file,
)
from beetdrop.search import Result


class FakeMB:
    """Offline stand-in for MusicBrainzClient serving canned payloads."""

    def __init__(self, recordings=None, releases=None, release=None):
        self.recordings = recordings or []
        self.releases = releases or []
        self.release = release or {}

    def search_recordings(self, title, artist, limit=10):
        return self.recordings

    def search_releases(self, album, artist, limit=10):
        return self.releases

    def get_release(self, mbid):
        return self.release

    def fetch_cover(self, **kwargs):
        return None


def full_release(track_titles, album="The Album", artist="Artist",
                 length_ms=200000):
    tracks = []
    for index, title in enumerate(track_titles, start=1):
        tracks.append({
            "id": "t-%d" % index, "position": index, "title": title,
            "length": length_ms,
            "recording": {"id": "rec-%d" % index, "title": title, "length": length_ms},
        })
    return {
        "id": "rel-1", "title": album, "date": "1999-05-01", "status": "Official",
        "artist-credit": [{"name": artist, "artist": {"id": "a-1", "name": artist}}],
        "release-group": {"id": "rg-1", "primary-type": "Album",
                          "first-release-date": "1999-05-01"},
        "media": [{"position": 1, "track-count": len(tracks), "tracks": tracks}],
    }


class TestResolveTrack:
    def test_matched_track_gets_full_identity(self):
        release = full_release(["One", "Two"])
        mb = FakeMB(
            recordings=[{
                "id": "rec-2", "title": "Two", "length": 200000, "score": "100",
                "artist-credit": [{"name": "Artist", "artist": {"id": "a-1", "name": "Artist"}}],
                "releases": [{"id": "rel-1", "status": "Official",
                              "release-group": {"id": "rg-1", "primary-type": "Album",
                                                "first-release-date": "1999-05-01"}}],
            }],
            release=release,
        )
        resolution = resolve_track(mb, "Two (Official Video)", "Artist", 200)
        assert resolution.matched
        tags = resolution.tags
        assert tags.title == "Two"
        assert tags.album == "The Album"
        assert tags.track_number == 2
        assert tags.track_total == 2
        assert tags.recording_mbid == "rec-2"
        assert tags.release_mbid == "rel-1"
        assert not tags.unverified

    def test_no_match_is_unverified(self):
        mb = FakeMB(recordings=[])
        resolution = resolve_track(mb, "Obscure Song", "Nobody", 200)
        assert not resolution.matched
        assert resolution.tags.unverified


class TestResolveAlbum:
    def yt_tracks(self, n=3, duration=200):
        return [Result(video_id="v%d" % i, title="Track %d" % i,
                       raw_title="Track %d" % i, artists=["Artist"],
                       album="The Album", duration_seconds=duration,
                       track_number=i)
                for i in range(1, n + 1)]

    def search_row(self):
        return {"id": "rel-1", "title": "The Album", "track-count": 3,
                "status": "Official",
                "artist-credit": [{"name": "Artist", "artist": {"id": "a-1", "name": "Artist"}}]}

    def test_album_aligns_by_order(self):
        mb = FakeMB(releases=[self.search_row()],
                    release=full_release(["One", "Two", "Three"]))
        resolution = resolve_album(mb, "The Album", "Artist", self.yt_tracks())
        assert resolution is not None
        assert resolution.track_tags[2].title == "Two"
        assert resolution.track_tags[2].track_number == 2
        assert resolution.track_tags[2].recording_mbid == "rec-2"

    def test_duration_disagreement_rejects_album(self):
        mb = FakeMB(releases=[self.search_row()],
                    release=full_release(["One", "Two", "Three"], length_ms=500000))
        assert resolve_album(mb, "The Album", "Artist", self.yt_tracks()) is None

    def test_poor_search_scores_reject(self):
        mb = FakeMB(releases=[{"id": "x", "title": "Unrelated Compilation",
                               "track-count": 40, "artist-credit": [
                                   {"name": "Various", "artist": {"id": "v", "name": "Various"}}]}])
        assert resolve_album(mb, "The Album", "Artist", self.yt_tracks()) is None


class TestFiling:
    def tags(self, **overrides):
        base = dict(title="Song: A/B", artist="Artist", album_artist="Artist",
                    album="The Album", date="1999-05-01", track_number=3,
                    track_total=10, disc_number=1, disc_total=1)
        base.update(overrides)
        return FullTags(**base)

    def test_album_dir_layout(self, tmp_path):
        assert album_dir(tmp_path, self.tags()) == tmp_path / "Artist" / "The Album (1999)"

    def test_unverified_goes_to_review(self, tmp_path):
        directory = album_dir(tmp_path, self.tags(unverified=True))
        assert directory == tmp_path / "_review" / "Artist - Song A_B".replace("_", "")

    def test_track_filename(self):
        assert track_filename(self.tags(), "opus") == "03 - Song AB.opus"
        assert track_filename(self.tags(disc_number=2, disc_total=2), "opus") == \
            "2-03 - Song AB.opus"
        assert track_filename(self.tags(track_number=0), "mp3") == "Song AB.mp3"

    def test_place_file_atomic_and_no_overwrite(self, tmp_path):
        source = tmp_path / "src.opus"
        source.write_bytes(b"one")
        dest = tmp_path / "lib" / "a.opus"
        first = place_file(source, dest)
        assert first == dest and dest.read_bytes() == b"one"
        source.write_bytes(b"two")
        second = place_file(source, dest)
        assert second == tmp_path / "lib" / "a (2).opus"
        assert dest.read_bytes() == b"one"
        assert not [p for p in dest.parent.iterdir() if p.name.startswith(".")]

    def test_cover_file_written_once(self, tmp_path):
        write_cover_file(tmp_path, b"img", "image/jpeg")
        assert (tmp_path / "cover.jpg").read_bytes() == b"img"
        write_cover_file(tmp_path, b"other", "image/jpeg")
        assert (tmp_path / "cover.jpg").read_bytes() == b"img"


@pytest.mark.parametrize("fmt,codec", [("opus", "libopus"), ("mp3", "libmp3lame"),
                                       ("m4a", "aac")])
class TestFullTagRoundtrip:
    def test_roundtrip(self, tmp_path, fmt, codec):
        path = tmp_path / ("t.%s" % fmt)
        try:
            subprocess.run(
                ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:a", codec, str(path), "-y", "-loglevel", "error"],
                check=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pytest.skip("ffmpeg unavailable")
        tags = FullTags(
            title="Svefn-g-englar", artist="Sigur Rós", album_artist="Sigur Rós",
            album="Ágætis byrjun", date="1999-06-12", track_number=2,
            track_total=10, disc_number=1, disc_total=1,
            recording_mbid="11111111-1111-1111-1111-111111111111",
            release_mbid="22222222-2222-2222-2222-222222222222",
            release_group_mbid="33333333-3333-3333-3333-333333333333",
            artist_mbids=["44444444-4444-4444-4444-444444444444"])
        write_full_tags(path, tags, cover=b"\xff\xd8fakejpg", cover_mime="image/jpeg")

        import mutagen
        easy = mutagen.File(str(path), easy=True)
        assert easy["title"] == ["Svefn-g-englar"]
        assert easy["album"] == ["Ágætis byrjun"]
        raw = mutagen.File(str(path))
        blob = str(dict(raw.tags or {})) + str(getattr(raw.tags, "keys", lambda: [])())
        assert "11111111-1111-1111-1111-111111111111" in str(raw.tags.pprint()
            if hasattr(raw.tags, "pprint") else dict(raw.tags))


class TestSingleGrabResolves:
    """Regression: run_grab must call resolve_track with the primary
    artist. A single grab crashed with 'Result has no attribute
    primary_artist' before the property existed."""

    def test_run_grab_files_matched_track(self, tmp_path, monkeypatch):
        import beetdrop.grab as grab_module
        from beetdrop.config import Config
        from beetdrop.library import TrackResolution
        from beetdrop.search import Result

        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")

        captured = {}

        def fake_download(video_id, scratch_dir, fmt="opus", **kwargs):
            scratch_dir.mkdir(parents=True, exist_ok=True)
            p = scratch_dir / ("%s.%s" % (video_id, fmt))
            p.write_bytes(b"x")
            return p

        def fake_resolve(mb, title, artist, duration):
            captured["artist"] = artist
            return TrackResolution(
                tags=FullTags(title=title, artist=artist, album_artist=artist,
                              album="Album", date="1999", track_number=1,
                              recording_mbid="rec-1"),
                matched=True)

        class FakeMB:
            def fetch_cover(self, **k):
                return None

        monkeypatch.setattr(grab_module, "download_audio", fake_download)
        monkeypatch.setattr(grab_module, "verify_audio", lambda p: None)
        monkeypatch.setattr(grab_module, "write_full_tags",
                            lambda p, t, c=None, m="image/jpeg": None)
        monkeypatch.setattr(grab_module, "get_mb_client", lambda c: FakeMB())
        monkeypatch.setattr(grab_module, "resolve_track", fake_resolve)
        monkeypatch.setattr(grab_module, "lookup_video",
                            lambda v: Result(video_id=v, title="Song",
                                             raw_title="Song",
                                             artists=["First", "Second"],
                                             duration_seconds=200))

        outcome = grab_module.run_grab("vid", config)
        assert outcome.verified
        # The FIRST artist is what matching received, not the joined list.
        assert captured["artist"] == "First"

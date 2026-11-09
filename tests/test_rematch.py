"""Correcting a track matched to the wrong recording.

There is no acoustic fingerprinting, so a cover or a same-length
different song can get through, and anything unverifiable is filed under
_review/ with tags from YouTube. Both leave a file whose tags are wrong
and which nothing else revisits.
"""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.backfill import read_track_meta
from beetdrop.fulltags import FullTags, write_full_tags
from beetdrop.rematch import (
    apply_choice,
    describe_recording,
    iter_unverified,
    search_candidates,
    search_library,
)


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def make_opus(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", str(path), "-y", "-loglevel", "error"],
        check=True, timeout=60)


RECORDING = {
    "id": "rec-1",
    "title": "9 to 5",
    "length": 161000,
    "artist-credit": [{"name": "Dolly Parton",
                       "artist": {"id": "art-1", "name": "Dolly Parton"}}],
    "releases": [{
        "id": "rel-1", "title": "9 to 5 and Odd Jobs",
        "date": "1980-11-01",
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "artist-credit": [{"name": "Dolly Parton"}],
    }],
}

RELEASE = {
    "id": "rel-1", "title": "9 to 5 and Odd Jobs", "date": "1980-11-01",
    "release-group": {"id": "rg-1"},
    "artist-credit": [{"name": "Dolly Parton"}],
    "media": [{"position": 1, "track-count": 2, "tracks": [
        {"position": 1, "title": "9 to 5",
         "recording": {"id": "rec-1", "title": "9 to 5"},
         "artist-credit": [{"name": "Dolly Parton"}]},
        {"position": 2, "title": "Hush-A-Bye Hard Times",
         "recording": {"id": "rec-2"}},
    ]}],
}


class FakeMB:
    """Stands in for the MusicBrainz client, which is rate limited to one
    request a second and must never be hit by a test."""

    def __init__(self, recordings=None, release=None):
        self.recordings = recordings if recordings is not None else [RECORDING]
        self.release = release if release is not None else RELEASE
        self.searches = []

    def search_recordings(self, title, artist, limit=10):
        self.searches.append((title, artist))
        return self.recordings

    def get_release(self, mbid):
        return self.release


class TestDescribingCandidates:
    """Two entries with the same title and artist are usually the studio
    cut and a live or compilation version, and only the release name says
    which - so it has to be shown."""

    def test_it_carries_what_tells_two_versions_apart(self):
        described = describe_recording(RECORDING)
        assert described["title"] == "9 to 5"
        assert described["artist"] == "Dolly Parton"
        assert described["album"] == "9 to 5 and Odd Jobs"
        assert described["year"] == "1980"
        assert described["duration"] == 161

    def test_a_recording_with_no_release_still_describes(self):
        bare = dict(RECORDING, releases=[])
        described = describe_recording(bare)
        assert described["album"] == "" and described["title"] == "9 to 5"

    def test_candidates_are_not_scored_or_rejected(self):
        # The automatic checks produced the wrong answer; running them
        # again would hide the right one.
        mb = FakeMB(recordings=[RECORDING, dict(RECORDING, id="rec-9",
                                                title="Something Else")])
        found = search_candidates(mb, "9 to 5", "Dolly Parton")
        assert [c["id"] for c in found] == ["rec-1", "rec-9"]

    def test_an_empty_title_asks_musicbrainz_nothing(self):
        mb = FakeMB()
        assert search_candidates(mb, "  ", "Dolly Parton") == []
        assert mb.searches == []


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestApplyingAChoice:
    def _library(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def _unverified(self, config, name="Unknown - Some Song"):
        track = config.music_root / "_review" / name / "track.opus"
        make_opus(track)
        write_full_tags(track, FullTags(title="Some Song", artist="Unknown",
                                        album_artist="Unknown",
                                        album="Some Song", unverified=True))
        return track

    def test_it_files_the_track_where_the_new_tags_say(self, tmp_path):
        config = self._library(tmp_path)
        track = self._unverified(config)
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Some Song", "Unknown")
        assert outcome.new_path == (
            config.music_root / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
            / "01 - 9 to 5.opus")
        assert outcome.new_path.is_file() and not track.exists()

    def test_the_tags_are_rewritten_from_musicbrainz(self, tmp_path):
        config = self._library(tmp_path)
        track = self._unverified(config)
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Some Song", "Unknown")
        artist, title, album, _duration = read_track_meta(outcome.new_path)
        assert title == "9 to 5" and artist == "Dolly Parton"
        assert album == "9 to 5 and Odd Jobs"
        # The tags handed back say what was written, including the ids
        # that make the match checkable later.
        assert outcome.tags.recording_mbid == "rec-1"
        assert outcome.tags.release_mbid == "rel-1"
        assert outcome.tags.unverified is False

    def test_the_lyrics_move_with_it(self, tmp_path):
        # Lyrics are matched to the audio, not the tags, so they are still
        # right - and leaving them would strand them next to nothing.
        config = self._library(tmp_path)
        track = self._unverified(config)
        track.with_suffix(".lrc").write_text("[00:09.93]Tumble out of bed")
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Some Song", "Unknown")
        assert outcome.moved_lyrics
        assert outcome.new_path.with_suffix(".lrc").read_text().startswith(
            "[00:09.93]")
        assert not track.with_suffix(".lrc").exists()

    def test_its_music_video_moves_with_it_under_the_new_title(self, tmp_path):
        # Plex links a video to a track by title, so a video left under
        # the old name in the old folder would be linked to nothing.
        config = self._library(tmp_path)
        track = self._unverified(config)
        (track.parent / "Some Song-video.mp4").write_bytes(b"clip")
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Some Song", "Unknown")
        assert outcome.moved_video
        assert (outcome.new_path.parent / "9 to 5-video.mp4").read_bytes() == b"clip"
        # And the folder it left is empty, so it goes too.
        assert not track.parent.exists()

    def test_retagging_in_place_renames_the_video(self, tmp_path):
        config = self._library(tmp_path)
        track = self._unverified(config)
        (track.parent / "Some Song-video.mp4").write_bytes(b"clip")
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Some Song", "Unknown", move=False)
        assert outcome.moved_video
        assert (track.parent / "9 to 5-video.mp4").is_file()
        assert not (track.parent / "Some Song-video.mp4").exists()

    def test_the_emptied_review_folder_is_removed(self, tmp_path):
        config = self._library(tmp_path)
        track = self._unverified(config)
        folder = track.parent
        apply_choice(config, FakeMB(), track, "rec-1", "Some Song", "Unknown")
        assert not folder.exists()

    def test_a_folder_that_still_holds_something_is_kept(self, tmp_path):
        config = self._library(tmp_path)
        track = self._unverified(config)
        (track.parent / "other.opus").write_bytes(b"x")
        apply_choice(config, FakeMB(), track, "rec-1", "Some Song", "Unknown")
        assert track.parent.is_dir()

    def test_a_recording_musicbrainz_no_longer_offers_is_refused(self, tmp_path):
        config = self._library(tmp_path)
        track = self._unverified(config)
        with pytest.raises(LookupError):
            apply_choice(config, FakeMB(), track, "gone", "Some Song", "Unknown")
        assert track.is_file()      # nothing moved

    def test_a_missing_file_is_refused(self, tmp_path):
        config = self._library(tmp_path)
        missing = config.music_root / "_review" / "x" / "gone.opus"
        with pytest.raises(FileNotFoundError):
            apply_choice(config, FakeMB(), missing, "rec-1", "S", "A")

    def test_a_recording_not_on_its_release_keeps_the_identity(self, tmp_path):
        # MusicBrainz data drifts; losing the track numbering is fine,
        # losing the song is not.
        config = self._library(tmp_path)
        track = self._unverified(config)
        empty = dict(RELEASE, media=[{"position": 1, "track-count": 0,
                                      "tracks": []}])
        outcome = apply_choice(config, FakeMB(release=empty), track, "rec-1",
                               "Some Song", "Unknown")
        assert outcome.tags.title == "9 to 5"
        assert outcome.tags.album == "9 to 5 and Odd Jobs"
        assert outcome.tags.track_number == 0

    def test_iter_unverified_finds_only_review(self, tmp_path):
        config = self._library(tmp_path)
        review = self._unverified(config)
        clean = config.music_root / "Dolly Parton" / "Album" / "01 - x.opus"
        make_opus(clean)
        assert list(iter_unverified(config.music_root)) == [review]

    def test_no_review_folder_is_not_an_error(self, tmp_path):
        config = self._library(tmp_path)
        assert list(iter_unverified(config.music_root)) == []


class TestTheEndpoints:
    def _config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_candidates_come_back_described(self, tmp_path, monkeypatch):
        import beetdrop.grab as grab
        config = self._config(tmp_path)
        monkeypatch.setattr(grab, "get_mb_client", lambda cfg: FakeMB())
        with TestClient(create_app(config)) as client:
            body = client.get(
                "/api/match/candidates?title=9 to 5&artist=Dolly").json()
        assert body["candidates"][0]["album"] == "9 to 5 and Odd Jobs"

    def test_an_empty_title_returns_nothing(self, tmp_path):
        config = self._config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/match/candidates?title=%20").json() == {
                "candidates": []}

    def test_a_path_outside_the_library_is_refused(self, tmp_path):
        # The path arrives over HTTP, so it is held to the library.
        config = self._config(tmp_path)
        with TestClient(create_app(config)) as client:
            response = client.post("/api/match/apply", json={
                "path": "/etc/passwd", "recording_id": "rec-1"})
        assert response.status_code == 400

    def test_an_empty_review_folder_lists_nothing(self, tmp_path):
        config = self._config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/match/unverified").json() == {
                "tracks": [], "total": 0}

    def test_the_page_wires_it_up(self):
        page = (Path(__file__).parent.parent / "beetdrop" / "static"
                / "index.html").read_text()
        source = (Path(__file__).parent.parent / "beetdrop" / "static"
                  / "app.js").read_text()
        assert 'click="loadUnverified"' in page
        assert "findMatches(track)" in page and "applyMatch(track, c)" in page
        for method in ("loadUnverified()", "findMatches(", "applyMatch("):
            assert method in source, method
        assert "/api/match/apply" in source


class TestFindingAConfidentlyWrongMatch:
    """_review/ only holds what matching *knew* it could not verify. A
    match that was confidently wrong - a cover, a re-upload, the other
    band with a similar name - is filed as verified and looks settled, so
    it never appears in that list. It has to be reachable by searching
    for whatever it is wrongly called."""

    def _library(self, tmp_path, *relative):
        root = tmp_path / "music"
        for name in relative:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        return root

    def test_it_finds_a_verified_track_by_its_wrong_name(self, tmp_path):
        root = self._library(
            tmp_path,
            "Electric Light Orchestra Part II/Thousand Eyes (1990)/01 - Thousand Eyes.opus",
            "Dolly Parton/9 to 5 (1980)/01 - 9 to 5.opus")
        found = search_library(root, "thousand eyes")
        assert [p.name for p in found] == ["01 - Thousand Eyes.opus"]

    def test_words_can_match_across_folder_and_file(self, tmp_path):
        root = self._library(tmp_path, "Dolly Parton/9 to 5 (1980)/01 - 9 to 5.opus")
        assert search_library(root, "dolly 9 to 5")
        assert not search_library(root, "dolly jolene")

    def test_the_search_ignores_case(self, tmp_path):
        root = self._library(tmp_path, "Dolly Parton/Album/01 - Jolene.opus")
        assert search_library(root, "JOLENE")

    def test_an_empty_query_matches_nothing(self, tmp_path):
        root = self._library(tmp_path, "A/B/c.opus")
        assert search_library(root, "   ") == []

    def test_non_audio_is_ignored(self, tmp_path):
        root = self._library(tmp_path, "A/B/cover.jpg", "A/B/song.opus")
        assert [p.name for p in search_library(root, "")] == []
        assert [p.name for p in search_library(root, "A")] == ["song.opus"]

    def test_the_limit_is_honoured(self, tmp_path):
        root = self._library(tmp_path, *["A/B/song%d.opus" % i for i in range(10)])
        assert len(search_library(root, "song", limit=3)) == 3

    def test_the_endpoint_returns_what_the_tags_say(self, tmp_path, monkeypatch):
        import beetdrop.backfill as backfill
        root = self._library(tmp_path, "A/B/song.opus")
        config = Config(music_root=root, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        monkeypatch.setattr(backfill, "read_track_meta",
                            lambda p: ("Wrong Band", "Wrong Song", "Al", 200))
        with TestClient(create_app(config)) as client:
            body = client.get("/api/match/tracks?q=song").json()
        assert body["total"] == 1
        assert body["tracks"][0]["artist"] == "Wrong Band"

    def test_the_page_offers_the_library_search(self):
        page = (Path(__file__).parent.parent / "beetdrop" / "static"
                / "index.html").read_text()
        source = (Path(__file__).parent.parent / "beetdrop" / "static"
                  / "app.js").read_text()
        assert 'click="searchLibrary"' in page
        assert "searchLibrary()" in source and "/api/match/tracks?q=" in source


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestWhetherTheFileMoves:
    """A wrong match is not only wrong tags - the folder and filename were
    built from them too - so the file moves by default. A library whose
    layout is not Beetdrop's can say no."""

    def _config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def _filed(self, config):
        # Already in the library, under the wrong artist and album.
        track = (config.music_root / "Wrong Band" / "Wrong Album (1999)"
                 / "01 - Wrong Song.opus")
        make_opus(track)
        write_full_tags(track, FullTags(title="Wrong Song", artist="Wrong Band",
                                        album_artist="Wrong Band",
                                        album="Wrong Album"))
        return track

    def test_by_default_it_moves_out_of_the_wrong_folder(self, tmp_path):
        config = self._config(tmp_path)
        track = self._filed(config)
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Wrong Song", "Wrong Band")
        assert outcome.new_path.parent.name == "9 to 5 and Odd Jobs (1980)"
        assert not track.exists()

    def test_move_false_corrects_the_tags_where_it_stands(self, tmp_path):
        config = self._config(tmp_path)
        track = self._filed(config)
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Wrong Song", "Wrong Band", move=False)
        assert outcome.new_path == track and track.is_file()
        artist, title, _album, _d = read_track_meta(track)
        assert title == "9 to 5" and artist == "Dolly Parton"

    def test_not_moving_leaves_the_lyrics_alone(self, tmp_path):
        config = self._config(tmp_path)
        track = self._filed(config)
        track.with_suffix(".lrc").write_text("[00:01.00]words")
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Wrong Song", "Wrong Band", move=False)
        assert not outcome.moved_lyrics
        assert track.with_suffix(".lrc").is_file()

    def test_the_page_offers_the_choice(self):
        page = (Path(__file__).parent.parent / "beetdrop" / "static"
                / "index.html").read_text()
        source = (Path(__file__).parent.parent / "beetdrop" / "static"
                  / "app.js").read_text()
        assert 'v-model="track.move"' in page
        assert "move: track.move" in source


class TestCandidateCoverArt:
    def test_a_candidate_links_its_release_cover(self):
        described = describe_recording(RECORDING)
        assert described["artwork"] == (
            "https://coverartarchive.org/release/rel-1/front-250")

    def test_a_recording_with_no_release_has_no_cover(self):
        assert describe_recording(dict(RECORDING, releases=[]))["artwork"] == ""

    def test_the_page_hides_art_that_does_not_load(self):
        # Plenty of releases have no cover in the archive; a broken-image
        # icon on every candidate would be worse than none.
        page = (Path(__file__).parent.parent / "beetdrop" / "static"
                / "index.html").read_text()
        source = (Path(__file__).parent.parent / "beetdrop" / "static"
                  / "app.js").read_text()
        assert 'error="hideImage"' in page
        assert "hideImage(event)" in source

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

    def __init__(self, recordings=None, release=None, releases=None):
        self.recordings = recordings if recordings is not None else [RECORDING]
        self.release = release if release is not None else RELEASE
        self.releases = releases or {}
        self.searches = []
        self.albums = []

    def search_recordings(self, title, artist, limit=10, album=""):
        self.searches.append((title, artist))
        self.albums.append(album)
        return self.recordings

    def get_release(self, mbid):
        return self.releases.get(mbid, self.release)


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



# The same recording on its original album and on a later compilation:
# left to itself the original wins, which is wrong for someone filing
# their compilation.
HITS_RELEASE_ROW = {
    "id": "rel-hits", "title": "Greatest Hits", "date": "1982-01-01",
    "status": "Official",
    "release-group": {"id": "rg-hits", "primary-type": "Album",
                      "secondary-types": ["Compilation"]},
    "artist-credit": [{"name": "Dolly Parton"}],
}
ON_TWO = dict(RECORDING, releases=[dict(RECORDING["releases"][0], status="Official"),
                                   HITS_RELEASE_ROW])
HITS_RELEASE = {
    "id": "rel-hits", "title": "Greatest Hits", "date": "1982-01-01",
    "release-group": {"id": "rg-hits"},
    "artist-credit": [{"name": "Dolly Parton"}],
    "media": [{"position": 1, "track-count": 10, "tracks": [
        {"position": 7, "title": "9 to 5",
         "recording": {"id": "rec-1", "title": "9 to 5"},
         "artist-credit": [{"name": "Dolly Parton"}]},
    ]}],
}


class TestNamingTheAlbum:
    def test_it_narrows_the_search(self):
        mb = FakeMB()
        search_candidates(mb, "9 to 5", "Dolly Parton", album=" Greatest Hits ")
        assert mb.albums == ["Greatest Hits"]

    def test_without_one_the_search_is_as_before(self):
        mb = FakeMB()
        search_candidates(mb, "9 to 5", "Dolly Parton")
        assert mb.albums == [""]

    def test_the_named_album_is_the_one_shown(self):
        assert describe_recording(ON_TWO)["album"] == "9 to 5 and Odd Jobs"
        assert describe_recording(ON_TWO, "greatest hits")["album"] == "Greatest Hits"

    def test_an_album_it_is_not_on_falls_back_to_the_usual_choice(self):
        assert describe_recording(ON_TWO, "Jolene")["album"] == "9 to 5 and Odd Jobs"

    def test_bracketed_extras_do_not_stop_a_match(self):
        from beetdrop.matching import releases_named
        rows = [{"id": "a", "title": "Rumours (Deluxe Edition)"},
                {"id": "b", "title": "Tusk"}]
        assert [r["id"] for r in releases_named(rows, "Rumours")] == ["a"]
        assert releases_named(rows, "") == []

    def test_musicbrainz_is_asked_for_that_release(self, tmp_path, monkeypatch):
        from urllib.parse import unquote

        from beetdrop.mb import MusicBrainzClient

        client = MusicBrainzClient(tmp_path / "mb.sqlite3")
        urls = []
        monkeypatch.setattr(client, "_cached_get",
                            lambda url: urls.append(unquote(url)) or {"recordings": []})
        client.search_recordings("9 to 5", "Dolly Parton", album="Greatest Hits")
        client.search_recordings("9 to 5", "Dolly Parton")
        client.close()
        assert 'AND release:"Greatest Hits"' in urls[0]
        assert "release:" not in urls[1]


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestFilingUnderTheNamedAlbum:
    def test_it_lands_on_the_album_asked_for(self, tmp_path):
        music = tmp_path / "music"
        track = music / "_review" / "Unknown - 9 to 5" / "track.opus"
        make_opus(track)
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        mb = FakeMB(recordings=[ON_TWO], releases={"rel-hits": HITS_RELEASE})
        outcome = apply_choice(config, mb, track, "rec-1", "9 to 5",
                               "Dolly Parton", album="Greatest Hits")
        assert outcome.tags.album == "Greatest Hits"
        assert outcome.tags.track_number == 7
        assert "Greatest Hits" in str(outcome.new_path)
        # The same search that offered it found it again.
        assert mb.albums == ["Greatest Hits"]


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

    def test_its_music_video_moves_with_it_under_the_new_name(self, tmp_path):
        # Plex links a video to a track by the track's filename, so a
        # video left under the old name in the old folder links to nothing.
        config = self._library(tmp_path)
        track = self._unverified(config)
        (track.parent / "track.mp4").write_bytes(b"clip")
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Some Song", "Unknown")
        assert outcome.moved_video
        expected = outcome.new_path.with_suffix(".mp4")
        assert expected.read_bytes() == b"clip"
        # And the folder it left is empty, so it goes too.
        assert not track.parent.exists()

    def test_retagging_in_place_fixes_a_title_named_video(self, tmp_path):
        # The file keeps its name, so a correctly named video stays put; one
        # an earlier version named for the title gets the track's name.
        config = self._library(tmp_path)
        track = self._unverified(config)
        (track.parent / "Some Song-video.mp4").write_bytes(b"clip")
        outcome = apply_choice(config, FakeMB(), track, "rec-1",
                               "Some Song", "Unknown", move=False)
        assert outcome.moved_video
        assert (track.parent / "track.mp4").is_file()
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

    def test_the_album_is_passed_through(self, tmp_path, monkeypatch):
        import beetdrop.grab as grab
        config = self._config(tmp_path)
        mb = FakeMB(recordings=[ON_TWO])
        monkeypatch.setattr(grab, "get_mb_client", lambda cfg: mb)
        with TestClient(create_app(config)) as client:
            body = client.get("/api/match/candidates?title=9 to 5"
                              "&artist=Dolly&album=Greatest Hits").json()
        assert mb.albums == ["Greatest Hits"]
        assert body["candidates"][0]["album"] == "Greatest Hits"

    def test_an_empty_title_returns_nothing(self, tmp_path):
        config = self._config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/match/candidates?title=%20").json() == {
                "candidates": [], "note": ""}

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



class StepMB(FakeMB):
    """MusicBrainz that finds the recording only one way: by `exact`
    phrases or by `words`, with or without the artist and the album."""

    def __init__(self, finds, **kwargs):
        super().__init__(recordings=[ON_TWO], **kwargs)
        self.finds = finds   # set of ("exact"|"words", by_artist: bool, on_album: bool)
        self.asked = []

    def _ask(self, how, artist, album):
        key = (how, bool(artist), bool(album))
        self.asked.append(key)
        return self.recordings if key in self.finds else []

    def search_recordings(self, title, artist, limit=10, album=""):
        return self._ask("exact", artist, album)

    def search_recordings_loose(self, title, artist, limit=10, album=""):
        return self._ask("words", artist, album)


class TestBroadeningTheSearch:
    """A real search - "Kyle Gordon - We Are the Zorg", on "Kyle Gordon Is
    Everywhere" - found nothing, while Plex matched it fine."""

    def test_an_exact_hit_is_not_broadened(self):
        from beetdrop.rematch import find_candidates
        mb = StepMB({("exact", True, True)})
        found, note = find_candidates(mb, "9 to 5", "Dolly Parton", album="Greatest Hits")
        assert found and note == "" and mb.asked == [("exact", True, True)]

    def test_without_the_album_next(self):
        from beetdrop.rematch import find_candidates
        mb = StepMB({("exact", True, False)})
        found, note = find_candidates(mb, "9 to 5", "Dolly Parton", album="Greatest Hitz")
        assert found and "any album" in note
        assert mb.asked == [("exact", True, True), ("exact", True, False)]

    def test_then_on_the_album_credited_to_anyone(self):
        from beetdrop.rematch import find_candidates
        mb = StepMB({("exact", False, True)})
        found, note = find_candidates(mb, "We Are the Zorg", "Kyle Gordon",
                                      album="Kyle Gordon Is Everywhere")
        assert found and "credited to someone else" in note
        assert mb.asked == [("exact", True, True), ("exact", True, False),
                            ("exact", False, True)]

    def test_then_word_by_word(self):
        from beetdrop.rematch import find_candidates
        mb = StepMB({("words", True, False)})
        found, note = find_candidates(mb, "We Are the Zorg", "Kyle Gordon",
                                      album="Kyle Gordon Is Everywhere")
        assert found and "closest" in note
        assert mb.asked == [("exact", True, True), ("exact", True, False),
                            ("exact", False, True), ("words", True, True),
                            ("words", True, False)]

    def test_last_that_title_by_anyone(self):
        from beetdrop.rematch import find_candidates
        mb = StepMB({("exact", False, False)})
        found, note = find_candidates(mb, "We Are the Zorg", "Kyle Gordon")
        assert found and "by anyone" in note
        assert mb.asked == [("exact", True, False), ("words", True, False),
                            ("exact", False, False)]

    def test_nothing_anywhere_is_nothing(self):
        from beetdrop.rematch import find_candidates
        assert find_candidates(StepMB(set()), "Song", "Nobody") == ([], "")

    def test_a_pick_from_a_looser_search_is_found_again(self, tmp_path):
        if not have_ffmpeg():
            pytest.skip("ffmpeg unavailable")
        music = tmp_path / "music"
        track = music / "_review" / "Kyle Gordon - We Are the Zorg" / "track.opus"
        make_opus(track)
        config = Config(music_root=music, scratch_root=tmp_path / "s", config_dir=tmp_path / "c")
        mb = StepMB({("words", True, False)}, releases={"rel-hits": HITS_RELEASE})
        outcome = apply_choice(config, mb, track, "rec-1", "9 to 5", "Dolly Parton")
        assert outcome.tags.recording_mbid == "rec-1"

    def test_the_word_search_asks_musicbrainz_by_words(self, tmp_path, monkeypatch):
        from urllib.parse import unquote

        from beetdrop.mb import MusicBrainzClient
        client = MusicBrainzClient(tmp_path / "mb.sqlite3")
        urls = []
        monkeypatch.setattr(client, "_cached_get",
                            lambda url: urls.append(unquote(url)) or {"recordings": []})
        client.search_recordings_loose("We Are the Zorg (feat. Billy)", "Kyle Gordon",
                                       album="Kyle Gordon Is Everywhere")
        client.close()
        assert ('recording:(We Are the Zorg \\(feat. Billy\\)) AND artist:(Kyle Gordon) '
                'AND release:(Kyle Gordon Is Everywhere)') in urls[0]


ZORG_RELEASE_ID = "1b2c3d4e-0000-4000-8000-00000000abcd"
ZORG_RECORDING_ID = "9a8b7c6d-0000-4000-8000-00000000beef"
ZORG_RELEASE = {
    "id": ZORG_RELEASE_ID, "title": "Kyle Gordon Is Everywhere", "date": "2024-08-09",
    "release-group": {"id": "rg-zorg"},
    "artist-credit": [{"name": "Kyle Gordon", "artist": {"id": "art-kg", "name": "Kyle Gordon"}}],
    "media": [{"position": 1, "track-count": 2, "tracks": [
        {"position": 1, "title": "Intro", "length": 60000,
         "recording": {"id": "rec-intro", "title": "Intro"}},
        {"position": 2, "title": "We Are the Zorg", "length": 201000,
         "recording": {"id": ZORG_RECORDING_ID, "title": "We Are the Zorg"}},
    ]}],
}
ZORG_RECORDING = {
    "id": ZORG_RECORDING_ID, "title": "We Are the Zorg", "length": 201000,
    "artist-credit": [{"name": "Kyle Gordon", "artist": {"id": "art-kg", "name": "Kyle Gordon"}}],
    "releases": [dict(ZORG_RELEASE, media=[])],
}


class LinkMB(FakeMB):
    """MusicBrainz whose search finds nothing - its index has not caught up
    - while the pages themselves are there."""

    def __init__(self):
        super().__init__(recordings=[], release=ZORG_RELEASE)
        self.looked_up = []

    def search_recordings_loose(self, title, artist, limit=10, album=""):
        self.searches.append((title, artist))
        return []

    def get_release(self, mbid):
        self.looked_up.append(("release", mbid))
        return ZORG_RELEASE if mbid == ZORG_RELEASE_ID else {}

    def get_recording(self, mbid):
        self.looked_up.append(("recording", mbid))
        return ZORG_RECORDING if mbid == ZORG_RECORDING_ID else {}


class TestPastingAMusicBrainzLink:
    """"I checked and the album was on MusicBrainz" - and still Fix match
    found nothing. A link to the page skips the search."""

    def test_links_are_recognised(self):
        from beetdrop.rematch import mb_link
        assert mb_link("https://musicbrainz.org/release/%s" % ZORG_RELEASE_ID) == \
            ("release", ZORG_RELEASE_ID)
        assert mb_link(" musicbrainz.org/recording/%s?tab=x " % ZORG_RECORDING_ID.upper()) == \
            ("recording", ZORG_RECORDING_ID)
        assert mb_link("We Are the Zorg") is None
        assert mb_link("https://musicbrainz.org/artist/%s" % ZORG_RELEASE_ID) is None

    def test_a_release_link_lists_its_tracks(self):
        from beetdrop.rematch import find_candidates
        mb = LinkMB()
        rows, note = find_candidates(
            mb, "https://musicbrainz.org/release/%s" % ZORG_RELEASE_ID, "Kyle Gordon")
        assert [r["title"] for r in rows] == ["Intro", "We Are the Zorg"]
        assert all(r["release_id"] == ZORG_RELEASE_ID for r in rows)
        assert rows[1]["album"] == "Kyle Gordon Is Everywhere" and rows[1]["year"] == "2024"
        assert rows[1]["duration"] == 201 and rows[1]["position"] == "2"
        assert "Kyle Gordon Is Everywhere" in note
        assert mb.searches == []                 # nothing searched

    def test_a_recording_link_gives_that_recording(self):
        from beetdrop.rematch import find_candidates
        mb = LinkMB()
        rows, _note = find_candidates(
            mb, "https://musicbrainz.org/recording/%s" % ZORG_RECORDING_ID, "")
        assert [r["id"] for r in rows] == [ZORG_RECORDING_ID]
        assert mb.searches == []

    def test_a_link_to_nothing_finds_nothing(self):
        from beetdrop.rematch import find_candidates
        rows, _ = find_candidates(
            LinkMB(), "https://musicbrainz.org/recording/%s" % ZORG_RELEASE_ID, "")
        assert rows == []

    @pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg")
    def test_a_track_picked_from_a_release_is_filed_under_it(self, tmp_path):
        config = TestApplyingAChoice()._library(tmp_path)
        track = TestApplyingAChoice()._unverified(config, "Kyle Gordon - We Are the Zorg")
        link = "https://musicbrainz.org/release/%s" % ZORG_RELEASE_ID
        mb = LinkMB()
        outcome = apply_choice(config, mb, track, ZORG_RECORDING_ID, link, "Kyle Gordon",
                               release_id=ZORG_RELEASE_ID)
        assert outcome.new_path == (config.music_root / "Kyle Gordon"
                                    / "Kyle Gordon Is Everywhere (2024)"
                                    / "02 - We Are the Zorg.opus")
        assert outcome.tags.release_mbid == ZORG_RELEASE_ID
        assert outcome.tags.track_number == 2 and outcome.tags.track_total == 2
        assert mb.searches == []

    @pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg")
    def test_a_recording_link_pick_is_looked_up_by_id(self, tmp_path):
        config = TestApplyingAChoice()._library(tmp_path)
        track = TestApplyingAChoice()._unverified(config, "Kyle Gordon - We Are the Zorg")
        link = "https://musicbrainz.org/recording/%s" % ZORG_RECORDING_ID
        mb = LinkMB()
        outcome = apply_choice(config, mb, track, ZORG_RECORDING_ID, link, "")
        assert outcome.tags.title == "We Are the Zorg"
        assert outcome.tags.album == "Kyle Gordon Is Everywhere"
        assert mb.searches == []

    @pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg")
    def test_a_recording_on_another_release_is_refused(self, tmp_path):
        config = TestApplyingAChoice()._library(tmp_path)
        track = TestApplyingAChoice()._unverified(config)
        with pytest.raises(LookupError):
            apply_choice(config, LinkMB(), track, "rec-not-there", "x", "",
                         release_id=ZORG_RELEASE_ID)
        assert track.is_file()

    @pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg")
    def test_the_endpoint_passes_the_release_on(self, tmp_path, monkeypatch):
        config = TestApplyingAChoice()._library(tmp_path)
        track = TestApplyingAChoice()._unverified(config, "Kyle Gordon - We Are the Zorg")
        mb = LinkMB()
        monkeypatch.setattr("beetdrop.grab.get_mb_client", lambda c: mb)
        link = "https://musicbrainz.org/release/%s" % ZORG_RELEASE_ID
        with TestClient(create_app(config)) as client:
            listed = client.get("/api/match/candidates",
                                params={"title": link, "artist": ""}).json()
            pick = listed["candidates"][1]
            body = client.post("/api/match/apply", json={
                "path": str(track), "recording_id": pick["id"], "title": link,
                "artist": "", "release_id": pick["release_id"]}).json()
        assert body["ok"] and body["path"].endswith("02 - We Are the Zorg.opus")


class TestForgettingEmptyAnswersSooner:
    """An empty answer is asked again after an hour; a real one is kept."""

    def make(self, tmp_path, monkeypatch, answers):
        import beetdrop.mb as mb_module

        calls = []

        class Response:
            status_code = 200
            ok = True
            headers = {}

            def __init__(self, data):
                self.data = data

            def json(self):
                return self.data

        def fake_get(url, **kwargs):
            calls.append(url)
            return Response(answers.pop(0))

        monkeypatch.setattr(mb_module.requests, "get", fake_get)
        monkeypatch.setattr(mb_module, "MIN_INTERVAL", 0)
        client = mb_module.MusicBrainzClient(tmp_path / "cache.db")
        client._next_ok = 0
        return mb_module, client, calls

    def test_nothing_found_is_asked_again_after_an_hour(self, tmp_path, monkeypatch):
        mb_module, client, calls = self.make(
            tmp_path, monkeypatch, [{"recordings": []}, {"recordings": [{"id": "r"}]}])
        clock = [1_000_000.0]
        monkeypatch.setattr(mb_module.time, "time", lambda: clock[0])
        assert client.search_recordings("Zorg", "Kyle Gordon") == []
        clock[0] += 600
        assert client.search_recordings("Zorg", "Kyle Gordon") == []
        assert len(calls) == 1                       # still remembered
        clock[0] += 3600
        assert client.search_recordings("Zorg", "Kyle Gordon") == [{"id": "r"}]
        assert len(calls) == 2

    def test_something_found_is_kept_a_month(self, tmp_path, monkeypatch):
        mb_module, client, calls = self.make(
            tmp_path, monkeypatch, [{"recordings": [{"id": "r"}]}])
        clock = [1_000_000.0]
        monkeypatch.setattr(mb_module.time, "time", lambda: clock[0])
        client.search_recordings("Zorg", "Kyle Gordon")
        clock[0] += 7 * 24 * 3600
        assert client.search_recordings("Zorg", "Kyle Gordon") == [{"id": "r"}]
        assert len(calls) == 1

    def test_a_looked_up_page_is_not_mistaken_for_an_empty_search(self):
        from beetdrop.mb import _found_nothing
        assert _found_nothing({"recordings": []})
        assert _found_nothing({"releases": [], "count": 0})
        assert not _found_nothing({"id": "x", "releases": []})   # a recording with none
        assert not _found_nothing({"recordings": [{"id": "r"}]})


# What the real MusicBrainz answered (the probe workflow, 2026-11-13): the
# song is on Kyle Gordon's album but credited to one of its characters.
EVERYWHERE_ID = "21b129d3-f131-4a94-97d8-6ec692dcb45c"
BILLY = [{"name": "Billy Mainframe", "artist": {"id": "art-billy", "name": "Billy Mainframe"}}]
KYLE = [{"name": "Kyle Gordon", "artist": {"id": "art-kg", "name": "Kyle Gordon"}}]
EVERYWHERE = {
    "id": EVERYWHERE_ID, "title": "Kyle Gordon Is Everywhere", "date": "2026-10-02",
    "status": "Official", "release-group": {"id": "rg-everywhere", "primary-type": "Album"},
    "artist-credit": KYLE,
    "media": [{"position": 1, "track-count": 3, "tracks": [
        {"position": 1, "title": "Kyle Gordon Is Everywhere", "length": 90000,
         "artist-credit": KYLE, "recording": {"id": "rec-title", "artist-credit": KYLE}},
        {"position": 2, "title": "We are the Zorg", "length": 176000,
         "artist-credit": BILLY, "recording": {"id": "rec-zorg", "artist-credit": BILLY}},
        {"position": 3, "title": "Outro", "length": 60000,
         "artist-credit": KYLE, "recording": {"id": "rec-outro", "artist-credit": KYLE}},
    ]}],
}
ZORG_BY_BILLY = {
    "id": "rec-zorg", "title": "We are the Zorg", "length": 176000, "score": 100,
    "artist-credit": BILLY,
    "releases": [{"id": EVERYWHERE_ID, "title": "Kyle Gordon Is Everywhere",
                  "date": "2026-10-02", "status": "Official", "artist-credit": KYLE,
                  "release-group": {"id": "rg-everywhere", "primary-type": "Album"}}],
}


class RealZorgMB:
    """Answers the way MusicBrainz does for it: nothing by artist "Kyle
    Gordon", the song by title (and album) alone."""

    def __init__(self):
        self.asked = []

    def search_recordings(self, title, artist, limit=10, album=""):
        self.asked.append(("exact", artist, album))
        if artist == "Kyle Gordon" or title.lower() != "we are the zorg":
            return []
        if album and album != "Kyle Gordon Is Everywhere":
            return []
        return [ZORG_BY_BILLY]

    def search_recordings_loose(self, title, artist, limit=10, album=""):
        self.asked.append(("words", artist, album))
        return []

    def search_releases(self, album, artist, limit=10):
        self.asked.append(("release", artist, album))
        return [EVERYWHERE] if album.lower() == "kyle gordon is everywhere" else []

    def get_release(self, mbid):
        return EVERYWHERE if mbid == EVERYWHERE_ID else {}

    def get_recording(self, mbid):
        return ZORG_BY_BILLY if mbid == "rec-zorg" else {}


class TestASongCreditedToSomeoneElseOnTheAlbum:
    def test_found_on_the_album(self):
        from beetdrop.rematch import find_candidates
        rows, note = find_candidates(RealZorgMB(), "We Are the Zorg", "Kyle Gordon",
                                     album="Kyle Gordon Is Everywhere")
        assert [(r["title"], r["artist"], r["album"]) for r in rows] == [
            ("We are the Zorg", "Billy Mainframe", "Kyle Gordon Is Everywhere")]
        assert "credited to someone else" in note

    def test_found_without_the_album_too(self):
        from beetdrop.rematch import find_candidates
        rows, note = find_candidates(RealZorgMB(), "We Are the Zorg", "Kyle Gordon")
        assert [r["id"] for r in rows] == ["rec-zorg"] and "by anyone" in note

    def test_a_title_spelled_differently_is_found_in_the_track_list(self):
        from beetdrop.rematch import find_candidates
        rows, note = find_candidates(RealZorgMB(), "We R Teh Zorgg", "Kyle Gordon",
                                     album="Kyle Gordon Is Everywhere")
        assert rows[0]["title"] == "We are the Zorg"           # closest first
        assert len(rows) == 3 and all(r["release_id"] == EVERYWHERE_ID for r in rows)
        assert "tracks of Kyle Gordon Is Everywhere" in note

    def test_an_album_that_is_not_there_lists_nothing(self):
        from beetdrop.rematch import find_candidates
        assert find_candidates(RealZorgMB(), "Nope", "Kyle Gordon",
                               album="Some Other Album") == ([], "")

    @pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg")
    def test_filed_under_kyle_gordon_as_plex_has_it(self, tmp_path):
        config = TestApplyingAChoice()._library(tmp_path)
        track = TestApplyingAChoice()._unverified(config, "Kyle Gordon - We Are the Zorg")
        outcome = apply_choice(config, RealZorgMB(), track, "rec-zorg", "We Are the Zorg",
                               "Kyle Gordon", album="Kyle Gordon Is Everywhere")
        # The album artist folder, as Plex shows it; the song keeps its credit.
        assert outcome.new_path == (config.music_root / "Kyle Gordon"
                                    / "Kyle Gordon Is Everywhere (2026)"
                                    / "02 - We are the Zorg.opus")
        assert outcome.tags.artist == "Billy Mainframe"
        assert outcome.tags.album_artist == "Kyle Gordon"

    def test_an_empty_artist_is_left_out_of_the_query(self, tmp_path, monkeypatch):
        from urllib.parse import unquote

        from beetdrop.mb import MusicBrainzClient
        client = MusicBrainzClient(tmp_path / "mb.sqlite3")
        urls = []
        monkeypatch.setattr(client, "_cached_get",
                            lambda url: urls.append(unquote(url)) or {"recordings": []})
        client.search_recordings("We Are the Zorg", "", album="Kyle Gordon Is Everywhere")
        client.search_releases("Kyle Gordon Is Everywhere", "")
        client.close()
        assert 'artist:' not in urls[0] and 'artist:' not in urls[1]
        assert 'recording:"We Are the Zorg" AND release:"Kyle Gordon Is Everywhere"' in urls[0]

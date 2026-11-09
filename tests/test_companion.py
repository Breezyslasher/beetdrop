"""A song's music video, filed beside the track for Plex."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beetdrop.app import create_app
from beetdrop.companion import (
    carry_companion,
    grab_companion,
    pick_music_video,
    plex_video_filename,
    rename_to_track_names,
)
from beetdrop.config import Config
from beetdrop.fulltags import FullTags
from beetdrop.search import Result


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def make_mp4(path: Path):
    """A real one-second mp4 with an audio stream, which is what
    verify_audio insists on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(path),
         "-y", "-loglevel", "error"],
        check=True, timeout=60)


def video(raw, artist="Dolly Parton", seconds=200, vid="v1"):
    from beetdrop.cleaning import clean_title
    return Result(video_id=vid, title=clean_title(raw), raw_title=raw,
                  artists=[artist], duration_seconds=seconds)


SONG = dict(title="9 to 5", artist="Dolly Parton", duration=161)


class TestThePlexName:
    # A real Plex links a video to a track when it has the track's own
    # file name, in the same folder - not the "-video" form, which made
    # an artist-level extra instead.
    def test_it_is_the_audio_filename_with_the_videos_extension(self):
        assert plex_video_filename("01 - 9 to 5.opus") == "01 - 9 to 5.mp4"

    def test_a_disc_prefix_is_kept(self):
        assert plex_video_filename("2-03 - Jolene.flac") == "2-03 - Jolene.mp4"

    def test_an_unnumbered_track(self):
        assert plex_video_filename("Jolene.opus") == "Jolene.mp4"

    def test_no_suffix(self):
        assert "-video" not in plex_video_filename("01 - 9 to 5.opus")

    def test_audio_that_is_itself_mp4_cannot_share_its_name(self):
        assert plex_video_filename("01 - Song.mp4") == "01 - Song-video.mp4"


class TestPickingTheVideo:
    def test_the_official_video_is_chosen(self):
        pick = pick_music_video(
            [video("Dolly Parton - 9 To 5 (Official Video)", seconds=190)],
            **SONG)
        assert pick.result is not None

    def test_an_official_audio_upload_is_not_a_video(self):
        # A still image over the track. Beside the song in Plex it would
        # look like a video and be a picture.
        pick = pick_music_video(
            [video("Dolly Parton - 9 To 5 (Official Audio)", seconds=161)],
            **SONG)
        assert pick.result is None
        assert "not a music video" in pick.reason

    def test_a_lyric_video_is_not_the_music_video(self):
        pick = pick_music_video(
            [video("Dolly Parton - 9 to 5 (Lyric Video)", seconds=161)], **SONG)
        assert pick.result is None

    def test_a_live_performance_is_a_different_recording(self):
        pick = pick_music_video(
            [video("Dolly Parton - 9 to 5 (Live)", seconds=170)], **SONG)
        assert pick.result is None

    def test_a_different_artists_video_is_refused(self):
        # The cover by someone else is the commonest wrong answer, and
        # filed beside the track Plex would show it as Dolly's.
        pick = pick_music_video(
            [video("9 to 5 (Official Video)", artist="Someone Else", seconds=165)],
            **SONG)
        assert pick.result is None

    def test_a_video_with_a_long_intro_is_still_the_video(self):
        pick = pick_music_video(
            [video("Dolly Parton - 9 to 5 (Official Video)", seconds=161 + 90)],
            **SONG)
        assert pick.result is not None

    def test_a_much_shorter_video_is_a_different_edit(self):
        pick = pick_music_video(
            [video("Dolly Parton - 9 to 5 (Official Video)", seconds=120)], **SONG)
        assert pick.result is None

    def test_the_closer_length_wins_between_two_good_ones(self):
        far = video("Dolly Parton - 9 to 5 (Official Video)", seconds=290, vid="far")
        near = video("Dolly Parton - 9 to 5 (Official Video)", seconds=175, vid="near")
        assert pick_music_video([far, near], **SONG).result.video_id == "near"

    def test_the_bare_youtube_music_title_works_too(self):
        # YouTube Music's own video entries carry the plain title, with
        # the artist in its own field.
        pick = pick_music_video([video("9 to 5", seconds=180)], **SONG)
        assert pick.result is not None

    def test_a_title_that_only_contains_the_song_name_is_refused(self):
        # "9 to 5" is inside this, and it is a different song.
        pick = pick_music_video(
            [video("Dolly Parton - Working 9 to 5 Medley Tribute", seconds=170)],
            **SONG)
        assert pick.result is None

    def test_nothing_found_says_so(self):
        pick = pick_music_video([], **SONG)
        assert pick.result is None and "no music video" in pick.reason


class FakeConfig:
    def __init__(self, tmp_path):
        self.scratch_root = tmp_path / "scratch"
        self.video_max_height = 1080
        self.cookies_file = ""


def library_track(tmp_path):
    album = tmp_path / "music" / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
    album.mkdir(parents=True)
    audio = album / "01 - 9 to 5.opus"
    audio.write_bytes(b"x")
    return audio


RESULT = Result(video_id="song", title="9 to 5", raw_title="9 to 5",
                artists=["Dolly Parton"], duration_seconds=161)
TAGS = FullTags(title="9 to 5", artist="Dolly Parton", album_artist="Dolly Parton")


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestGrabbingIt:
    def downloader(self, calls):
        def download(video_id, scratch, **kwargs):
            calls.append(video_id)
            target = scratch / "out.mp4"
            make_mp4(target)
            return target
        return download

    def test_it_lands_beside_the_track_under_the_plex_name(self, tmp_path):
        audio = library_track(tmp_path)
        calls = []
        found = grab_companion(
            FakeConfig(tmp_path), audio, TAGS, RESULT,
            search=lambda q: [video("Dolly Parton - 9 to 5 (Official Video)",
                                    seconds=175)],
            download=self.downloader(calls))
        assert found.path == audio.parent / "01 - 9 to 5.mp4"
        assert found.path.is_file() and calls == ["v1"]
        # Nothing half-written or left in scratch.
        assert not list(audio.parent.glob(".*beetdrop-tmp"))
        assert not (tmp_path / "scratch").exists() or not any(
            (tmp_path / "scratch").iterdir())

    def test_a_track_that_already_has_one_is_left_alone(self, tmp_path):
        # A second copy would be "01 - 9 to 5 (2).mp4", which Plex links
        # to nothing; the first video stays and nothing is downloaded.
        audio = library_track(tmp_path)
        existing = audio.parent / "01 - 9 to 5.mp4"
        existing.write_bytes(b"theirs")
        calls = []
        found = grab_companion(FakeConfig(tmp_path), audio, TAGS, RESULT,
                               search=lambda q: pytest.fail("searched anyway"),
                               download=self.downloader(calls))
        assert calls == [] and existing.read_bytes() == b"theirs"
        assert "already" in found.detail

    @pytest.mark.parametrize("old_name", ["9 to 5-video.mp4", "01 - 9 to 5-video.mp4"])
    def test_one_an_earlier_version_filed_is_renamed(self, tmp_path, old_name):
        # 0.65-0.67 named it for the title, 0.68-0.72 with "-video".
        audio = library_track(tmp_path)
        (audio.parent / old_name).write_bytes(b"old")
        calls = []
        found = grab_companion(FakeConfig(tmp_path), audio, TAGS, RESULT,
                               search=lambda q: pytest.fail("searched anyway"),
                               download=self.downloader(calls))
        assert calls == []
        assert (audio.parent / "01 - 9 to 5.mp4").read_bytes() == b"old"
        assert not (audio.parent / old_name).exists()
        assert "renamed" in found.detail

    def test_no_match_downloads_nothing(self, tmp_path):
        audio = library_track(tmp_path)
        calls = []
        found = grab_companion(
            FakeConfig(tmp_path), audio, TAGS, RESULT,
            search=lambda q: [video("Dolly Parton - 9 to 5 (Official Audio)")],
            download=self.downloader(calls))
        assert found.path is None and calls == []

    def test_a_failed_search_never_fails_the_grab(self, tmp_path):
        def boom(query):
            raise RuntimeError("YouTube is down")
        found = grab_companion(FakeConfig(tmp_path), library_track(tmp_path),
                               TAGS, RESULT, search=boom,
                               download=self.downloader([]))
        assert found.path is None and "YouTube is down" in found.detail

    def test_a_failed_download_leaves_nothing_behind(self, tmp_path):
        audio = library_track(tmp_path)

        def broken(video_id, scratch, **kwargs):
            scratch.mkdir(parents=True, exist_ok=True)
            (scratch / "partial.mp4.part").write_bytes(b"half")
            raise RuntimeError("HTTP 403")

        found = grab_companion(
            FakeConfig(tmp_path), audio, TAGS, RESULT,
            search=lambda q: [video("Dolly Parton - 9 to 5 (Official Video)",
                                    seconds=175)],
            download=broken)
        assert found.path is None and "403" in found.detail
        assert not (audio.parent / "01 - 9 to 5.mp4").exists()
        scratch = tmp_path / "scratch"
        assert not scratch.exists() or not any(scratch.iterdir())


class TestCarryingItAlong:
    def test_a_refiled_track_takes_its_video_under_the_new_name(self, tmp_path):
        old_dir = tmp_path / "_review" / "Unknown - Some Song"
        new_dir = tmp_path / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
        old_dir.mkdir(parents=True)
        (old_dir / "Some Song.mp4").write_bytes(b"clip")
        moved = carry_companion(old_dir / "Some Song.opus", "Some Song",
                                new_dir / "01 - 9 to 5.opus")
        assert moved == new_dir / "01 - 9 to 5.mp4"
        assert moved.read_bytes() == b"clip"
        assert not (old_dir / "Some Song.mp4").exists()

    @pytest.mark.parametrize("old_name", ["Some Song-video.mp4", "track-video.mp4"])
    def test_one_under_an_earlier_name_is_found_too(self, tmp_path, old_name):
        (tmp_path / old_name).write_bytes(b"clip")
        moved = carry_companion(tmp_path / "track.opus", "Some Song",
                                tmp_path / "01 - 9 to 5.opus")
        assert moved == tmp_path / "01 - 9 to 5.mp4"

    def test_a_video_already_at_the_destination_is_not_overwritten(self, tmp_path):
        (tmp_path / "a.mp4").write_bytes(b"old")
        (tmp_path / "b.mp4").write_bytes(b"chosen")
        moved = carry_companion(tmp_path / "a.opus", "A", tmp_path / "b.opus")
        assert moved is None
        assert (tmp_path / "b.mp4").read_bytes() == b"chosen"

    def test_no_video_is_nothing_to_do(self, tmp_path):
        assert carry_companion(tmp_path / "a.opus", "A", tmp_path / "b.opus") is None


class TestRenamingEarlierOnes:
    def album(self, tmp_path, *names):
        folder = tmp_path / "Artist" / "Album (1999)"
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).write_bytes(b"x")
        return folder

    def test_a_suffix_named_video_gets_its_tracks_own_name(self, tmp_path):
        folder = self.album(tmp_path, "01 - Intro.opus", "02 - 9 to 5.opus",
                            "02 - 9 to 5-video.mp4")
        assert rename_to_track_names(tmp_path) == 1
        assert (folder / "02 - 9 to 5.mp4").is_file()
        assert not (folder / "02 - 9 to 5-video.mp4").exists()

    def test_a_title_named_video_too(self, tmp_path):
        folder = self.album(tmp_path, "01 - Intro.opus", "02 - 9 to 5.opus",
                            "9 to 5-video.mp4")
        assert rename_to_track_names(tmp_path) == 1
        assert (folder / "02 - 9 to 5.mp4").is_file()

    def test_a_disc_numbered_track_too(self, tmp_path):
        folder = self.album(tmp_path, "2-03 - Jolene.flac", "Jolene-video.mp4")
        assert rename_to_track_names(tmp_path) == 1
        assert (folder / "2-03 - Jolene.mp4").is_file()

    def test_a_video_already_under_the_new_name_is_kept(self, tmp_path):
        folder = self.album(tmp_path, "01 - Song.opus", "01 - Song.mp4",
                            "01 - Song-video.mp4")
        assert rename_to_track_names(tmp_path) == 0
        assert (folder / "01 - Song-video.mp4").is_file()

    def test_two_tracks_with_that_title_is_not_guessed(self, tmp_path):
        folder = self.album(tmp_path, "01 - Song.opus", "2-01 - Song.opus",
                            "Song-video.mp4")
        assert rename_to_track_names(tmp_path) == 0
        assert (folder / "Song-video.mp4").is_file()

    def test_no_track_to_own_it_is_left_alone(self, tmp_path):
        folder = self.album(tmp_path, "01 - Other.opus", "Song-video.mp4")
        assert rename_to_track_names(tmp_path) == 0
        assert (folder / "Song-video.mp4").is_file()


class TestTheApi:
    def make_config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_a_song_grab_can_ask_for_its_video(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module

        seen = {}
        monkeypatch.setattr(
            app_module.JobManager, "enqueue",
            lambda self, vid, fmt="", bitrate="", kind="track", with_video=False:
            seen.update(kind=kind, with_video=with_video)
            or {"id": "j1", "video_id": vid, "kind": kind})
        with TestClient(create_app(self.make_config(tmp_path))) as client:
            response = client.post("/api/grab", json={
                "video_id": "abc", "kind": "track", "with_video": True})
        assert response.status_code == 202
        assert seen == {"kind": "track", "with_video": True}

    def test_an_album_cannot(self, tmp_path):
        # A search per track, and most album tracks have no video: the
        # closest match would be wrong more often than right.
        with TestClient(create_app(self.make_config(tmp_path))) as client:
            response = client.post("/api/grab", json={
                "video_id": "MPREb_x", "kind": "album", "with_video": True})
        assert response.status_code == 422

    def test_the_job_remembers_it(self, tmp_path):
        from beetdrop.db import Store
        store = Store(tmp_path / "jobs.sqlite3")
        job = store.create_job("abc", "opus", "", kind="track", with_video=True)
        assert job["with_video"] == 1
        plain = store.create_job("def", "opus", "", kind="track")
        assert plain["with_video"] == 0


class TestStatsCountsThem:
    def test_videos_beside_tracks_are_counted_apart(self, tmp_path):
        from beetdrop.libraryview import count_companion_videos

        music = tmp_path / "music"
        album = music / "Dolly Parton" / "9 to 5 (1980)"
        album.mkdir(parents=True)
        (album / "01 - 9 to 5.opus").write_bytes(b"x")
        (album / "01 - 9 to 5.mp4").write_bytes(b"x")          # named like its track
        (album / "Hush-a-Bye-video.mp4").write_bytes(b"x")     # not renamed yet
        (album / "Making Of.mp4").write_bytes(b"x")            # no track by that name
        # The Kodi-style library sits inside the music root by default;
        # nothing in it is a companion, whatever it is called.
        kodi = music / "Music Videos" / "Dolly Parton"
        kodi.mkdir(parents=True)
        (kodi / "Dolly Parton - 9 to 5-video.mp4").write_bytes(b"x")
        assert count_companion_videos(music, music / "Music Videos") == 2


class TestTheJobRunsIt:
    """End to end through the queue: the grab's outcome decides whether a
    video is fetched, and nothing about the video can fail the grab."""

    def client(self, tmp_path, monkeypatch, verified=True, companion=None):
        import time

        import beetdrop.jobs as jobs_module
        from beetdrop.grab import GrabOutcome

        config = self.config = Config(
            music_root=tmp_path / "music", scratch_root=tmp_path / "s",
            config_dir=tmp_path / "c")
        config.music_root.mkdir()
        audio = library_track(tmp_path)

        def fake_run_grab(video_id, cfg, fmt="", bitrate="", on_stage=None,
                          on_progress=None, on_resolved=None, logger=None):
            on_resolved(RESULT)
            return GrabOutcome(inbox_path=audio, result=RESULT,
                               verified=verified, tags=TAGS)

        self.asked = []

        def fake_companion(cfg, audio_path, tags, result, **kwargs):
            self.asked.append(audio_path)
            if isinstance(companion, Exception):
                raise companion
            from beetdrop.companion import Companion
            return companion or Companion(audio_path.parent / "01 - 9 to 5.mp4",
                                          "music video filed beside it")

        monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab)
        monkeypatch.setattr(jobs_module, "grab_companion", fake_companion)
        self.time = time
        return TestClient(create_app(config))

    def finished(self, client, job_id):
        deadline = self.time.time() + 5
        while self.time.time() < deadline:
            job = next(j for j in client.get("/api/jobs").json()["jobs"]
                       if j["id"] == job_id)
            if job["stage"] in ("done", "failed"):
                return job
            self.time.sleep(0.02)
        raise AssertionError("job never finished")

    def test_the_video_outcome_is_on_the_card(self, tmp_path, monkeypatch):
        with self.client(tmp_path, monkeypatch) as client:
            job = client.post("/api/grab", json={
                "video_id": "abc", "with_video": True}).json()
            done = self.finished(client, job["id"])
        assert done["stage"] == "done"
        assert "music video filed beside it" in done["detail"]

    def test_a_grab_without_it_never_looks(self, tmp_path, monkeypatch):
        with self.client(tmp_path, monkeypatch) as client:
            job = client.post("/api/grab", json={"video_id": "abc"}).json()
            self.finished(client, job["id"])
        assert self.asked == []

    def test_a_track_sent_to_review_gets_its_video_too(self, tmp_path, monkeypatch):
        # The video is named for the audio file, and fixing the match later
        # carries it along, so an unverified track need not go without.
        # (It did once, when the video was named for the title.)
        with self.client(tmp_path, monkeypatch, verified=False) as client:
            job = client.post("/api/grab", json={
                "video_id": "abc", "with_video": True}).json()
            done = self.finished(client, job["id"])
        assert len(self.asked) == 1 and done["stage"] == "done"
        assert "music video filed beside it" in done["detail"]


class TestRenamingOnStartup:
    def test_it_runs_once_in_the_background(self, tmp_path, monkeypatch):
        import time as _time

        import beetdrop.companion as companion_module
        from beetdrop.db import Store

        music = tmp_path / "music"
        folder = music / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
        folder.mkdir(parents=True)
        (folder / "01 - 9 to 5.opus").write_bytes(b"x")
        (folder / "01 - 9 to 5-video.mp4").write_bytes(b"clip")
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)):
            for _ in range(100):
                if Store(config.db_path).get_settings().get("companion_names_v3"):
                    break
                _time.sleep(0.02)
        assert (folder / "01 - 9 to 5.mp4").read_bytes() == b"clip"

        walks = []
        monkeypatch.setattr(companion_module, "rename_to_track_names",
                            lambda root: walks.append(root) or 0)
        with TestClient(create_app(config)):
            _time.sleep(0.1)
        assert walks == []


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# Builds a real mp4, as TestGrabbingIt does; CI has no ffmpeg.
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestArtwork:
    def grab(self, tmp_path, fetch_art):
        audio = library_track(tmp_path)

        def download(video_id, scratch, **kwargs):
            target = scratch / "out.mp4"
            make_mp4(target)
            return target

        return grab_companion(
            FakeConfig(tmp_path), audio, TAGS, RESULT,
            search=lambda q: [video("Dolly Parton - 9 to 5 (Official Video)",
                                    seconds=175)],
            download=download, fetch_art=fetch_art)

    def test_the_videos_own_thumbnail_is_embedded(self, tmp_path):
        from mutagen.mp4 import MP4
        asked = []
        found = self.grab(tmp_path, lambda vid, url: asked.append(vid) or (JPEG, "image/jpeg"))
        covers = MP4(str(found.path)).get("covr")
        assert asked == ["v1"]
        assert covers and bytes(covers[0]) == JPEG
        # The track's tags are still there beside it.
        assert MP4(str(found.path))["\xa9nam"] == ["9 to 5"]

    def test_no_artwork_still_files_the_video_and_says_so(self, tmp_path):
        found = self.grab(tmp_path, lambda vid, url: None)
        assert found.path.is_file() and "without artwork" in found.detail

    def test_a_failing_fetch_still_files_the_video(self, tmp_path):
        def boom(vid, url):
            raise RuntimeError("no network")
        found = self.grab(tmp_path, boom)
        assert found.path.is_file()


class TestFetchingTheThumbnail:
    class Response:
        def __init__(self, status, content=b""):
            self.status_code, self.content = status, content

    def serve(self, monkeypatch, pages):
        import beetdrop.companion as companion_module  # noqa: F401
        import requests
        asked = []

        def get(url, **kwargs):
            asked.append(url)
            return pages.get(url, self.Response(404))

        monkeypatch.setattr(requests, "get", get)
        return asked

    MAXRES = "https://i.ytimg.com/vi/abc/maxresdefault.jpg"
    HQ720 = "https://i.ytimg.com/vi/abc/hq720.jpg"
    DEFAULT = "https://i.ytimg.com/vi/abc/hqdefault.jpg?sqp=1"

    def test_the_full_size_frame_comes_first(self, monkeypatch):
        from beetdrop.companion import fetch_thumbnail
        asked = self.serve(monkeypatch, {self.MAXRES: self.Response(200, JPEG),
                                         self.DEFAULT: self.Response(200, PNG)})
        assert fetch_thumbnail("abc", self.DEFAULT) == (JPEG, "image/jpeg")
        assert asked == [self.MAXRES]

    def test_a_missing_one_falls_through_to_the_next(self, monkeypatch):
        # A missing maxresdefault is a 404 that still carries an image.
        from beetdrop.companion import fetch_thumbnail
        asked = self.serve(monkeypatch, {self.MAXRES: self.Response(404, JPEG),
                                         self.DEFAULT: self.Response(200, PNG)})
        assert fetch_thumbnail("abc", self.DEFAULT) == (PNG, "image/png")
        assert asked == [self.MAXRES, self.HQ720, self.DEFAULT]

    def test_an_image_mp4_cannot_hold_is_skipped(self, monkeypatch):
        from beetdrop.companion import fetch_thumbnail
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8 "
        self.serve(monkeypatch, {self.MAXRES: self.Response(200, webp),
                                 self.HQ720: self.Response(200, b"<html>")})
        assert fetch_thumbnail("abc", "") is None

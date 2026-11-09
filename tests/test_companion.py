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
    def test_it_is_the_title_with_the_video_suffix(self):
        assert plex_video_filename("9 to 5") == "9 to 5-video.mp4"

    def test_the_track_number_is_not_part_of_it(self):
        # Plex matches the title it shows, and "03 - " is only how
        # Beetdrop names the audio file.
        assert not plex_video_filename("Jolene").startswith("0")

    def test_a_title_that_is_not_a_safe_filename_is_made_one(self):
        name = plex_video_filename("AC/DC: Back?")
        assert "/" not in name and name.endswith("-video.mp4")


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
        assert found.path == audio.parent / "9 to 5-video.mp4"
        assert found.path.is_file() and calls == ["v1"]
        # Nothing half-written or left in scratch.
        assert not list(audio.parent.glob(".*beetdrop-tmp"))
        assert not (tmp_path / "scratch").exists() or not any(
            (tmp_path / "scratch").iterdir())

    def test_a_track_that_already_has_one_is_left_alone(self, tmp_path):
        # A second copy would be "9 to 5-video (2).mp4", which Plex reads
        # as nothing; the first video stays and nothing is downloaded.
        audio = library_track(tmp_path)
        existing = audio.parent / "9 to 5-video.mp4"
        existing.write_bytes(b"theirs")
        calls = []
        found = grab_companion(FakeConfig(tmp_path), audio, TAGS, RESULT,
                               search=lambda q: pytest.fail("searched anyway"),
                               download=self.downloader(calls))
        assert calls == [] and existing.read_bytes() == b"theirs"
        assert "already" in found.detail

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
        assert not (audio.parent / "9 to 5-video.mp4").exists()
        scratch = tmp_path / "scratch"
        assert not scratch.exists() or not any(scratch.iterdir())


class TestCarryingItAlong:
    def test_a_refiled_track_takes_its_video_under_the_new_title(self, tmp_path):
        old_dir = tmp_path / "_review" / "Unknown - Some Song"
        new_dir = tmp_path / "Dolly Parton" / "9 to 5 and Odd Jobs (1980)"
        old_dir.mkdir(parents=True)
        (old_dir / "Some Song-video.mp4").write_bytes(b"clip")
        moved = carry_companion(old_dir / "track.opus", "Some Song",
                                new_dir / "01 - 9 to 5.opus", "9 to 5")
        assert moved == new_dir / "9 to 5-video.mp4"
        assert moved.read_bytes() == b"clip"
        assert not (old_dir / "Some Song-video.mp4").exists()

    def test_a_video_already_at_the_destination_is_not_overwritten(self, tmp_path):
        (tmp_path / "Some Song-video.mp4").write_bytes(b"old")
        (tmp_path / "9 to 5-video.mp4").write_bytes(b"chosen")
        moved = carry_companion(tmp_path / "a.opus", "Some Song",
                                tmp_path / "b.opus", "9 to 5")
        assert moved is None
        assert (tmp_path / "9 to 5-video.mp4").read_bytes() == b"chosen"

    def test_no_video_is_nothing_to_do(self, tmp_path):
        assert carry_companion(tmp_path / "a.opus", "A",
                               tmp_path / "b.opus", "B") is None


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
        (album / "9 to 5-video.mp4").write_bytes(b"x")
        # The Kodi-style library sits inside the music root by default;
        # nothing in it is a companion, whatever it is called.
        kodi = music / "Music Videos" / "Dolly Parton"
        kodi.mkdir(parents=True)
        (kodi / "Dolly Parton - 9 to 5-video.mp4").write_bytes(b"x")
        assert count_companion_videos(music, music / "Music Videos") == 1


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
            return companion or Companion(audio_path.parent / "9 to 5-video.mp4",
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

    def test_a_track_sent_to_review_gets_no_video(self, tmp_path, monkeypatch):
        # Its title is YouTube's guess and changes when the match is
        # fixed; a video named for it now would be named for nothing.
        with self.client(tmp_path, monkeypatch, verified=False) as client:
            job = client.post("/api/grab", json={
                "video_id": "abc", "with_video": True}).json()
            done = self.finished(client, job["id"])
        assert self.asked == [] and done["stage"] == "done"
        assert "_review" in done["detail"]

"""Filing new grabs in a folder inside the library, and offering a song's
music video when the song is already there."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beetdrop.app import create_app
from beetdrop.config import Config, clean_subfolder
from beetdrop.fulltags import FullTags
from beetdrop.search import Result


def config_for(tmp_path, sub=""):
    music = tmp_path / "music"
    music.mkdir(parents=True, exist_ok=True)
    return Config(music_root=music, scratch_root=tmp_path / "s",
                  config_dir=tmp_path / "c", grab_subfolder=sub)


class TestTheFolder:
    def test_only_a_folder_inside_the_library(self):
        assert clean_subfolder("explo") == "explo"
        assert clean_subfolder("/explo/") == "explo"
        assert clean_subfolder("a/b") == "a/b"
        assert clean_subfolder("../elsewhere") == ""
        assert clean_subfolder("x/../../y") == ""
        assert clean_subfolder("") == ""

    def test_grabs_are_filed_in_it(self, tmp_path):
        config = config_for(tmp_path, "explo")
        assert config.filing_root == config.music_root / "explo"
        assert config_for(tmp_path).filing_root == config.music_root

    def test_a_song_grab_lands_there(self, tmp_path, monkeypatch):
        import beetdrop.grab as grab_module
        from beetdrop.library import TrackResolution

        config = config_for(tmp_path, "explo")

        def fake_download(video_id, scratch_dir, fmt="opus", **kwargs):
            scratch_dir.mkdir(parents=True, exist_ok=True)
            path = scratch_dir / ("%s.%s" % (video_id, fmt))
            path.write_bytes(b"x")
            return path

        class FakeMB:
            def fetch_cover(self, **kwargs):
                return None

        monkeypatch.setattr(grab_module, "download_audio", fake_download)
        monkeypatch.setattr(grab_module, "verify_audio", lambda p: None)
        monkeypatch.setattr(grab_module, "write_full_tags",
                            lambda p, t, c=None, m="image/jpeg": None)
        monkeypatch.setattr(grab_module, "get_mb_client", lambda c: FakeMB())
        monkeypatch.setattr(grab_module, "resolve_track", lambda mb, t, a, d: TrackResolution(
            tags=FullTags(title="Stan", artist="Eminem", album_artist="Eminem",
                          album="The Marshall Mathers LP", date="2000",
                          track_number=3, recording_mbid="rec"),
            matched=True))
        monkeypatch.setattr(grab_module, "lookup_video", lambda v: Result(
            video_id=v, title="Stan", raw_title="Stan", artists=["Eminem"],
            duration_seconds=404))
        outcome = grab_module.run_grab("vid", config)
        assert outcome.inbox_path == (config.music_root / "explo" / "Eminem"
                                      / "The Marshall Mathers LP (2000)" / "03 - Stan.opus")

    def test_settings_take_it_and_refuse_a_way_out(self, tmp_path):
        config = config_for(tmp_path)
        with TestClient(create_app(config)) as client:
            body = client.put("/api/settings", json={"grab_subfolder": "/explo/"}).json()
            assert body["grab_subfolder"] == "explo"
            assert client.put("/api/settings",
                              json={"grab_subfolder": "../elsewhere"}).status_code == 422
            assert client.put("/api/settings", json={"grab_subfolder": ""}).json()[
                "grab_subfolder"] == ""


class TestFixingAMatchKeepsItsLibrary:
    def test_where_a_rematched_track_is_filed(self, tmp_path):
        from beetdrop.rematch import filing_root_for
        config = config_for(tmp_path, "explo")
        music = config.music_root
        # In another library beside it: stays there.
        assert filing_root_for(config, music / "Nick" / "Eminem" / "x" / "a.opus") == music / "Nick"
        # In the folder for grabs, or its _review: there.
        assert filing_root_for(config, music / "explo" / "_review" / "A - B" / "a.opus") == \
            music / "explo"
        # An old _review at the top: where grabs go now.
        assert filing_root_for(config, music / "_review" / "A - B" / "a.opus") == music / "explo"
        # No folder set: the library, as before.
        plain = config_for(tmp_path / "p")
        assert filing_root_for(plain, plain.music_root / "Nick" / "E" / "x" / "a.opus") == \
            plain.music_root

    def test_both_review_folders_are_listed(self, tmp_path):
        from beetdrop.rematch import iter_unverified, review_roots
        config = config_for(tmp_path, "explo")
        for folder in (config.music_root / "_review" / "Old - One",
                       config.music_root / "explo" / "_review" / "New - One"):
            folder.mkdir(parents=True)
            (folder / "track.opus").write_bytes(b"x")
        found = [p.parent.name for root in review_roots(config) for p in iter_unverified(root)]
        assert sorted(found) == ["New - One", "Old - One"]

    def test_stats_count_review_inside_the_folder_as_unverified(self, tmp_path):
        from beetdrop.libraryview import scan_albums
        config = config_for(tmp_path, "explo")
        folder = config.music_root / "explo" / "_review" / "Artist - Song"
        folder.mkdir(parents=True)
        (folder / "track.opus").write_bytes(b"x")
        albums = scan_albums(config.music_root)
        assert [a.verified for a in albums] == [False]


class TestTheMusicVideoForASongAlreadyThere:
    def setup(self, tmp_path, monkeypatch):
        import beetdrop.jobs as jobs_module
        from beetdrop.companion import Companion

        config = config_for(tmp_path)
        song = config.music_root / "Eminem" / "Curtain Call (2005)" / "05 - Stan.opus"
        song.parent.mkdir(parents=True)
        song.write_bytes(b"audio")
        result = Result(video_id="stanvideo01", title="Stan", raw_title="Stan",
                        artists=["Eminem"], duration_seconds=404)

        def fake_run_grab(video_id, cfg, **kwargs):
            from beetdrop.grab import GrabOutcome
            kwargs["on_resolved"](result)
            return GrabOutcome(inbox_path=song, result=result, tags=FullTags(
                title="Stan", artist="Eminem", album_artist="Eminem"))

        self.companions = []

        def fake_companion(cfg, audio, tags, res, **kwargs):
            self.companions.append((audio, tags.title, res.video_id))
            video = audio.with_suffix(".mp4")
            video.write_bytes(b"video")
            return Companion(video, "music video filed beside it")

        monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab)
        monkeypatch.setattr(jobs_module, "grab_companion", fake_companion)
        monkeypatch.setattr("beetdrop.search.lookup_video", lambda v: result)
        return config, song

    def finished(self, client, job_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            job = next(j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id)
            if job["stage"] in ("done", "failed"):
                return job
            time.sleep(0.02)
        raise AssertionError("job never finished")

    def test_grabbing_it_again_says_it_is_there_without_a_video(self, tmp_path, monkeypatch):
        config, song = self.setup(tmp_path, monkeypatch)
        with TestClient(create_app(config)) as client:
            first = client.post("/api/grab", json={"video_id": "stanvideo01"}).json()
            self.finished(client, first["id"])
            again = client.post("/api/grab", json={"video_id": "stanvideo01"})
            assert again.status_code == 409
            detail = again.json()["detail"]
            assert detail["in_library"] is True and detail["has_video"] is False

    def test_just_the_video_is_fetched_beside_it(self, tmp_path, monkeypatch):
        config, song = self.setup(tmp_path, monkeypatch)
        with TestClient(create_app(config)) as client:
            first = client.post("/api/grab", json={"video_id": "stanvideo01"}).json()
            self.finished(client, first["id"])
            queued = client.post("/api/jobs/%s/video" % first["id"])
            assert queued.status_code == 202
            job = queued.json()
            assert job["kind"] == "companion" and job["title"] == "Stan"
            done = self.finished(client, job["id"])
            assert done["stage"] == "done", done
            assert "music video filed beside it" in done["detail"]
            assert self.companions == [(song, "Stan", "stanvideo01")]
            # Now it has one: asking again is refused, and the grab says so.
            assert client.post("/api/jobs/%s/video" % first["id"]).status_code == 409
            again = client.post("/api/grab", json={"video_id": "stanvideo01"}).json()
            assert again["detail"]["has_video"] is True
        assert song.read_bytes() == b"audio"        # the song itself untouched

    def test_a_song_no_longer_there_is_said(self, tmp_path, monkeypatch):
        config, song = self.setup(tmp_path, monkeypatch)
        with TestClient(create_app(config)) as client:
            first = client.post("/api/grab", json={"video_id": "stanvideo01"}).json()
            self.finished(client, first["id"])
            song.unlink()
            assert client.post("/api/jobs/%s/video" % first["id"]).status_code == 409
            again = client.post("/api/grab", json={"video_id": "stanvideo01"}).json()
            assert "in_library" not in again["detail"]

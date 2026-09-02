"""Music-video support: Kodi filing/NFO/poster, the videos search, the
run_video_grab pipeline, and the API surface (search type, grab kind,
settings). The network boundary is faked; filing runs for real."""

import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

import beetdrop.grab as grab_module
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.fulltags import FullTags
from beetdrop.grab import run_video_grab
from beetdrop.library import TrackResolution
from beetdrop.search import Result
from beetdrop.videos import (
    video_basename,
    video_dir,
    video_filename,
    write_nfo,
    write_poster,
)


# -- pure filing helpers -------------------------------------------------


class TestFilingHelpers:
    def test_basename_and_paths(self, tmp_path):
        tags = FullTags(title="GO", artist="AmaLee", album="MY NINJA WAY")
        assert video_basename(tags) == "AmaLee - GO"
        assert video_filename(tags) == "AmaLee - GO.mp4"
        assert video_dir(tmp_path, tags) == tmp_path / "AmaLee"

    def test_basename_sanitises_and_falls_back(self, tmp_path):
        tags = FullTags(title="A/B: C", artist="")
        # No slashes survive into one path segment; blank artist has a fallback.
        assert "/" not in video_basename(tags)
        assert video_basename(tags).startswith("Unknown Artist - ")

    def test_nfo_has_kodi_musicvideo_shape(self, tmp_path):
        video = tmp_path / "AmaLee - GO.mp4"
        video.write_bytes(b"x")
        tags = FullTags(title="GO", artist="AmaLee", album="MY NINJA WAY",
                        date="2019-05-01", track_number=2)
        nfo = write_nfo(video, tags, duration_seconds=215)
        assert nfo == tmp_path / "AmaLee - GO.nfo"
        root = ET.fromstring(nfo.read_text())
        assert root.tag == "musicvideo"
        assert root.findtext("title") == "GO"
        assert root.findtext("artist") == "AmaLee"
        assert root.findtext("album") == "MY NINJA WAY"
        assert root.findtext("year") == "2019"
        assert root.findtext("premiered") == "2019-05-01"
        assert root.findtext("runtime") == "4"  # round(215/60)
        assert root.findtext("track") == "2"
        assert root.findtext("userrating") == "0"
        thumb = root.find("thumb")
        assert thumb.get("aspect") == "poster"
        assert thumb.text == "AmaLee - GO-poster.jpg"

    def test_nfo_omits_unknown_album_and_year(self, tmp_path):
        video = tmp_path / "X - Y.mp4"
        video.write_bytes(b"x")
        nfo = write_nfo(video, FullTags(title="Y", artist="X"))
        root = ET.fromstring(nfo.read_text())
        assert root.find("album") is None
        assert root.find("year") is None
        assert root.findtext("track") == "0"

    def test_nfo_and_poster_do_not_overwrite(self, tmp_path):
        video = tmp_path / "X - Y.mp4"
        video.write_bytes(b"x")
        nfo = write_nfo(video, FullTags(title="Y", artist="X"))
        nfo.write_text("<musicvideo><title>hand-edited</title></musicvideo>")
        write_nfo(video, FullTags(title="Y", artist="X"))  # must not clobber
        assert "hand-edited" in nfo.read_text()

        poster = write_poster(video, b"first")
        assert poster.name == "X - Y-poster.jpg"
        write_poster(video, b"second")
        assert poster.read_bytes() == b"first"

    def test_poster_extension_follows_mime(self, tmp_path):
        video = tmp_path / "X - Y.mp4"
        video.write_bytes(b"x")
        png = write_poster(video, b"png", mime="image/png")
        assert png.name == "X - Y-poster.png"


# -- search --------------------------------------------------------------


class TestSearchVideos:
    def test_search_videos_normalises_rows(self, monkeypatch):
        import beetdrop.search as search_module

        class FakeYT:
            def search(self, query, filter=None, limit=8):
                assert filter == "videos"
                return [{"videoId": "vid1", "title": "Naruto - GO",
                         "artists": [{"name": "AmaLee"}],
                         "duration": "3:35", "thumbnails": [{"url": "http://x/t.jpg"}]}]

        monkeypatch.setattr(search_module, "YTMusic", FakeYT)
        results = search_module.search_videos("go")
        assert len(results) == 1
        assert results[0].video_id == "vid1"
        assert results[0].artists == ["AmaLee"]
        assert results[0].duration_seconds == 215


# -- pipeline ------------------------------------------------------------


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def make_video(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
         str(path), "-y", "-loglevel", "error"],
        check=True, timeout=60)


class FakeMBCover:
    def __init__(self, cover):
        self._cover = cover

    def fetch_cover(self, release_mbid="", release_group_mbid="", thumbnail_url=""):
        return self._cover


@pytest.fixture
def config(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    return Config(music_root=music, video_root=tmp_path / "videos",
                  scratch_root=tmp_path / "scratch", config_dir=tmp_path / "config")


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestVideoGrabPipeline:
    def _install(self, monkeypatch, resolution, cover):
        def fake_download(video_id, scratch_dir, max_height=1080, **kwargs):
            path = Path(scratch_dir) / ("%s.mp4" % video_id)
            make_video(path)
            return path

        monkeypatch.setattr(grab_module, "download_video", fake_download)
        monkeypatch.setattr(grab_module, "get_mb_client",
                            lambda c: FakeMBCover(cover))
        monkeypatch.setattr(grab_module, "resolve_track",
                            lambda mb, title, artist, dur: resolution)
        monkeypatch.setattr(grab_module, "lookup_video",
                            lambda v: Result(video_id=v, title="GO",
                                             raw_title="Naruto - GO (Official)",
                                             artists=["AmaLee"], duration_seconds=215,
                                             thumbnail_url="http://x/t.jpg"))

    def test_matched_video_files_with_nfo_and_poster(self, config, monkeypatch):
        matched = TrackResolution(
            tags=FullTags(title="GO", artist="AmaLee", album_artist="AmaLee",
                          album="MY NINJA WAY", date="2019-05-01",
                          recording_mbid="rec-9"),
            matched=True)
        self._install(monkeypatch, matched, cover=(b"\xff\xd8poster", "image/jpeg", "caa"))

        outcome = run_video_grab("vid1", config)
        assert outcome.verified
        video = config.video_root / "AmaLee" / "AmaLee - GO.mp4"
        assert video.is_file()
        assert (video.with_suffix(".nfo")).is_file()
        poster = video.with_name("AmaLee - GO-poster.jpg")
        assert poster.read_bytes() == b"\xff\xd8poster"
        # NFO carries the matched album, not the YouTube title.
        root = ET.fromstring(video.with_suffix(".nfo").read_text())
        assert root.findtext("album") == "MY NINJA WAY"
        # Basic metadata embedded into the mp4 container too.
        import mutagen
        tags = mutagen.File(str(video))
        assert tags is not None

    def test_unmatched_video_still_files(self, config, monkeypatch):
        unmatched = TrackResolution(
            tags=FullTags(title="GO", artist="AmaLee", album="GO", unverified=True),
            matched=False)
        self._install(monkeypatch, unmatched, cover=None)

        outcome = run_video_grab("vid2", config)
        assert not outcome.verified  # no MB match...
        video = config.video_root / "AmaLee" / "AmaLee - GO.mp4"
        assert video.is_file()  # ...but the video is filed anyway
        # Unmatched: no invented album in the NFO.
        root = ET.fromstring(video.with_suffix(".nfo").read_text())
        assert root.find("album") is None


# -- API -----------------------------------------------------------------


class TestVideoApi:
    def make_config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, video_root=tmp_path / "videos",
                      scratch_root=tmp_path / "s", config_dir=tmp_path / "c")

    def test_search_type_videos_dispatches(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)
        monkeypatch.setattr(app_module, "search_videos",
                            lambda q, limit: [Result(video_id="v", title="T",
                                                     raw_title="T", artists=["A"])])
        with TestClient(create_app(config)) as client:
            body = client.get("/api/search?q=x&type=videos").json()
        assert body["type"] == "videos"
        assert body["results"][0]["video_id"] == "v"

    def test_bad_search_type_rejected(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/search?q=x&type=clips").status_code == 422

    def test_grab_accepts_musicvideo_kind(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)
        captured = {}
        monkeypatch.setattr(app_module.JobManager, "enqueue",
                            lambda self, vid, fmt="", bitrate="", kind="track":
                            captured.update(vid=vid, kind=kind) or
                            {"id": "j1", "video_id": vid, "kind": kind})
        with TestClient(create_app(config)) as client:
            resp = client.post("/api/grab", json={"video_id": "vid1", "kind": "musicvideo"})
        assert resp.status_code == 202
        assert captured == {"vid": "vid1", "kind": "musicvideo"}

    def test_grab_rejects_unknown_kind(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.post("/api/grab",
                               json={"video_id": "v", "kind": "clip"}).status_code == 422

    def test_settings_expose_video_fields(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            s = client.get("/api/settings").json()
            assert s["video_root"].endswith("videos")
            assert s["video_root_locked"] is False
            assert s["video_max_height"] == 1080

    def test_video_max_height_persists_and_validates(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.put("/api/settings",
                              json={"video_max_height": 9000}).status_code == 422
            body = client.put("/api/settings", json={"video_max_height": 720}).json()
            assert body["video_max_height"] == 720
        from beetdrop.db import Store
        assert Store(config.db_path).get_settings()["video_max_height"] == "720"

    def test_video_root_persists(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            body = client.put("/api/settings",
                              json={"video_root": "/data/mv"}).json()
            assert body["video_root"] == "/data/mv"

    def test_video_root_locked_rejects_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VIDEO_PATH", "/locked/mv")
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            s = client.get("/api/settings").json()
            assert s["video_root_locked"] is True
            assert client.put("/api/settings",
                              json={"video_root": "/other"}).status_code == 422

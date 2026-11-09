"""Sending music videos' embedded artwork to Plex as their thumbnails."""

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import beetdrop.plex as plex
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.plex import PlexError
from beetdrop.plexart import local_path, send_artwork
from beetdrop.videoart import embed_cover

pytestmark = pytest.mark.usefixtures("loopback_only")

TOKEN = "owner-token"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def make_mp4(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(path),
         "-y", "-loglevel", "error"],
        check=True, timeout=60)


class FakePlex:
    """The calls this needs, as the Plex Media Server API 1.2.2 has them:

      GET  /library/sections/all                 libraries and their folders
      GET  /library/sections/{id}/all?type=8     the artists in one
      GET  /library/metadata/{id}/extras         an artist's extras
      POST /library/metadata/{id}/thumb          set an item's thumbnail,
                                                 the image as the body
    """

    def __init__(self, artists, refuse_uploads=False):
        # artists: {artist_key: [(extra_key, title, plex_file), ...]}
        self.artists = artists
        self.uploads = []
        self.refuse_uploads = refuse_uploads
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def _ok(self):
                if self.headers.get("X-Plex-Token") != TOKEN:
                    self._reply(401)
                    return False
                return True

            def do_GET(self):
                if not self._ok():
                    return
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/library/sections/all":
                    return self._reply(200, {"MediaContainer": {"Directory": [
                        {"key": "9", "title": "Nicks-Music", "type": "artist",
                         "Location": [{"path": "/srv/disk/Music/explo"}]},
                        {"key": "1", "title": "Movies", "type": "movie",
                         "Location": [{"path": "/srv/disk/Movies"}]}]}})
                if url.path == "/library/sections/9/all" and query.get("type") == ["8"]:
                    return self._reply(200, {"MediaContainer": {"Metadata": [
                        {"ratingKey": key, "type": "artist"} for key in fake.artists]}})
                parts = url.path.split("/")
                if url.path.startswith("/library/metadata/") and parts[-1] == "extras":
                    extras = fake.artists.get(parts[3], [])
                    return self._reply(200, {"MediaContainer": {"Metadata": [
                        {"ratingKey": key, "title": title, "type": "clip",
                         "extraType": 4, "subtype": "musicVideo",
                         "Media": [{"Part": [{"file": path}]}]}
                        for key, title, path in extras]}})
                self._reply(404)

            def do_POST(self):
                if not self._ok():
                    return
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                parts = url.path.split("/")
                if url.path.startswith("/library/metadata/") and parts[-1] == "thumb":
                    if fake.refuse_uploads:
                        return self._reply(400)
                    fake.uploads.append((parts[3], body, self.headers.get("Content-Type")))
                    return self._reply(200)
                self._reply(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Cfg:
    def __init__(self, tmp_path, url):
        self.music_root = tmp_path / "music"
        self.music_root.mkdir(exist_ok=True)
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir(exist_ok=True)
        self.plex_url, self.plex_token = url, TOKEN
        self.plex_music_path = "/srv/disk/Music/explo"
        self.plex_section = ""


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch, tmp_path_factory):
    plex._cache.clear()
    monkeypatch.setattr(plex, "MOUNTINFO", tmp_path_factory.mktemp("proc") / "none")
    yield
    plex._cache.clear()


def test_a_plex_file_maps_back_here(tmp_path):
    root = tmp_path / "music"
    assert local_path("/srv/disk/Music/explo/A/B/v.mp4", "/srv/disk/Music/explo",
                      root) == root / "A" / "B" / "v.mp4"
    assert local_path("/srv/disk/Music/other/v.mp4", "/srv/disk/Music/explo", root) is None
    assert local_path("/srv/disk/Music/explorer/v.mp4", "/srv/disk/Music/explo", root) is None


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestSending:
    def library(self, tmp_path):
        music = tmp_path / "music"
        with_art = music / "Weezer" / "Videos" / "Buddy Holly-video.mp4"
        make_mp4(with_art)
        embed_cover(with_art, JPEG, "image/jpeg")
        png_art = music / "Weezer" / "Videos" / "Say It Ain't So-video.mp4"
        make_mp4(png_art)
        embed_cover(png_art, PNG, "image/png")
        bare = music / "Ed Sheeran" / "One.mp4"
        make_mp4(bare)
        base = "/srv/disk/Music/explo"
        return {
            "100": [("500", "Buddy Holly", base + "/Weezer/Videos/Buddy Holly-video.mp4"),
                    ("501", "Say It Ain't So", base + "/Weezer/Videos/Say It Ain't So-video.mp4")],
            "101": [("502", "One", base + "/Ed Sheeran/One.mp4"),
                    ("503", "Gone", base + "/Ed Sheeran/Gone.mp4"),          # not on disk
                    ("504", "Trailer", "/srv/disk/Movies/trailer.mkv")],     # not a music video file
            # The same extra listed under a second artist is sent once.
            "102": [("500", "Buddy Holly", base + "/Weezer/Videos/Buddy Holly-video.mp4")],
        }

    def test_each_videos_art_is_uploaded_once(self, tmp_path):
        fake = FakePlex(self.library(tmp_path))
        try:
            config = Cfg(tmp_path, fake.url)
            result = send_artwork(config)
            assert sorted((key, body, kind) for key, body, kind in fake.uploads) == [
                ("500", JPEG, "image/jpeg"), ("501", PNG, "image/png")]
            assert (result.artists, result.videos, result.sent, result.no_art,
                    result.not_here) == (3, 4, 2, 1, 1)

            # A second run sends nothing: Plex keeps every image uploaded.
            fake.uploads.clear()
            again = send_artwork(config)
            assert fake.uploads == [] and again.unchanged == 2
        finally:
            fake.close()

    def test_a_changed_picture_is_sent_again(self, tmp_path):
        artists = self.library(tmp_path)
        fake = FakePlex(artists)
        try:
            config = Cfg(tmp_path, fake.url)
            send_artwork(config)
            embed_cover(tmp_path / "music" / "Weezer" / "Videos" / "Buddy Holly-video.mp4",
                        PNG, "image/png")
            fake.uploads.clear()
            send_artwork(config)
            assert [(key, body) for key, body, _ in fake.uploads] == [("500", PNG)]
        finally:
            fake.close()

    def test_a_plex_that_refuses_is_given_up_on(self, tmp_path):
        base = "/srv/disk/Music/explo"
        music = tmp_path / "music"
        artists = {"100": []}
        for n in range(8):
            video = music / ("v%d.mp4" % n)
            make_mp4(video)
            embed_cover(video, JPEG, "image/jpeg")
            artists["100"].append((str(600 + n), "v%d" % n, "%s/v%d.mp4" % (base, n)))
        fake = FakePlex(artists, refuse_uploads=True)
        try:
            result = send_artwork(Cfg(tmp_path, fake.url))
            assert result.failed == 5 and "may not take artwork" in result.stopped
            assert "HTTP 400" in result.stopped
        finally:
            fake.close()


def test_without_plex_it_says_so(tmp_path):
    config = Cfg(tmp_path, "")
    with pytest.raises(PlexError, match="sign in to Plex"):
        send_artwork(config)


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_it_runs_from_the_queue(tmp_path):
    music = tmp_path / "music"
    video = music / "Weezer" / "Buddy Holly-video.mp4"
    make_mp4(video)
    embed_cover(video, JPEG, "image/jpeg")
    fake = FakePlex({"100": [("500", "Buddy Holly",
                              "/srv/disk/Music/explo/Weezer/Buddy Holly-video.mp4")]})
    config = Config(music_root=music, scratch_root=tmp_path / "s", config_dir=tmp_path / "c")
    try:
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={
                "plex_url": fake.url, "plex_token": TOKEN,
                "plex_music_path": "/srv/disk/Music/explo"})
            job = client.post("/api/plex/send-artwork").json()
            deadline = time.time() + 10
            while time.time() < deadline:
                done = next(j for j in client.get("/api/jobs").json()["jobs"]
                            if j["id"] == job["id"])
                if done["stage"] in ("done", "failed"):
                    break
                time.sleep(0.05)
    finally:
        fake.close()
    assert done["stage"] == "done", done
    assert "sent artwork to Plex for 1 of 1" in done["detail"]
    assert [key for key, _, _ in fake.uploads] == ["500"]

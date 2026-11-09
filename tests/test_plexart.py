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
      GET  /library/sections/{id}/all?type=10    its tracks, each with the
                                                 primaryExtraKey of a music
                                                 video linked to it
      GET  /library/metadata/{id}/extras         an artist's extras
      POST /library/metadata/{id}/posters        upload an image, as the body
                                                 (python-plexapi's uploadPoster)
      PUT  /library/metadata/{id}/refresh        refresh an item

    and POST .../thumb answers 404, as a real server did.
    """

    def __init__(self, artists, refuse_uploads=False, refuse_refresh=False,
                 tracks=(), items=(), keeps_pictures=False):
        # artists: {artist_key: [(extra_key, title, plex_file), ...]}
        # tracks: [(track_key, title, audio_plex_file, extra_key or None)]
        self.artists = artists
        self.tracks = list(tracks)
        self.items = dict(items)      # {extra_key: plex_file} for GET /library/metadata/{id}
        # keeps_pictures: a list of pictures per item, GET .../posters,
        # where an upload is added but - like the real server, sometimes -
        # not chosen, and PUT .../poster?url= chooses one.
        self.keeps_pictures = keeps_pictures
        self.pictures = {}            # {item: [{"ratingKey", "selected"}]}
        self.selections = []
        self.uploads = []
        self.refreshes = []
        self.refuse_uploads = refuse_uploads
        self.refuse_refresh = refuse_refresh
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
                if url.path == "/library/sections/9/all" and query.get("type") == ["10"]:
                    return self._reply(200, {"MediaContainer": {"Metadata": [
                        dict({"ratingKey": key, "title": title, "type": "track",
                              "Media": [{"Part": [{"file": audio}]}]},
                             **({"primaryExtraKey": "/library/metadata/%s" % extra}
                                if extra else {}))
                        for key, title, audio, extra in fake.tracks]}})
                if url.path == "/library/sections/9/all" and query.get("type") == ["8"]:
                    return self._reply(200, {"MediaContainer": {"Metadata": [
                        {"ratingKey": key, "type": "artist"} for key in fake.artists]}})
                parts = url.path.split("/")
                if (url.path.startswith("/library/metadata/") and len(parts) == 4
                        and parts[3] in fake.items):
                    return self._reply(200, {"MediaContainer": {"Metadata": [
                        {"ratingKey": parts[3], "type": "clip",
                         "Media": [{"Part": [{"file": fake.items[parts[3]]}]}]}]}})
                if (fake.keeps_pictures and url.path.startswith("/library/metadata/")
                        and parts[-1] == "posters"):
                    return self._reply(200, {"MediaContainer": {"Metadata": [
                        dict(p) for p in fake.pictures.setdefault(
                            parts[3], [{"ratingKey": "metadata://frame", "selected": True}])]}})
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
                if url.path.startswith("/library/metadata/") and parts[-1] == "posters":
                    if fake.refuse_uploads:
                        return self._reply(404)
                    fake.uploads.append((parts[3], body, self.headers.get("Content-Type")))
                    if fake.keeps_pictures:
                        listed = fake.pictures.setdefault(
                            parts[3], [{"ratingKey": "metadata://frame", "selected": True}])
                        listed.append({"ratingKey": "upload://posters/%d" % len(fake.uploads),
                                       "selected": False})
                    return self._reply(200)
                self._reply(404)

            def do_PUT(self):
                if not self._ok():
                    return
                url = urlparse(self.path)
                parts = url.path.split("/")
                if url.path.startswith("/library/metadata/") and parts[-1] == "poster":
                    chosen = parse_qs(url.query).get("url", [""])[0]
                    listed = fake.pictures.get(parts[3], [])
                    if not any(p["ratingKey"] == chosen for p in listed):
                        return self._reply(404)
                    for p in listed:
                        p["selected"] = p["ratingKey"] == chosen
                    fake.selections.append((parts[3], chosen))
                    return self._reply(200)
                if self.path.startswith("/library/metadata/") and parts[-1] == "refresh":
                    if fake.refuse_refresh:
                        return self._reply(404)
                    fake.refreshes.append(parts[3])
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
                    result.not_here, result.not_mp4) == (3, 5, 2, 1, 1, 1)
            # The one not found is named the way Plex names it.
            assert result.missing == ["/srv/disk/Music/explo/Ed Sheeran/Gone.mp4"]

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

    def refusing_library(self, tmp_path, n):
        base = "/srv/disk/Music/explo"
        music = tmp_path / "music"
        artists = {"100": []}
        for i in range(n):
            video = music / ("v%d.mp4" % i)
            make_mp4(video)
            embed_cover(video, JPEG, "image/jpeg")
            artists["100"].append((str(600 + i), "v%d" % i, "%s/v%d.mp4" % (base, i)))
        return artists

    def test_a_refused_upload_falls_back_to_a_refresh(self, tmp_path):
        fake = FakePlex(self.refusing_library(tmp_path, 3), refuse_uploads=True)
        try:
            result = send_artwork(Cfg(tmp_path, fake.url))
            assert (result.sent, result.refreshed, result.failed) == (0, 3, 0)
            assert sorted(fake.refreshes) == ["600", "601", "602"]
            assert "upload refused: Plex answered HTTP 404" in result.reasons
        finally:
            fake.close()

    def test_refusing_both_is_given_up_on(self, tmp_path):
        fake = FakePlex(self.refusing_library(tmp_path, 8),
                        refuse_uploads=True, refuse_refresh=True)
        try:
            result = send_artwork(Cfg(tmp_path, fake.url))
            assert result.failed == 5 and "refused both" in result.stopped
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


def test_a_file_is_found_by_the_end_of_its_path(tmp_path):
    from beetdrop.plexart import by_tail
    root = tmp_path / "music"
    (root / "Weezer").mkdir(parents=True)
    (root / "Weezer" / "Buddy Holly-video.mp4").write_bytes(b"x")
    (root / "Intro.mp4").write_bytes(b"x")
    found = by_tail("/srv/disk/Music/explo/Weezer/Buddy Holly-video.mp4", root)
    assert found == (root / "Weezer" / "Buddy Holly-video.mp4", "/srv/disk/Music/explo")
    # A file name alone could be anyone's.
    assert by_tail("/srv/disk/Other/Intro.mp4", root) is None
    assert by_tail("/srv/disk/Music/explo/Ed Sheeran/One.mp4", root) is None


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_without_a_folder_mapping_it_learns_one(tmp_path):
    music = tmp_path / "music"
    first = music / "Weezer" / "Buddy Holly-video.mp4"
    second = music / "Ed Sheeran" / "One-video.mp4"
    for video in (first, second):
        make_mp4(video)
        embed_cover(video, JPEG, "image/jpeg")
    base = "/srv/disk/Music/explo"
    fake = FakePlex({
        "100": [("500", "Buddy Holly", base + "/Weezer/Buddy Holly-video.mp4")],
        "101": [("501", "One", base + "/Ed Sheeran/One-video.mp4")]})
    try:
        config = Cfg(tmp_path, fake.url)
        config.plex_music_path = ""      # not set, and nothing to guess it from
        notes = []
        result = send_artwork(config, on_detail=notes.append)
        assert result.sent == 2 and result.not_here == 0
        assert "Plex sees the music folder at /srv/disk/Music/explo" in notes
    finally:
        fake.close()



@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_videos_linked_to_tracks_are_found_through_the_tracks(tmp_path):
    # Named like the song, beside it: Plex links it to the track, and it
    # is not among the artist's extras at all.
    music = tmp_path / "music"
    album = music / "Ed Sheeran" / "x (2014)"
    album.mkdir(parents=True)
    (album / "01 - One.mp3").write_bytes(b"audio")
    make_mp4(album / "01 - One.mp4")
    embed_cover(album / "01 - One.mp4", JPEG, "image/jpeg")
    (album / "02 - I'm a Mess.mp3").write_bytes(b"audio")
    base = "/srv/disk/Music/explo/Ed Sheeran/x (2014)"
    fake = FakePlex({"100": []}, tracks=[
        ("900", "One", base + "/01 - One.mp3", "777"),
        ("901", "I'm a Mess", base + "/02 - I'm a Mess.mp3", None)])
    try:
        result = send_artwork(Cfg(tmp_path, fake.url))
        assert [(key, body) for key, body, _ in fake.uploads] == [("777", JPEG)]
        assert (result.videos, result.sent) == (1, 1)
    finally:
        fake.close()



@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_a_linked_video_named_unlike_its_song_is_asked_of_plex(tmp_path):
    # Plex linked "One (Official Video).mp4" to "01 - One.mp3": not the
    # song's name, so the file is looked up rather than guessed.
    music = tmp_path / "music"
    album = music / "Nick" / "Ed Sheeran" / "x (2014)"
    album.mkdir(parents=True)
    (album / "01 - One.mp3").write_bytes(b"audio")
    make_mp4(album / "One (Official Video).mp4")
    embed_cover(album / "One (Official Video).mp4", JPEG, "image/jpeg")
    (album / "02 - Photograph.mp3").write_bytes(b"audio")
    base = "/srv/disk/Music/Nick/Ed Sheeran/x (2014)"
    fake = FakePlex({"100": []}, tracks=[
        ("900", "One", base + "/01 - One.mp3", "777"),
        ("901", "Photograph", base + "/02 - Photograph.mp3", "778")],
        items={"777": base + "/One (Official Video).mp4",
               "778": base + "/Photograph.mkv"})
    try:
        config = Cfg(tmp_path, fake.url)
        config.plex_music_path = "/srv/disk/Music"       # one folder up
        result = send_artwork(config)
        assert [key for key, _, _ in fake.uploads] == ["777"]
        assert (result.sent, result.not_mp4, result.not_here) == (1, 1, 0)
    finally:
        fake.close()



@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestThePictureShowing:
    """Uploading adds a picture to an item's choices; Plex does not always
    show it, and makes the item anew when its file changes."""

    def setup(self, tmp_path, **kw):
        music = tmp_path / "music"
        video = music / "Weezer" / "Buddy Holly.mp4"
        make_mp4(video)
        embed_cover(video, JPEG, "image/jpeg")
        fake = FakePlex({"100": [("500", "Buddy Holly",
                                  "/srv/disk/Music/explo/Weezer/Buddy Holly.mp4")]},
                        keeps_pictures=True, **kw)
        return fake, Cfg(tmp_path, fake.url)

    def test_an_upload_plex_did_not_choose_is_chosen(self, tmp_path):
        fake, config = self.setup(tmp_path)
        try:
            result = send_artwork(config)
            assert result.sent == 1
            assert fake.selections == [("500", "upload://posters/1")]
            assert [p for p in fake.pictures["500"] if p["selected"]] == [
                {"ratingKey": "upload://posters/1", "selected": True}]
        finally:
            fake.close()

    def test_sent_before_but_not_showing_is_chosen_again(self, tmp_path):
        fake, config = self.setup(tmp_path)
        try:
            send_artwork(config)
            # Plex went back to a frame of the video.
            for p in fake.pictures["500"]:
                p["selected"] = p["ratingKey"] == "metadata://frame"
            fake.uploads.clear()
            result = send_artwork(config)
            assert (result.reselected, result.sent) == (1, 0) and fake.uploads == []
            assert fake.selections[-1] == ("500", "upload://posters/1")
        finally:
            fake.close()

    def test_sent_before_but_gone_from_plex_is_sent_again(self, tmp_path):
        fake, config = self.setup(tmp_path)
        try:
            send_artwork(config)
            # The file changed and Plex made the item anew: our picture is gone.
            fake.pictures["500"] = [{"ratingKey": "metadata://frame", "selected": True}]
            result = send_artwork(config)
            assert (result.sent, result.resent) == (1, 1)
        finally:
            fake.close()

    def test_showing_is_left_alone(self, tmp_path):
        fake, config = self.setup(tmp_path)
        try:
            send_artwork(config)
            fake.uploads.clear()
            fake.selections.clear()
            result = send_artwork(config)
            assert result.unchanged == 1 and fake.uploads == [] and fake.selections == []
        finally:
            fake.close()


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestNotSendingEverythingAgain:
    """A real run sent all 732 again: pictures remembered by an older
    version (the image's hash alone) were judged gone because Plex's list
    did not look as expected. Without plain evidence, nothing is resent."""

    def setup(self, tmp_path):
        import hashlib
        import json as _json
        from beetdrop.plexart import STATE_FILE
        music = tmp_path / "music"
        video = music / "Weezer" / "Buddy Holly.mp4"
        make_mp4(video)
        embed_cover(video, JPEG, "image/jpeg")
        fake = FakePlex({"100": [("500", "Buddy Holly",
                                  "/srv/disk/Music/explo/Weezer/Buddy Holly.mp4")]},
                        keeps_pictures=True)
        config = Cfg(tmp_path, fake.url)
        (config.config_dir / STATE_FILE).write_text(_json.dumps(
            {fake.url: {"500": hashlib.sha1(JPEG).hexdigest()}}))
        return fake, config

    def test_an_older_record_is_trusted_whatever_plex_lists(self, tmp_path):
        fake, config = self.setup(tmp_path)
        try:
            # However this server names or marks its pictures.
            fake.pictures["500"] = [{"ratingKey": "something://else", "selected": True}]
            result = send_artwork(config)
            assert result.unchanged == 1 and result.resent == 0 and fake.uploads == []
        finally:
            fake.close()

    def test_an_empty_list_is_trusted_too(self, tmp_path):
        fake, config = self.setup(tmp_path)
        try:
            fake.pictures["500"] = []
            result = send_artwork(config)
            assert result.unchanged == 1 and fake.uploads == []
        finally:
            fake.close()

    def test_what_plex_answered_is_kept_for_the_log(self, tmp_path):
        fake, config = self.setup(tmp_path)
        try:
            result = send_artwork(config)
            assert result.sample.startswith("GET /library/metadata/500/posters answered:")
            assert "metadata://frame" in result.sample
        finally:
            fake.close()

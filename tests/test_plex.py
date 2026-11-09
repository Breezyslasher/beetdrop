"""Telling Plex to scan what a grab just filed."""

import json
import socket
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

TOKEN = "good-token"

# Every test here talks to a FakePlex on 127.0.0.1; nothing else is let out.
pytestmark = pytest.mark.usefixtures("loopback_only")


class FakePlex:
    """Enough of a Plex Media Server to be scanned, held to the published
    API (OpenAPI, Plex Media Server 1.2.2) rather than to what Beetdrop
    happens to send:

      GET  /library/sections/all             the library list
      POST /library/sections/{id}/refresh    a scan, ?path= restricting it

    anything else gets the status a real server gives a wrong method or
    path. `legacy=True` is a server older than that API, which answers
    only the bare /library/sections. `signed_out=True` refuses refreshes
    with the 503 the API documents for a server signed out of plex.tv.
    """

    def __init__(self, sections, legacy=False, signed_out=False):
        self.sections = sections
        self.refreshes = []
        self.requests = []
        fake = self
        listing = "/library/sections" if legacy else "/library/sections/all"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=b""):
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self, url):
                fake.requests.append((self.command, url.path))
                if self.headers.get("X-Plex-Token") != TOKEN:
                    self._reply(401)
                    return False
                return True

            def do_GET(self):
                url = urlparse(self.path)
                if not self._authorised(url):
                    return
                if url.path == listing:
                    self._reply(200, json.dumps({"MediaContainer": {"Directory": [
                        {"key": key, "title": title, "type": kind,
                         "Location": [{"id": i, "path": path}
                                      for i, path in enumerate(paths)]}
                        for key, title, kind, paths in fake.sections]}}).encode())
                elif url.path.endswith("/refresh"):
                    self._reply(405)   # a refresh is a POST
                else:
                    self._reply(404)

            def do_POST(self):
                url = urlparse(self.path)
                if not self._authorised(url):
                    return
                parts = url.path.split("/")
                if (len(parts) == 5 and parts[1:3] == ["library", "sections"]
                        and parts[4] == "refresh"):
                    if signed_out:
                        self._reply(503)
                        return
                    fake.refreshes.append(
                        (parts[3], parse_qs(url.query).get("path", [""])[0]))
                    self._reply(200)
                else:
                    self._reply(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch, tmp_path_factory):
    plex._cache.clear()
    # This machine's own mounts must not suggest anything; tests that
    # want a mount write their own.
    monkeypatch.setattr(plex, "MOUNTINFO", tmp_path_factory.mktemp("proc") / "none")
    yield
    plex._cache.clear()


@pytest.fixture
def server():
    fake = FakePlex([("3", "Music", "artist", ["/data/music"]),
                     ("7", "Movies", "movie", ["/data/movies"])])
    yield fake
    fake.close()


class Cfg:
    def __init__(self, tmp_path, url="", token="", mapped=""):
        self.music_root = tmp_path / "music"
        self.music_root.mkdir(exist_ok=True)
        self.plex_url, self.plex_token, self.plex_music_path = url, token, mapped


class TestWhosePath:
    def test_a_folder_under_the_library_is_rerooted(self, tmp_path):
        root = tmp_path / "music"
        (root / "Artist" / "Album").mkdir(parents=True)
        assert plex.plex_path(root / "Artist" / "Album", root, "/data/music") == \
            "/data/music/Artist/Album"

    def test_the_library_itself_maps_to_its_plex_path(self, tmp_path):
        root = tmp_path / "music"
        root.mkdir()
        assert plex.plex_path(root, root, "/data/music/") == "/data/music"

    def test_no_mapping_means_the_same_path(self, tmp_path):
        assert plex.plex_path(tmp_path / "x", tmp_path, "") == (tmp_path / "x").as_posix()

    def test_a_folder_outside_the_library_is_passed_through(self, tmp_path):
        outside = tmp_path / "videos"
        assert plex.plex_path(outside, tmp_path / "music", "/data/music") == \
            outside.as_posix()


class TestWhichLibrary:
    SECTIONS = [{"key": "1", "title": "All", "type": "artist", "locations": ["/data"]},
                {"key": "3", "title": "Music", "type": "artist", "locations": ["/data/music"]}]

    def test_a_library_inside_another_is_in_both(self):
        # Plex indexes the folder in each, so each has to scan it.
        keys = [s["key"] for s in plex.sections_for(self.SECTIONS, "/data/music/A/B")]
        assert keys == ["1", "3"]

    def test_a_shared_prefix_is_not_containment(self):
        # /data/music2 is not inside /data/music, however it is spelled.
        only = [self.SECTIONS[1]]
        assert plex.sections_for(only, "/data/music2/A") == []

    def test_nothing_contains_it(self):
        assert plex.sections_for(self.SECTIONS, "/elsewhere") == []


DISK = "/srv/dev-disk-by-uuid-0000AAAA"
# Shaped like a real NAS: one "Both" folder in two people's music
# libraries, and video libraries beside them.
NAS = [
    {"key": "1", "title": "Movies", "type": "movie", "locations": [DISK + "/Media/VIDEO/movies"]},
    {"key": "8", "title": "CD Music", "type": "artist",
     "locations": [DISK + "/Media/Music/Both", DISK + "/Media/Music/ALL"]},
    {"key": "9", "title": "Nick's Music", "type": "artist",
     "locations": [DISK + "/Media/Music/explo", DISK + "/Media/Music/Nick",
                   DISK + "/Media/Music/Both"]},
]


def mountinfo(source, point="/music"):
    return "\n".join([
        "600 500 0:52 / / rw,relatime - overlay overlay rw",
        "610 600 8:17 %s %s rw,relatime - ext4 /dev/sdb1 rw" % (source, point),
        "611 600 0:5 / /dev rw,nosuid - tmpfs tmpfs rw",
    ])


class TestFindingTheMapping:
    def test_the_mounted_folder_is_read_from_mountinfo(self):
        assert plex.source_parts(Path("/music"), mountinfo("/Media/Music/Nick")) == \
            ["Media", "Music", "Nick"]

    def test_a_folder_below_the_mount_keeps_its_rest(self):
        info = mountinfo("/Media/Music", point="/data")
        assert plex.source_parts(Path("/data/Nick"), info) == ["Media", "Music", "Nick"]

    def test_a_space_in_the_folder_name_is_unescaped(self):
        assert plex.source_parts(Path("/music"), mountinfo("/Media/My\\040Music")) == \
            ["Media", "My Music"]

    def test_not_a_bind_mount_says_nothing(self):
        assert plex.source_parts(Path("/music"), mountinfo("/x", point="/other")) == []

    def test_the_plex_folder_is_picked_out_by_its_tail(self):
        tail = ["Media", "Music", "Nick"]
        assert plex.suggest_mapping(NAS, tail) == DISK + "/Media/Music/Nick"

    def test_a_folder_inside_a_library_folder_is_found_too(self):
        tail = ["Media", "Music", "Nick", "Beetdrop"]
        assert plex.suggest_mapping(NAS, tail) == DISK + "/Media/Music/Nick/Beetdrop"

    def test_one_matching_name_is_not_enough(self):
        # Every music library ends in something; "Nick" alone could be
        # anyone's folder.
        assert plex.suggest_mapping(NAS, ["Nick"]) == ""
        assert plex.suggest_mapping(NAS, ["srv", "other", "Nick"]) == ""


class TestScanning:
    def test_unconfigured_says_nothing_and_asks_nothing(self, tmp_path, server):
        assert plex.scan_folder(Cfg(tmp_path), tmp_path / "music") == ""
        assert server.requests == []

    def test_it_scans_just_the_folder_in_the_right_library(self, tmp_path, server):
        config = Cfg(tmp_path, server.url, TOKEN, "/data/music")
        album = config.music_root / "Dolly Parton" / "9 to 5 (1980)"
        album.mkdir(parents=True)
        line = plex.scan_folder(config, album)
        assert server.refreshes == [("3", "/data/music/Dolly Parton/9 to 5 (1980)")]
        assert "Music" in line

    def test_a_folder_in_two_libraries_is_scanned_into_both(self, tmp_path):
        both = FakePlex([(s["key"], s["title"], s["type"], s["locations"]) for s in NAS])
        try:
            config = Cfg(tmp_path, both.url, TOKEN, DISK + "/Media/Music/Both")
            line = plex.scan_folder(config, config.music_root / "Artist")
            target = DISK + "/Media/Music/Both/Artist"
            assert sorted(both.refreshes) == [("8", target), ("9", target)]
            assert '"CD Music" and "Nick\'s Music"' in line
        finally:
            both.close()

    def test_the_library_list_is_not_fetched_every_time(self, tmp_path, server):
        config = Cfg(tmp_path, server.url, TOKEN, "/data/music")
        for _ in range(3):
            plex.scan_folder(config, config.music_root)
        assert server.requests.count(("GET", "/library/sections/all")) == 1
        assert len(server.refreshes) == 3

    def test_a_folder_plex_does_not_cover_is_reported_not_scanned(self, tmp_path, server):
        config = Cfg(tmp_path, server.url, TOKEN, "/somewhere/else")
        line = plex.scan_folder(config, config.music_root)
        assert server.refreshes == []
        assert "no library containing /somewhere/else" in line

    def test_a_bad_token_is_said_plainly(self, tmp_path, server):
        config = Cfg(tmp_path, server.url, "wrong", "/data/music")
        assert "refused the token" in plex.scan_folder(config, config.music_root)

    def test_the_scan_is_a_post_as_the_api_documents(self, tmp_path, server):
        # A refresh changes server state; the published API has it as a
        # POST. This fake answers a GET refresh with 405, as a server
        # following the API may, so a GET would show up here as no scan.
        config = Cfg(tmp_path, server.url, TOKEN, "/data/music")
        plex.scan_folder(config, config.music_root)
        assert ("POST", "/library/sections/3/refresh") in server.requests
        assert ("GET", "/library/sections/3/refresh") not in server.requests

    def test_an_older_server_without_the_documented_listing_still_works(self, tmp_path):
        old = FakePlex([("3", "Music", "artist", ["/data/music"])], legacy=True)
        try:
            config = Cfg(tmp_path, old.url, TOKEN, "/data/music")
            assert "Music" in plex.scan_folder(config, config.music_root)
            assert old.refreshes == [("3", "/data/music")]
        finally:
            old.close()

    def test_a_signed_out_server_says_why(self, tmp_path):
        out = FakePlex([("3", "Music", "artist", ["/data/music"])], signed_out=True)
        try:
            config = Cfg(tmp_path, out.url, TOKEN, "/data/music")
            assert "signed out of plex.tv" in plex.scan_folder(config, config.music_root)
        finally:
            out.close()

    def test_an_unreachable_plex_never_raises(self, tmp_path):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed = "http://127.0.0.1:%d" % sock.getsockname()[1]
        config = Cfg(tmp_path, closed, TOKEN, "/data/music")
        started = time.monotonic()
        line = plex.scan_folder(config, config.music_root)
        assert "could not reach Plex" in line
        assert time.monotonic() - started < 5


class TestSettings:
    def make(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_the_token_is_kept_but_never_sent_back(self, tmp_path):
        with TestClient(create_app(self.make(tmp_path))) as client:
            body = client.put("/api/settings", json={
                "plex_url": "http://10.0.0.5:32400/", "plex_token": "secret-tok",
                "plex_music_path": "/data/music"}).json()
        assert body["plex_token_set"] is True
        assert "secret-tok" not in json.dumps(body)
        assert body["plex_url"] == "http://10.0.0.5:32400"
        assert body["plex_music_path"] == "/data/music"

    def test_an_address_without_a_scheme_is_refused(self, tmp_path):
        with TestClient(create_app(self.make(tmp_path))) as client:
            response = client.put("/api/settings", json={"plex_url": "10.0.0.5:32400"})
        assert response.status_code == 422

    def test_the_test_button_names_the_library(self, tmp_path, server):
        config = self.make(tmp_path)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={
                "plex_url": server.url, "plex_token": TOKEN,
                "plex_music_path": "/data/music"})
            result = client.post("/api/plex/test").json()
        assert result["ok"] is True and result["section"] == "Music"
        # Every library is listed with its key, to pick from.
        assert {"key": "7", "title": "Movies", "type": "movie",
                "locations": ["/data/movies"]} in result["libraries"]

    def test_with_nothing_holding_the_folder_it_asks_for_a_library(self, tmp_path, server):
        config = self.make(tmp_path)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={
                "plex_url": server.url, "plex_token": TOKEN,
                "plex_music_path": "/not/there"})
            result = client.post("/api/plex/test").json()
        assert result["ok"] is False and "Pick the library to scan" in result["detail"]

    def test_the_mounted_folder_is_used_without_typing_it(self, tmp_path, monkeypatch):
        nas = FakePlex([(s["key"], s["title"], s["type"], s["locations"]) for s in NAS])
        config = self.make(tmp_path)
        info = tmp_path / "mountinfo"
        info.write_text(mountinfo("/Media/Music/Nick", point=str(config.music_root)))
        monkeypatch.setattr(plex, "MOUNTINFO", info)
        try:
            with TestClient(create_app(config)) as client:
                client.put("/api/settings", json={"plex_url": nas.url, "plex_token": TOKEN})
                result = client.post("/api/plex/test").json()
        finally:
            nas.close()
        assert result["ok"] is True and result["sections"] == ["Nick's Music"]
        assert result["mapped"] == DISK + "/Media/Music/Nick"
        assert result["mapped_how"] == "mount"
        assert result["mapped"] in result["detail"]

    def test_a_chosen_library_with_no_known_folder_is_scanned_whole(self, tmp_path, server):
        config = self.make(tmp_path)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={
                "plex_url": server.url, "plex_token": TOKEN, "plex_section": "3"})
            result = client.post("/api/plex/test").json()
            assert client.get("/api/settings").json()["plex_section"] == "3"
        assert result["ok"] is True and result["sections"] == ["Music"]
        assert "rescans all of" in result["detail"]

    def test_a_chosen_library_that_is_gone_says_so(self, tmp_path, server):
        config = self.make(tmp_path)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={
                "plex_url": server.url, "plex_token": TOKEN, "plex_section": "99"})
            result = client.post("/api/plex/test").json()
        assert result["ok"] is False and "is gone" in result["detail"]

    def test_signing_out_forgets_the_library(self, tmp_path, server):
        # A library key means something on one server only.
        config = self.make(tmp_path)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={
                "plex_url": server.url, "plex_token": TOKEN, "plex_section": "3"})
            client.post("/api/plex/sign-out")
            assert client.get("/api/settings").json()["plex_section"] == ""


class TestChoosingTheLibrary:
    def nas(self):
        return FakePlex([(s["key"], s["title"], s["type"], s["locations"]) for s in NAS])

    def test_only_the_chosen_library_is_scanned(self, tmp_path):
        fake = self.nas()
        try:
            config = Cfg(tmp_path, fake.url, TOKEN, DISK + "/Media/Music/Both")
            config.plex_section = "9"
            plex.scan_folder(config, config.music_root / "Artist")
            assert fake.refreshes == [("9", DISK + "/Media/Music/Both/Artist")]
        finally:
            fake.close()

    def test_without_a_known_folder_the_whole_library_is_scanned(self, tmp_path):
        fake = self.nas()
        try:
            config = Cfg(tmp_path, fake.url, TOKEN, "")
            config.plex_section = "9"
            line = plex.scan_folder(config, config.music_root / "Artist")
            # No path: Plex rescans the library, skipping what it has.
            assert fake.refreshes == [("9", "")]
            assert "rescanning all of \"Nick's Music\"" in line
        finally:
            fake.close()

    def test_a_folder_outside_the_chosen_library_rescans_it_whole(self, tmp_path):
        # A wrong path would scan nothing; the whole library is never wrong.
        fake = self.nas()
        try:
            config = Cfg(tmp_path, fake.url, TOKEN, DISK + "/Media/Music/ALL")
            config.plex_section = "9"
            plex.scan_folder(config, config.music_root)
            assert fake.refreshes == [("9", "")]
        finally:
            fake.close()

    def test_on_automatic_the_mounted_folder_is_used(self, tmp_path, monkeypatch):
        fake = self.nas()
        config = Cfg(tmp_path, fake.url, TOKEN, "")
        info = tmp_path / "mountinfo"
        info.write_text(mountinfo("/Media/Music/explo", point=str(config.music_root)))
        monkeypatch.setattr(plex, "MOUNTINFO", info)
        try:
            plex.scan_folder(config, config.music_root / "Artist" / "Album")
            assert fake.refreshes == [("9", DISK + "/Media/Music/explo/Artist/Album")]
        finally:
            fake.close()

    def test_a_typed_folder_beats_the_mounted_one(self, tmp_path, monkeypatch):
        fake = self.nas()
        config = Cfg(tmp_path, fake.url, TOKEN, DISK + "/Media/Music/Nick")
        info = tmp_path / "mountinfo"
        info.write_text(mountinfo("/Media/Music/explo", point=str(config.music_root)))
        monkeypatch.setattr(plex, "MOUNTINFO", info)
        try:
            plex.scan_folder(config, config.music_root)
            assert fake.refreshes == [("9", DISK + "/Media/Music/Nick")]
        finally:
            fake.close()


class TestAfterAGrab:
    def client(self, tmp_path, monkeypatch, url):
        import beetdrop.jobs as jobs_module
        from beetdrop.grab import GrabOutcome
        from beetdrop.search import Result

        config = Config(music_root=tmp_path / "music", scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        album = config.music_root / "Dolly Parton" / "9 to 5 (1980)"
        album.mkdir(parents=True)
        audio = album / "01 - 9 to 5.opus"
        audio.write_bytes(b"x")
        result = Result(video_id="v", title="9 to 5", raw_title="9 to 5",
                        artists=["Dolly Parton"], duration_seconds=161)

        def fake_run_grab(video_id, cfg, **kwargs):
            kwargs["on_resolved"](result)
            return GrabOutcome(inbox_path=audio, result=result)

        monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab)
        client = TestClient(create_app(config))
        client.__enter__()
        client.put("/api/settings", json={
            "plex_url": url, "plex_token": TOKEN, "plex_music_path": "/data/music"})
        return client

    def finished(self, client, job_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            job = next(j for j in client.get("/api/jobs").json()["jobs"]
                       if j["id"] == job_id)
            if job["stage"] in ("done", "failed") and "Plex" in job["detail"]:
                return job
            time.sleep(0.02)
        raise AssertionError("job never finished with a Plex line")

    def test_the_album_folder_is_scanned_when_the_grab_is_done(
            self, tmp_path, monkeypatch, server):
        client = self.client(tmp_path, monkeypatch, server.url)
        try:
            job = client.post("/api/grab", json={"video_id": "v"}).json()
            done = self.finished(client, job["id"])
        finally:
            client.__exit__(None, None, None)
        assert done["stage"] == "done"
        assert server.refreshes == [("3", "/data/music/Dolly Parton/9 to 5 (1980)")]
        assert "Plex is scanning it into \"Music\"" in done["detail"]

    def test_a_plex_that_is_down_does_not_fail_the_grab(self, tmp_path, monkeypatch):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed = "http://127.0.0.1:%d" % sock.getsockname()[1]
        client = self.client(tmp_path, monkeypatch, closed)
        try:
            job = client.post("/api/grab", json={"video_id": "v"}).json()
            done = self.finished(client, job["id"])
        finally:
            client.__exit__(None, None, None)
        assert done["stage"] == "done"
        assert "Plex was not told" in done["detail"]


class TestTheNetworkGuardStillHolds:
    def test_loopback_access_lets_nothing_else_out(self):
        # The fixture this module uses opens 127.0.0.1 for the fake Plex.
        # A real server must still be refused, or the suite stops being
        # hermetic without anyone noticing.
        import requests
        from conftest import NetworkUsedInTest
        with pytest.raises(NetworkUsedInTest):
            requests.get("http://plex.example.com:32400/library/sections")

"""Signing in to Plex with a PIN, and choosing the server to scan."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import beetdrop.plex as plex
import beetdrop.plexauth as plexauth
from beetdrop.app import create_app
from beetdrop.config import Config
from test_plex import TOKEN as SERVER_TOKEN, FakePlex

pytestmark = pytest.mark.usefixtures("loopback_only")

ACCOUNT_TOKEN = "account-wide-token"


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % sock.getsockname()[1]


class FakePlexTv:
    """plex.tv's PIN and resources endpoints, as Vita Plex uses them.

    It insists on what real plex.tv insists on - a client identifier and
    product on every PIN request - so a client that leaves them out fails
    here rather than only in production.
    """

    def __init__(self, resources):
        self.resources = resources
        self.linked = False
        self.expired = False
        self.calls = []
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

            def _record(self):
                url = urlparse(self.path)
                fake.calls.append((self.command, url.path,
                                   self.headers.get("X-Plex-Client-Identifier"),
                                   self.headers.get("X-Plex-Token")))
                return url

            def do_POST(self):
                url = self._record()
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode())
                if url.path != "/api/v2/pins":
                    return self._reply(404)
                if not (self.headers.get("X-Plex-Client-Identifier")
                        and self.headers.get("X-Plex-Product")):
                    return self._reply(400, {"error": "client identifier required"})
                if form.get("strong") != ["false"]:
                    # A strong PIN is a 25-character one meant for a link
                    # opened by the client, not typed at plex.tv/link.
                    return self._reply(400, {"error": "expected a short code"})
                self._reply(201, {"id": 4242, "code": "QX7P", "expiresIn": 900})

            def do_GET(self):
                url = self._record()
                if url.path == "/api/v2/pins/4242":
                    return self._reply(200, {
                        "id": 4242, "code": "QX7P", "expired": fake.expired,
                        "authToken": ACCOUNT_TOKEN if fake.linked else None})
                if url.path.startswith("/api/v2/pins/"):
                    return self._reply(404)
                if url.path == "/api/v2/resources":
                    if self.headers.get("X-Plex-Token") != ACCOUNT_TOKEN:
                        return self._reply(401)
                    return self._reply(200, fake.resources)
                self._reply(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def plex_server():
    fake = FakePlex([("3", "Music", "artist", ["/data/music"])])
    yield fake
    fake.close()


@pytest.fixture
def plex_tv(plex_server, monkeypatch):
    dead = closed_port_url()
    fake = FakePlexTv([
        {"name": "Shared Box", "provides": "server", "owned": False,
         "sourceTitle": "a friend", "clientIdentifier": "shared-1",
         "accessToken": "shared-token",
         "connections": [{"uri": plex_server.url, "local": True}]},
        {"name": "Living Room TV", "provides": "client,player", "owned": True,
         "clientIdentifier": "tv-1", "connections": []},
        {"name": "Home Server", "provides": "server", "owned": True,
         "productVersion": "1.41.0", "clientIdentifier": "home-1",
         "accessToken": SERVER_TOKEN,
         # A relay listed first and an address that will not answer before
         # the one that does: the order must be fixed and the dead one
         # skipped, as a real server's list often looks.
         "connections": [
             {"uri": dead, "local": False, "relay": True},
             {"uri": dead, "local": True},
             {"uri": plex_server.url, "local": True}]},
    ])
    monkeypatch.setattr(plexauth, "PLEX_TV", fake.url)
    monkeypatch.setattr(plexauth, "PROBE_TIMEOUT", 2)
    plex._cache.clear()
    yield fake
    fake.close()
    plex._cache.clear()


@pytest.fixture
def app(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    config = Config(music_root=music, scratch_root=tmp_path / "s",
                    config_dir=tmp_path / "c")
    with TestClient(create_app(config)) as client:
        client.put("/api/settings", json={"plex_music_path": "/data/music"})
        yield client


def link(app, plex_tv):
    pin = app.post("/api/plex/pin").json()
    plex_tv.linked = True
    return pin, app.get("/api/plex/pin/%s" % pin["id"]).json()


class TestTheCode:
    def test_it_is_a_short_code_to_type_at_plex_tv_link(self, app, plex_tv):
        pin = app.post("/api/plex/pin").json()
        assert pin["code"] == "QX7P"
        assert pin["link"] == "https://plex.tv/link"
        assert pin["expires_in"] == 900

    def test_until_it_is_entered_the_page_is_told_to_wait(self, app, plex_tv):
        pin = app.post("/api/plex/pin").json()
        assert app.get("/api/plex/pin/%s" % pin["id"]).json() == {"state": "waiting"}

    def test_an_expired_code_says_so(self, app, plex_tv):
        pin = app.post("/api/plex/pin").json()
        plex_tv.expired = True
        assert app.get("/api/plex/pin/%s" % pin["id"]).json()["state"] == "expired"

    def test_only_codes_this_beetdrop_made_are_looked_up(self, app, plex_tv):
        # Otherwise the endpoint would ask plex.tv about anyone's PIN.
        assert app.get("/api/plex/pin/9999").json() == {"state": "unknown"}
        assert not [c for c in plex_tv.calls if c[1] == "/api/v2/pins/9999"]

    def test_the_device_identity_is_kept_between_sign_ins(self, app, plex_tv):
        # One "Beetdrop" in the account's Authorized Devices, not one
        # per sign-in.
        app.post("/api/plex/pin")
        app.post("/api/plex/pin")
        ids = {c[2] for c in plex_tv.calls if c[0] == "POST"}
        assert len(ids) == 1 and next(iter(ids)).startswith("beetdrop-")


class TestTheServers:
    def test_once_linked_they_are_listed_and_the_token_is_not(self, app, plex_tv):
        _pin, found = link(app, plex_tv)
        assert found["state"] == "linked"
        assert [s["name"] for s in found["servers"]] == ["Home Server", "Shared Box"]
        text = json.dumps(found)
        assert ACCOUNT_TOKEN not in text and SERVER_TOKEN not in text

    def test_players_are_not_servers(self, app, plex_tv):
        _pin, found = link(app, plex_tv)
        assert "Living Room TV" not in [s["name"] for s in found["servers"]]

    def test_a_shared_server_says_who_shares_it(self, app, plex_tv):
        _pin, found = link(app, plex_tv)
        shared = next(s for s in found["servers"] if s["name"] == "Shared Box")
        assert shared["owned"] is False and shared["shared_by"] == "a friend"


class TestChoosing:
    def test_the_address_that_answers_is_found_and_saved(self, app, plex_tv, plex_server):
        pin, _found = link(app, plex_tv)
        result = app.post("/api/plex/pin/%s/server" % pin["id"],
                          json={"server_id": "home-1"}).json()
        # The relay and the dead local address were tried and passed over.
        assert result["url"] == plex_server.url
        assert result["ok"] is True and result["section"] == "Music"
        settings = app.get("/api/settings").json()
        assert settings["plex_url"] == plex_server.url
        assert settings["plex_token_set"] is True

    def test_what_is_kept_is_the_servers_token_not_the_accounts(
            self, app, plex_tv, tmp_path):
        from beetdrop.config import Config
        from beetdrop.db import Store

        pin, _found = link(app, plex_tv)
        app.post("/api/plex/pin/%s/server" % pin["id"], json={"server_id": "home-1"})
        db = Config(music_root=tmp_path / "music", scratch_root=tmp_path / "s",
                    config_dir=tmp_path / "c").db_path
        assert Store(db).get_settings()["plex_token"] == SERVER_TOKEN
        # Not in a setting, and not anywhere else in the file either.
        assert ACCOUNT_TOKEN.encode() not in db.read_bytes()

    def test_a_shared_server_cannot_be_chosen(self, app, plex_tv):
        pin, _found = link(app, plex_tv)
        response = app.post("/api/plex/pin/%s/server" % pin["id"],
                            json={"server_id": "shared-1"})
        assert response.status_code == 502
        assert "only its owner" in response.json()["detail"]

    def test_a_server_with_no_address_that_answers_says_what_was_tried(
            self, app, plex_tv):
        dead = closed_port_url()
        plex_tv.resources = [{
            "name": "Away", "provides": "server", "owned": True,
            "clientIdentifier": "away-1", "accessToken": SERVER_TOKEN,
            "connections": [{"uri": dead, "local": True}]}]
        pin, _found = link(app, plex_tv)
        response = app.post("/api/plex/pin/%s/server" % pin["id"],
                            json={"server_id": "away-1"})
        assert response.status_code == 502
        assert dead in response.json()["detail"]

    def test_the_sign_in_is_used_up_once_a_server_is_chosen(self, app, plex_tv):
        pin, _found = link(app, plex_tv)
        app.post("/api/plex/pin/%s/server" % pin["id"], json={"server_id": "home-1"})
        # The account token went with it.
        assert app.get("/api/plex/pin/%s" % pin["id"]).json() == {"state": "unknown"}


class TestSigningOut:
    def test_it_forgets_the_server_and_keeps_the_folder_mapping(self, app, plex_tv):
        pin, _found = link(app, plex_tv)
        app.post("/api/plex/pin/%s/server" % pin["id"], json={"server_id": "home-1"})
        app.post("/api/plex/sign-out")
        settings = app.get("/api/settings").json()
        assert settings["plex_url"] == "" and settings["plex_token_set"] is False
        assert settings["plex_music_path"] == "/data/music"


class TestTheOrder:
    def test_local_then_direct_then_relay(self):
        C = plexauth.Connection
        ordered = plexauth._order([C("relay", relay=True), C("remote"),
                                   C("lan", local=True)])
        assert [c.uri for c in ordered] == ["lan", "remote", "relay"]

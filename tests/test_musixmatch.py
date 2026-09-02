"""Musixmatch client, the token endpoint, and the LRCLIB->Musixmatch
fallback chain. All HTTP is faked."""

import pytest
from fastapi.testclient import TestClient

import beetdrop.lyrics as lyrics_module
import beetdrop.musixmatch as mxm
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.musixmatch import MusixmatchError


class FakeResp:
    def __init__(self, data=None, ok=True):
        self._data = data if data is not None else {}
        self.ok = ok

    def json(self):
        return self._data


def token_payload(token="tok-123", status=200):
    return {"message": {"header": {"status_code": status},
                        "body": {"user_token": token}}}


def subtitles_payload(body):
    # Deliberately nested a few levels, as the real macro response is.
    return {"message": {"body": {"macro_calls": {"track.subtitles.get": {
        "message": {"body": {"subtitle_list": [
            {"subtitle": {"subtitle_body": body}}]}}}}}}}


class TestToken:
    def test_fetch_token_ok(self, monkeypatch):
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: FakeResp(token_payload("abc")))
        assert mxm.fetch_token() == "abc"

    def test_refused_token_raises(self, monkeypatch):
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: FakeResp(token_payload("", status=401)))
        with pytest.raises(MusixmatchError):
            mxm.fetch_token()

    def test_network_error_raises(self, monkeypatch):
        def boom(*a, **k):
            raise mxm.requests.RequestException("down")
        monkeypatch.setattr(mxm.requests, "get", boom)
        with pytest.raises(MusixmatchError):
            mxm.fetch_token()


class TestFetchSynced:
    def test_synced_body_extracted(self, monkeypatch):
        lrc = "[00:01.00]Line one\n[00:04.00]Line two"
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: FakeResp(subtitles_payload(lrc)))
        assert mxm.fetch_synced("tok", "Artist", "Song", 200) == lrc

    def test_plain_body_rejected(self, monkeypatch):
        # No timestamps -> not synced -> skipped.
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: FakeResp(subtitles_payload("Just plain words")))
        assert mxm.fetch_synced("tok", "Artist", "Song", 200) is None

    def test_empty_and_missing(self, monkeypatch):
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: FakeResp({"message": {"body": {}}}))
        assert mxm.fetch_synced("tok", "Artist", "Song") is None

    def test_no_token_or_fields(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(mxm.requests, "get",
                            lambda *a, **k: called.__setitem__("n", 1) or FakeResp())
        assert mxm.fetch_synced("", "A", "S") is None
        assert mxm.fetch_synced("tok", "", "S") is None
        assert called["n"] == 0


class TestProviderChain:
    def _mock(self, monkeypatch, lrclib=None, mxm=None):
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda a, t, al, d: lrclib)
        monkeypatch.setattr(lyrics_module.musixmatch, "fetch_synced",
                            lambda tok, a, t, d: mxm)

    def test_default_lrclib_first(self, monkeypatch):
        self._mock(monkeypatch, lrclib="[00:01.00]L", mxm="[00:02.00]M")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", musixmatch_token="tok") == "[00:01.00]L"

    def test_lrclib_falls_back_to_musixmatch(self, monkeypatch):
        self._mock(monkeypatch, lrclib=None, mxm="[00:02.00]M")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", musixmatch_token="tok", provider="lrclib") == "[00:02.00]M"

    def test_musixmatch_primary_first(self, monkeypatch):
        self._mock(monkeypatch, lrclib="[00:01.00]L", mxm="[00:02.00]M")
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", musixmatch_token="tok", provider="musixmatch") == "[00:02.00]M"

    def test_musixmatch_primary_falls_back_to_lrclib(self, monkeypatch):
        self._mock(monkeypatch, lrclib="[00:01.00]L", mxm=None)
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", musixmatch_token="tok", provider="musixmatch") == "[00:01.00]L"

    def test_musixmatch_skipped_without_token(self, monkeypatch):
        monkeypatch.setattr(lyrics_module, "_lrclib", lambda a, t, al, d: None)
        monkeypatch.setattr(lyrics_module.musixmatch, "fetch_synced",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
        # No token: musixmatch never called even when it is primary.
        assert lyrics_module.fetch_synced_lyrics(
            "A", "S", provider="musixmatch") is None


class TestTokenEndpoint:
    def make_config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_fetch_stores_token(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)
        monkeypatch.setattr(app_module.musixmatch, "fetch_token", lambda: "stored-tok")
        with TestClient(create_app(config)) as client:
            settings = client.get("/api/settings").json()
            assert settings["musixmatch_token_set"] is False
            resp = client.post("/api/lyrics/musixmatch-token")
            assert resp.status_code == 200 and resp.json()["token_set"] is True
            settings = client.get("/api/settings").json()
            assert settings["musixmatch_token_set"] is True
            # The token value itself is never exposed in settings.
            assert "stored-tok" not in str(settings)

    def test_fetch_failure_502(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)

        def boom():
            raise MusixmatchError("rate limited")
        monkeypatch.setattr(app_module.musixmatch, "fetch_token", boom)
        with TestClient(create_app(config)) as client:
            resp = client.post("/api/lyrics/musixmatch-token")
        assert resp.status_code == 502
        assert "rate limited" in resp.json()["detail"]

    def test_manual_token_paste_and_clear(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={"musixmatch_token": "pasted"})
            assert client.get("/api/settings").json()["musixmatch_token_set"] is True
            client.put("/api/settings", json={"musixmatch_token": ""})
            assert client.get("/api/settings").json()["musixmatch_token_set"] is False

    def test_provider_switch_persists_and_validates(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["lyrics_provider"] == "lrclib"
            assert client.put("/api/settings",
                              json={"lyrics_provider": "spotify"}).status_code == 422
            body = client.put("/api/settings",
                              json={"lyrics_provider": "musixmatch"}).json()
            assert body["lyrics_provider"] == "musixmatch"
        from beetdrop.db import Store
        assert Store(config.db_path).get_settings()["lyrics_provider"] == "musixmatch"

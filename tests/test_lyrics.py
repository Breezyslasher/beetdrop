"""Synced-lyrics fetch, sidecar writing, and the setting round-trip."""

import pytest
from fastapi.testclient import TestClient

import beetdrop.lyrics as lyrics_module
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.library import write_lyrics_sidecar


class FakeResp:
    def __init__(self, ok=True, data=None):
        self.ok = ok
        self._data = data or {}

    def json(self):
        return self._data


class TestFetchSynced:
    def test_returns_synced_lrc(self, monkeypatch):
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: FakeResp(data={
                                "syncedLyrics": "[00:01.00]Hello\n[00:03.00]World",
                                "plainLyrics": "Hello\nWorld"}))
        lrc = lyrics_module.fetch_synced_lyrics("Artist", "Song", "Album", 200)
        assert lrc.startswith("[00:01.00]Hello")

    def test_plain_only_is_skipped(self, monkeypatch):
        # Timed only: a result with no syncedLyrics returns None.
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: FakeResp(data={
                                "syncedLyrics": "", "plainLyrics": "Hello"}))
        assert lyrics_module.fetch_synced_lyrics("A", "S", "Al", 200) is None

    def test_404_is_none(self, monkeypatch):
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: FakeResp(ok=False))
        assert lyrics_module.fetch_synced_lyrics("A", "S") is None

    def test_network_error_is_none(self, monkeypatch):
        def boom(*a, **k):
            raise lyrics_module.requests.RequestException("down")
        monkeypatch.setattr(lyrics_module.requests, "get", boom)
        assert lyrics_module.fetch_synced_lyrics("A", "S") is None

    def test_missing_fields_no_request(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(lyrics_module.requests, "get",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or FakeResp())
        assert lyrics_module.fetch_synced_lyrics("", "Song") is None
        assert lyrics_module.fetch_synced_lyrics("Artist", "") is None
        assert called["n"] == 0  # never hit the network without artist+title


class TestSidecar:
    def test_writes_lrc_next_to_audio(self, tmp_path):
        audio = tmp_path / "01 - Song.opus"
        audio.write_bytes(b"x")
        target = write_lyrics_sidecar(audio, "[00:01.00]Hi")
        assert target == tmp_path / "01 - Song.lrc"
        assert target.read_text() == "[00:01.00]Hi"
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    def test_does_not_overwrite(self, tmp_path):
        audio = tmp_path / "s.opus"
        audio.write_bytes(b"x")
        (tmp_path / "s.lrc").write_text("original")
        write_lyrics_sidecar(audio, "new")
        assert (tmp_path / "s.lrc").read_text() == "original"


class TestLyricsSetting:
    def make_config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_default_on_and_toggle_persists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MUSIC_PATH", raising=False)
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["lyrics"] is True
            body = client.put("/api/settings", json={"lyrics": False}).json()
            assert body["lyrics"] is False
        # Persisted across a restart.
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["lyrics"] is False

    def test_env_disables(self, monkeypatch):
        monkeypatch.setenv("BEETDROP_LYRICS", "0")
        assert Config().lyrics_enabled is False
        monkeypatch.setenv("BEETDROP_LYRICS", "1")
        assert Config().lyrics_enabled is True

"""Library filing plumbing: settings, health, and job states."""

import time

import pytest
from fastapi.testclient import TestClient

import beetdrop.jobs as jobs_module
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.grab import GrabOutcome
from beetdrop.search import Result


def make_config(tmp_path, **overrides):
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    defaults = dict(music_root=music,
                    scratch_root=tmp_path / "scratch",
                    config_dir=tmp_path / "config", min_free_mb=0,
                    track_delay="0")
    defaults.update(overrides)
    return Config(**defaults)


def wait_done(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json()["jobs"]
                    if j["id"] == job_id), None)
        if job and job["stage"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def grab_faker(verified):
    def fake(video_id, cfg, fmt="", bitrate="", on_stage=lambda s: None,
             on_progress=lambda p: None, on_resolved=lambda r: None,
             logger=None):
        destination = cfg.music_root / "Artist" / "Album (1999)" / "01 - T.opus"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")
        return GrabOutcome(
            inbox_path=destination,
            result=Result(video_id=video_id, title="T", raw_title="T",
                          artists=["Artist"]),
            verified=verified)
    return fake


class TestLibrarySettings:
    def test_music_root_roundtrip(self, tmp_path):
        config = make_config(tmp_path)
        other = tmp_path / "other-library"
        other.mkdir()
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["music_root"] == str(config.music_root)
            body = client.put("/api/settings",
                              json={"music_root": str(other)}).json()
            assert body["music_root"] == str(other)
            health = client.get("/api/health").json()
            assert health["library"] == str(other)
            assert health["library_writable"] is True

    def test_health_flags_missing_library(self, tmp_path):
        config = make_config(tmp_path, music_root=tmp_path / "missing-library")
        with TestClient(create_app(config)) as client:
            health = client.get("/api/health").json()
        assert health["status"] == "degraded"
        assert "missing-library" in health["library_problem"]

    def test_music_root_locked_by_env(self, tmp_path, monkeypatch):
        # MUSIC_PATH in the environment (the Docker case) locks the path:
        # it is reported locked, PUT rejects changes, and the env value
        # wins over any stored override.
        env_music = tmp_path / "env-music"
        env_music.mkdir()
        monkeypatch.setenv("MUSIC_PATH", str(env_music))
        config = make_config(tmp_path)  # config_dir etc, music_root ignored below
        config.music_root = env_music
        with TestClient(create_app(config)) as client:
            settings = client.get("/api/settings").json()
            assert settings["music_root_locked"] is True
            assert settings["music_root"] == str(env_music)
            # A change attempt is refused.
            resp = client.put("/api/settings", json={"music_root": "/somewhere/else"})
            assert resp.status_code == 422
            # And even a stored value (from a pre-lock era) is ignored.
            from beetdrop.db import Store
            Store(config.db_path).set_settings({"music_root": "/stale/path"})
            assert client.get("/api/health").json()["library"] == str(env_music)

    def test_music_root_editable_without_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MUSIC_PATH", raising=False)
        config = make_config(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["music_root_locked"] is False
            assert client.put("/api/settings",
                              json={"music_root": str(other)}).status_code == 200


class TestFilingStates:
    def test_verified_grab_marks_filed(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        monkeypatch.setattr(jobs_module, "run_grab", grab_faker(verified=True))
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "v1"}).json()
            finished = wait_done(client, job["id"])
        assert finished["stage"] == "done"
        assert finished["inbox_state"] == "filed"

    def test_unverified_grab_marked(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        monkeypatch.setattr(jobs_module, "run_grab", grab_faker(verified=False))
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "v1"}).json()
            finished = wait_done(client, job["id"])
        assert finished["inbox_state"] == "unverified"

"""Filing-mode plumbing: settings, health, job states, sweep guard."""

import time

import pytest
from fastapi.testclient import TestClient

import beetdrop.jobs as jobs_module
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.grab import GrabOutcome
from beetdrop.search import Result


def make_config(tmp_path, **overrides):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    defaults = dict(inbox=inbox, music_root=music,
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


class TestModeSettings:
    def test_mode_roundtrip_and_validation(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["mode"] == "inbox"
            assert client.put("/api/settings", json={"mode": "sideways"}).status_code == 422
            body = client.put("/api/settings",
                              json={"mode": "library",
                                    "music_root": str(config.music_root)}).json()
            assert body["mode"] == "library"
            assert body["music_root"] == str(config.music_root)
            health = client.get("/api/health").json()
            assert health["mode"] == "library"

    def test_health_checks_active_root(self, tmp_path):
        config = make_config(tmp_path, mode="library",
                             music_root=tmp_path / "missing-library")
        with TestClient(create_app(config)) as client:
            health = client.get("/api/health").json()
        assert health["status"] == "degraded"
        assert "missing-library" in health["inbox_problem"]


class TestLibraryJobStates:
    def test_verified_grab_marks_filed(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, mode="library")
        monkeypatch.setattr(jobs_module, "run_grab", grab_faker(verified=True))
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "v1"}).json()
            finished = wait_done(client, job["id"])
        assert finished["stage"] == "done"
        assert finished["inbox_state"] == "filed"

    def test_unverified_grab_marked(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, mode="library")
        monkeypatch.setattr(jobs_module, "run_grab", grab_faker(verified=False))
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "v1"}).json()
            finished = wait_done(client, job["id"])
        assert finished["inbox_state"] == "unverified"

    def test_sweep_leaves_library_jobs_alone(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, mode="library")
        monkeypatch.setattr(jobs_module, "run_grab", grab_faker(verified=True))
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "v1"}).json()
            wait_done(client, job["id"])
            app_manager = None
            # Drive a sweep directly with an aggressive grace period: a
            # filed job must never flip to review or picked_up.
            from beetdrop.db import Store
            from beetdrop.events import Broadcaster
            from beetdrop.jobs import JobManager
            manager = JobManager(Store(config.db_path), Broadcaster(), lambda: config)
            manager.sweep_inbox(review_after=-1)
            refreshed = next(j for j in client.get("/api/jobs").json()["jobs"]
                             if j["id"] == job["id"])
        assert refreshed["inbox_state"] == "filed"

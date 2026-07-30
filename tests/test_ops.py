"""Operational features: restart resume, disk space check, failure
counter, yt-dlp update endpoint, and job logs."""

import time

import pytest
from fastapi.testclient import TestClient

import beetdrop.app as app_module
import beetdrop.jobs as jobs_module
from beetdrop.app import create_app
from beetdrop.config import Config, InboxError, check_inbox, inbox_problem
from beetdrop.db import Store
from beetdrop.download import LogCollector
from beetdrop.grab import GrabOutcome
from beetdrop.search import Result


def make_config(tmp_path, **overrides):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    defaults = dict(inbox=inbox, scratch_root=tmp_path / "scratch",
                    config_dir=tmp_path / "config", min_free_mb=0)
    defaults.update(overrides)
    return Config(**defaults)


def fake_run_grab_factory(ran):
    def fake_run_grab(video_id, cfg, fmt="", bitrate="", on_stage=lambda s: None,
                      on_progress=lambda p: None, on_resolved=lambda r: None,
                      logger=None):
        ran.append(video_id)
        on_stage("searching")
        if logger is not None:
            logger.add("[download] pretending to fetch %s" % video_id)
        if video_id == "boom":
            raise ValueError("simulated failure")
        destination = cfg.inbox / "Artist - Title"
        destination.mkdir(parents=True, exist_ok=True)
        return GrabOutcome(
            inbox_path=destination,
            result=Result(video_id=video_id, title="Title", raw_title="Title",
                          artists=["Artist"]),
        )
    return fake_run_grab


def wait_for(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json()["jobs"]
                    if j["id"] == job_id), None)
        if job and job["stage"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job %s never finished" % job_id)


class TestRestartResume:
    def test_queued_jobs_rerun_and_inflight_fail(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        ran = []
        monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab_factory(ran))

        # Simulate a previous process that died with one queued and one
        # mid-download job.
        store = Store(config.db_path)
        queued = store.create_job("vid-queued", "opus", "192")
        inflight = store.create_job("vid-inflight", "opus", "192")
        store.update_job(inflight["id"], stage="downloading", progress=40.0)
        store.close()

        with TestClient(create_app(config)) as client:
            finished = wait_for(client, queued["id"])
            assert finished["stage"] == "done"
            jobs = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}
            assert jobs[inflight["id"]]["stage"] == "failed"
            assert "interrupted by restart" in jobs[inflight["id"]]["error"]
        assert ran == ["vid-queued"]


class TestDiskSpace:
    def test_low_space_reports_problem(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        # An absurd minimum no filesystem satisfies.
        problem = inbox_problem(inbox, min_free_mb=10**9)
        assert "below the" in problem
        with pytest.raises(InboxError):
            check_inbox(inbox, min_free_mb=10**9)

    def test_ample_space_is_fine(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        assert inbox_problem(inbox, min_free_mb=1) == ""

    def test_health_degrades_and_grab_fails(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, min_free_mb=10**9)
        ran = []
        monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab_factory(ran))
        with TestClient(create_app(config)) as client:
            health = client.get("/api/health").json()
            assert health["status"] == "degraded"
            assert health["min_free_mb"] == 10**9
            assert health["inbox_free_mb"] >= 0
            job = client.post("/api/grab", json={"video_id": "v"}).json()
            finished = wait_for(client, job["id"])
            assert finished["stage"] == "failed"
            assert "below the" in finished["error"]
        assert ran == []  # refused before any download


class TestFailureCounter:
    def test_counts_recent_failures_only(self, tmp_path):
        store = Store(tmp_path / "db.sqlite3")
        recent = store.create_job("v1", "opus", "192")
        store.update_job(recent["id"], stage="failed", error="x")
        old = store.create_job("v2", "opus", "192")
        store.update_job(old["id"], stage="failed", error="x")
        with store._lock:
            store._db.execute("UPDATE jobs SET updated_at = ? WHERE id = ?",
                              (time.time() - 7200, old["id"]))
            store._db.commit()
        done = store.create_job("v3", "opus", "192")
        store.update_job(done["id"], stage="done")
        assert store.count_failed_since(3600) == 1

    def test_health_exposes_counter(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/health").json()["failures_last_hour"] == 0


class TestJobLog:
    def test_failed_job_stores_log(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        ran = []
        monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab_factory(ran))
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "boom"}).json()
            finished = wait_for(client, job["id"])
        assert finished["stage"] == "failed"
        assert "stage: searching" in finished["log"]
        assert "pretending to fetch boom" in finished["log"]
        assert "ERROR: simulated failure" in finished["log"]

    def test_collector_ring_buffer_and_interface(self):
        collector = LogCollector(limit=3)
        for i in range(5):
            collector.add("line %d" % i)
        assert collector.text() == "line 2\nline 3\nline 4"
        collector.warning("careful")
        collector.error("broken")
        assert "WARNING: careful" in collector.text()
        assert "ERROR: broken" in collector.text()

    def test_retry_clears_log(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        ran = []
        monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab_factory(ran))
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "boom"}).json()
            wait_for(client, job["id"])
            retried = client.post("/api/jobs/%s/retry" % job["id"]).json()
            assert retried["log"] == ""


class TestYtdlpUpdate:
    def test_update_reports_versions(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)

        def fake_run(cmd, **kwargs):
            class R:
                returncode = 0
                stderr = ""
            return R()

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("importlib.metadata.version", lambda name: "2099.01.01")
        with TestClient(create_app(config)) as client:
            body = client.post("/api/ytdlp/update").json()
        assert body["installed_version"] == "2099.01.01"
        assert body["restart_needed"] is True

    def test_update_failure_is_502(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)

        def fake_run(cmd, **kwargs):
            class R:
                returncode = 1
                stderr = "no network"
            return R()

        monkeypatch.setattr("subprocess.run", fake_run)
        with TestClient(create_app(config)) as client:
            response = client.post("/api/ytdlp/update")
        assert response.status_code == 502
        assert "no network" in response.json()["detail"]

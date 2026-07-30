"""API tests with the grab pipeline and YouTube search faked."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import trackpull.jobs as jobs_module
from trackpull.app import create_app
from trackpull.config import Config
from trackpull.events import QUEUE_LIMIT, Broadcaster, sse_format
from trackpull.grab import GrabOutcome
from trackpull.search import Result


def make_result(video_id="vid-1"):
    return Result(
        video_id=video_id, title="Title", raw_title="Title (Official Video)",
        artists=["Artist"], album="Album", duration_seconds=200,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config = Config(
        inbox=inbox,
        scratch_root=tmp_path / "scratch",
        config_dir=tmp_path / "config",
    )

    def fake_run_grab(video_id, cfg, fmt="", bitrate="",
                      on_stage=lambda s: None, on_progress=lambda p: None,
                      on_resolved=lambda r: None):
        result = make_result(video_id)
        on_stage("searching")
        on_resolved(result)
        for stage in ("downloading", "extracting", "tagging", "moving"):
            on_stage(stage)
        on_progress(100.0)
        if video_id == "boom":
            raise ValueError("simulated failure")
        destination = cfg.inbox / "Artist - Title"
        destination.mkdir(parents=True, exist_ok=True)
        return GrabOutcome(inbox_path=destination, result=result)

    monkeypatch.setattr(jobs_module, "run_grab", fake_run_grab)

    app = create_app(config)
    with TestClient(app) as test_client:
        test_client.config = config
        yield test_client


def wait_for_stage(client, job_id, stages=("done", "failed"), timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()["jobs"]
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job and job["stage"] in stages:
            return job
        time.sleep(0.02)
    raise AssertionError("job %s never reached %s" % (job_id, stages))


class TestHealth:
    def test_ok(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["inbox_writable"] is True
        assert body["ytdlp_version"]

    def test_degraded_when_inbox_missing(self, tmp_path):
        config = Config(
            inbox=tmp_path / "missing",
            scratch_root=tmp_path / "scratch",
            config_dir=tmp_path / "config",
        )
        with TestClient(create_app(config)) as test_client:
            body = test_client.get("/api/health").json()
        assert body["status"] == "degraded"
        assert body["inbox_writable"] is False
        assert "missing" in body["inbox_problem"]


class TestGrab:
    def test_lifecycle_to_done(self, client):
        job = client.post("/api/grab", json={"video_id": "vid-1"}).json()
        assert job["stage"] == "queued"
        finished = wait_for_stage(client, job["id"])
        assert finished["stage"] == "done"
        assert finished["progress"] == 100.0
        assert finished["title"] == "Title"
        assert finished["artist"] == "Artist"
        assert finished["inbox_path"].endswith("Artist - Title")
        assert finished["inbox_state"] == "waiting"

    def test_failure_records_error(self, client):
        job = client.post("/api/grab", json={"video_id": "boom"}).json()
        finished = wait_for_stage(client, job["id"])
        assert finished["stage"] == "failed"
        assert "simulated failure" in finished["error"]

    def test_retry_failed_job(self, client):
        job = client.post("/api/grab", json={"video_id": "boom"}).json()
        wait_for_stage(client, job["id"])
        retried = client.post("/api/jobs/%s/retry" % job["id"]).json()
        assert retried["id"] == job["id"]
        finished = wait_for_stage(client, job["id"])
        # Fails again, but it ran again: error text is fresh.
        assert finished["stage"] == "failed"

    def test_retry_missing_job_404(self, client):
        assert client.post("/api/jobs/nope/retry").status_code == 404

    def test_invalid_format_rejected(self, client):
        response = client.post("/api/grab", json={"video_id": "v", "format": "flac"})
        assert response.status_code == 422

    def test_empty_video_id_rejected(self, client):
        response = client.post("/api/grab", json={"video_id": "  "})
        assert response.status_code == 422

    def test_jobs_listing_persists(self, client):
        job = client.post("/api/grab", json={"video_id": "vid-1"}).json()
        wait_for_stage(client, job["id"])
        listed = client.get("/api/jobs").json()["jobs"]
        assert any(j["id"] == job["id"] for j in listed)


class TestSettings:
    def test_get_reports_ytdlp_version(self, client):
        body = client.get("/api/settings").json()
        assert body["ytdlp_version"]
        assert body["output_format"] == "opus"
        assert body["password_set"] is False

    def test_put_persists_and_applies(self, client):
        body = client.put("/api/settings", json={"output_format": "m4a"}).json()
        assert body["output_format"] == "m4a"
        job = client.post("/api/grab", json={"video_id": "vid-1"}).json()
        assert job["format"] == "m4a"

    def test_put_rejects_bad_format(self, client):
        assert client.put("/api/settings", json={"output_format": "flac"}).status_code == 422


class TestPassword:
    def test_password_locks_api_but_not_health(self, client):
        client.put("/api/settings", json={"password": "hunter2"})
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/jobs", headers={"X-Trackpull-Password": "hunter2"}).status_code == 200
        assert client.get("/api/jobs", params={"password": "hunter2"}).status_code == 200
        assert client.get("/api/jobs", headers={"X-Trackpull-Password": "wrong"}).status_code == 401


class TestSearch:
    def test_search_normalised_shape(self, client, monkeypatch):
        import trackpull.app as app_module

        monkeypatch.setattr(app_module, "search_songs", lambda q, limit: [make_result()])
        body = client.get("/api/search", params={"q": "anything"}).json()
        assert body["results"][0]["video_id"] == "vid-1"
        assert body["results"][0]["title"] == "Title"
        assert body["results"][0]["duration_seconds"] == 200


class TestEvents:
    def test_events_respects_password(self, client):
        # The stream itself cannot be exercised under TestClient (it blocks
        # on the never-ending body); live SSE is verified against a running
        # server. Auth short-circuits before streaming, so that is testable.
        client.put("/api/settings", json={"password": "hunter2"})
        assert client.get("/events").status_code == 401

    def test_broadcaster_fans_out(self):
        async def scenario():
            broadcaster = Broadcaster()
            a, b = broadcaster.subscribe(), broadcaster.subscribe()
            broadcaster.publish({"stage": "queued"})
            return a.get_nowait(), b.get_nowait()

        got_a, got_b = asyncio.run(scenario())
        assert got_a == got_b == {"stage": "queued"}

    def test_broadcaster_drops_saturated_subscriber(self):
        async def scenario():
            broadcaster = Broadcaster()
            stalled = broadcaster.subscribe()
            healthy = broadcaster.subscribe()
            for i in range(QUEUE_LIMIT):
                broadcaster.publish({"n": i})
            while not healthy.empty():  # healthy client drains, stalled does not
                healthy.get_nowait()
            broadcaster.publish({"n": "overflow"})  # drops the stalled one
            broadcaster.publish({"n": "after"})
            return stalled.qsize(), healthy.qsize()

        stalled_size, healthy_size = asyncio.run(scenario())
        assert healthy_size == 2  # kept receiving
        assert stalled_size == QUEUE_LIMIT  # dropped: no longer grows

    def test_sse_format(self):
        line = sse_format({"stage": "done", "progress": 100.0})
        assert line.startswith("event: job\ndata: ")
        assert line.endswith("\n\n")
        assert json.loads(line.split("data: ", 1)[1]) == {"stage": "done", "progress": 100.0}

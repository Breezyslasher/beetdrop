"""API tests with the grab pipeline and YouTube search faked."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import beetdrop.jobs as jobs_module
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.events import QUEUE_LIMIT, Broadcaster, sse_format
from beetdrop.grab import GrabOutcome
from beetdrop.search import Result


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
        music_root=inbox,
        scratch_root=tmp_path / "scratch",
        config_dir=tmp_path / "config",
    )

    def fake_run_grab(video_id, cfg, fmt="", bitrate="",
                      on_stage=lambda s: None, on_progress=lambda p: None,
                      on_resolved=lambda r: None, logger=None):
        result = make_result(video_id)
        on_stage("searching")
        on_resolved(result)
        for stage in ("downloading", "extracting", "tagging", "moving"):
            on_stage(stage)
        on_progress(100.0)
        if video_id == "boom":
            raise ValueError("simulated failure")
        destination = cfg.music_root / "Artist - Title"
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
        assert body["library_writable"] is True
        assert body["ytdlp_version"]

    def test_degraded_when_library_missing(self, tmp_path):
        config = Config(
            music_root=tmp_path / "missing",
            scratch_root=tmp_path / "scratch",
            config_dir=tmp_path / "config",
        )
        with TestClient(create_app(config)) as test_client:
            body = test_client.get("/api/health").json()
        assert body["status"] == "degraded"
        assert body["library_writable"] is False
        assert "missing" in body["library_problem"]


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
        assert finished["inbox_state"] == "filed"

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

    def test_album_grab_lifecycle(self, client, monkeypatch):
        from beetdrop.grab import AlbumGrabOutcome

        calls = []

        def fake_album_grab(browse_id, cfg, fmt="", bitrate="",
                            on_stage=lambda s: None, on_progress=lambda p: None,
                            on_resolved=lambda t, a: None, on_detail=lambda d: None,
                            logger=None, only_tracks=None):
            calls.append({"only_tracks": only_tracks})
            on_stage("searching")
            on_resolved("The Album", "Artist")
            on_stage("downloading")
            on_detail("track 2/2: Song")
            on_stage("moving")
            destination = cfg.music_root / "Artist - The Album"
            destination.mkdir(parents=True, exist_ok=True)
            failed = [] if only_tracks else [{"n": 3, "title": "Bad One", "reason": "nope"}]
            return AlbumGrabOutcome(
                inbox_path=destination, album_title="The Album",
                album_artist="Artist", delivered=2 if not only_tracks else 1,
                failed=failed,
            )

        monkeypatch.setattr(jobs_module, "run_album_grab", fake_album_grab)
        job = client.post("/api/grab", json={"video_id": "MPREb_1", "kind": "album"}).json()
        assert job["kind"] == "album"
        finished = wait_for_stage(client, job["id"])
        assert finished["stage"] == "done"
        assert finished["title"] == "The Album"
        assert finished["detail"] == "delivered 2/3 tracks; failed: Bad One: nope"
        assert finished["inbox_state"] == "filed"
        assert calls[0] == {"only_tracks": None}
        import json as jsonlib
        assert jsonlib.loads(finished["failed_tracks"]) == [
            {"n": 3, "title": "Bad One", "reason": "nope"}]

        # Retry of a done-with-gaps album targets only the failed tracks.
        retried = client.post("/api/jobs/%s/retry" % job["id"]).json()
        assert retried["stage"] == "queued"
        recovered = wait_for_stage(client, job["id"])
        assert recovered["stage"] == "done"
        assert calls[1]["only_tracks"] == {3}
        assert recovered["detail"] == "retry recovered 1 of 1 failed tracks"
        assert recovered["failed_tracks"] == ""

    def test_invalid_kind_rejected(self, client):
        response = client.post("/api/grab", json={"video_id": "v", "kind": "playlist"})
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
        assert client.get("/api/jobs", headers={"X-Beetdrop-Password": "hunter2"}).status_code == 200
        # The password is never accepted in a query string: query strings
        # end up in access logs.
        assert client.get("/api/jobs", params={"password": "hunter2"}).status_code == 401
        assert client.get("/api/jobs", headers={"X-Beetdrop-Password": "wrong"}).status_code == 401

    def test_login_sets_session_cookie(self, client):
        client.put("/api/settings", json={"password": "hunter2"})
        response = client.post("/api/login", json={"password": "hunter2"})
        assert response.status_code == 200
        assert "beetdrop_session" in response.cookies
        # TestClient carries the cookie jar forward.
        assert client.get("/api/jobs").status_code == 200

    def test_wrong_login_rejected(self, client):
        client.put("/api/settings", json={"password": "hunter2"})
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401

    def test_password_stored_hashed(self, client):
        client.put("/api/settings", json={"password": "hunter2"})
        from beetdrop.db import Store
        stored = Store(client.config.db_path).get_settings()["password"]
        assert "hunter2" not in stored
        assert stored.startswith("pbkdf2:sha256:")

    def test_password_change_invalidates_sessions(self, client):
        client.put("/api/settings", json={"password": "hunter2"})
        client.post("/api/login", json={"password": "hunter2"})
        assert client.get("/api/jobs").status_code == 200
        client.put("/api/settings", json={"password": "different"},
                   headers={"X-Beetdrop-Password": "hunter2"})
        assert client.get("/api/jobs").status_code == 401


class TestSearch:
    def test_search_normalised_shape(self, client, monkeypatch):
        import beetdrop.app as app_module

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

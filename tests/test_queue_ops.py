"""Cancel, duplicate detection, pruning, track spacing, cookies upload,
and the concurrency setting."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import beetdrop.jobs as jobs_module
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.db import Store
from beetdrop.grab import GrabOutcome
from beetdrop.search import Result


def make_config(tmp_path, **overrides):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    defaults = dict(inbox=inbox, scratch_root=tmp_path / "scratch",
                    config_dir=tmp_path / "config", min_free_mb=0,
                    track_delay="0")
    defaults.update(overrides)
    return Config(**defaults)


def wait_for(client, job_id, stages=("done", "failed", "cancelled"), timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json()["jobs"]
                    if j["id"] == job_id), None)
        if job and job["stage"] in stages:
            return job
        time.sleep(0.02)
    raise AssertionError("job %s never reached %s" % (job_id, stages))


class TestCancel:
    def test_cancel_running_job(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        started = threading.Event()

        def slow_grab(video_id, cfg, fmt="", bitrate="", on_stage=lambda s: None,
                      on_progress=lambda p: None, on_resolved=lambda r: None,
                      logger=None):
            on_stage("downloading")
            started.set()
            for i in range(200):
                on_progress(float(i))  # checkpoint raises when cancelled
                time.sleep(0.02)
            raise AssertionError("never cancelled")

        monkeypatch.setattr(jobs_module, "run_grab", slow_grab)
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "v1"}).json()
            assert started.wait(5)
            response = client.post("/api/jobs/%s/cancel" % job["id"])
            assert response.status_code == 200
            finished = wait_for(client, job["id"])
            assert finished["stage"] == "cancelled"
            assert "cancelled" in finished["error"]

    def test_cancelled_job_can_retry(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        cancel_first = {"value": True}

        def grab(video_id, cfg, fmt="", bitrate="", on_stage=lambda s: None,
                 on_progress=lambda p: None, on_resolved=lambda r: None,
                 logger=None):
            if cancel_first["value"]:
                for _ in range(500):
                    on_progress(1.0)
                    time.sleep(0.02)
            destination = cfg.inbox / "Artist - Title"
            destination.mkdir(parents=True, exist_ok=True)
            return GrabOutcome(inbox_path=destination, result=Result(
                video_id=video_id, title="Title", raw_title="Title", artists=["Artist"]))

        monkeypatch.setattr(jobs_module, "run_grab", grab)
        with TestClient(create_app(config)) as client:
            job = client.post("/api/grab", json={"video_id": "v1"}).json()
            time.sleep(0.3)
            client.post("/api/jobs/%s/cancel" % job["id"])
            assert wait_for(client, job["id"])["stage"] == "cancelled"
            cancel_first["value"] = False
            client.post("/api/jobs/%s/retry" % job["id"])
            assert wait_for(client, job["id"])["stage"] == "done"

    def test_cancel_unknown_404(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.post("/api/jobs/nope/cancel").status_code == 404


def fake_ok_grab(video_id, cfg, fmt="", bitrate="", on_stage=lambda s: None,
                 on_progress=lambda p: None, on_resolved=lambda r: None,
                 logger=None):
    destination = cfg.inbox / ("Artist - %s" % video_id)
    destination.mkdir(parents=True, exist_ok=True)
    return GrabOutcome(inbox_path=destination, result=Result(
        video_id=video_id, title="Title", raw_title="Title", artists=["Artist"]))


class TestDuplicates:
    def test_second_grab_conflicts_and_force_overrides(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        monkeypatch.setattr(jobs_module, "run_grab", fake_ok_grab)
        with TestClient(create_app(config)) as client:
            first = client.post("/api/grab", json={"video_id": "v1"}).json()
            wait_for(client, first["id"], stages=("done",))
            conflict = client.post("/api/grab", json={"video_id": "v1"})
            assert conflict.status_code == 409
            detail = conflict.json()["detail"]
            assert "already grabbed" in detail["message"]
            assert detail["existing_job"]["id"] == first["id"]
            forced = client.post("/api/grab", json={"video_id": "v1", "force": True})
            assert forced.status_code == 202

    def test_failed_previous_grab_does_not_conflict(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)

        def boom(video_id, cfg, **kwargs):
            raise ValueError("nope")

        monkeypatch.setattr(jobs_module, "run_grab", boom)
        with TestClient(create_app(config)) as client:
            first = client.post("/api/grab", json={"video_id": "v1"}).json()
            wait_for(client, first["id"], stages=("failed",))
            again = client.post("/api/grab", json={"video_id": "v1"})
            assert again.status_code == 202

    def test_different_kind_does_not_conflict(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        monkeypatch.setattr(jobs_module, "run_grab", fake_ok_grab)
        with TestClient(create_app(config)) as client:
            first = client.post("/api/grab", json={"video_id": "x1"}).json()
            wait_for(client, first["id"], stages=("done",))
            # Same id as an album browseId is a different thing entirely.
            store = Store(config.db_path)
            assert store.find_duplicate("x1", "album") is None


class TestPrune:
    def test_prune_by_count_and_age(self, tmp_path):
        store = Store(tmp_path / "db.sqlite3")
        old_ids, recent_ids = [], []
        for i in range(30):
            job = store.create_job("v%d" % i, "opus", "192")
            store.update_job(job["id"], stage="done")
            if i < 10:
                with store._lock:
                    store._db.execute(
                        "UPDATE jobs SET created_at = ? WHERE id = ?",
                        (time.time() - 90 * 86400, job["id"]))
                    store._db.commit()
                old_ids.append(job["id"])
            else:
                recent_ids.append(job["id"])
        # keep_jobs=15: the 10 old ones are beyond both limits.
        deleted = store.prune(keep_jobs=15, keep_days=30)
        assert deleted == 10
        remaining = {j["id"] for j in store.list_jobs(100)}
        assert remaining == set(recent_ids)

    def test_recent_jobs_kept_even_beyond_count(self, tmp_path):
        store = Store(tmp_path / "db.sqlite3")
        for i in range(20):
            job = store.create_job("v%d" % i, "opus", "192")
            store.update_job(job["id"], stage="done")
        # All are recent, so age protects them despite keep_jobs=5.
        assert store.prune(keep_jobs=5, keep_days=30) == 0

    def test_active_jobs_never_pruned(self, tmp_path):
        store = Store(tmp_path / "db.sqlite3")
        job = store.create_job("v1", "opus", "192")
        store.update_job(job["id"], stage="downloading")
        with store._lock:
            store._db.execute("UPDATE jobs SET created_at = ? WHERE id = ?",
                              (time.time() - 90 * 86400, job["id"]))
            store._db.commit()
        assert store.prune(keep_jobs=0, keep_days=1) == 0


class TestTrackDelay:
    def test_range_parsing(self):
        assert Config(track_delay="2-5").track_delay_range() == (2.0, 5.0)
        assert Config(track_delay="3").track_delay_range() == (3.0, 3.0)
        assert Config(track_delay="5-2").track_delay_range() == (2.0, 5.0)
        assert Config(track_delay="0").track_delay_range() == (0.0, 0.0)
        assert Config(track_delay="garbage").track_delay_range() == (2.0, 5.0)

    def test_sleep_called_between_album_tracks(self, tmp_path, monkeypatch):
        import beetdrop.grab as grab_module
        from tests.test_album import make_lookup

        config = make_config(tmp_path, track_delay="1-1")
        sleeps = []
        monkeypatch.setattr(grab_module.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(grab_module, "lookup_album", lambda b: make_lookup(3))
        monkeypatch.setattr(grab_module, "verify_audio", lambda p: None)
        monkeypatch.setattr(grab_module, "write_seed_tags", lambda p, s: None)

        def fake_download(video_id, scratch_dir, fmt="opus", **kwargs):
            scratch_dir.mkdir(parents=True, exist_ok=True)
            path = scratch_dir / ("%s.%s" % (video_id, fmt))
            path.write_bytes(b"x")
            return path

        monkeypatch.setattr(grab_module, "download_audio", fake_download)
        grab_module.run_album_grab("b1", config)
        assert sleeps == [1.0, 1.0]  # between tracks only, not before the first


class TestCookiesUpload:
    def test_upload_use_and_clear(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["cookies_set"] is False
            body = client.put("/api/settings",
                              json={"cookies": "# Netscape HTTP Cookie File\nline"}).json()
            assert body["cookies_set"] is True
            cookie_file = config.config_dir / "cookies.txt"
            assert cookie_file.read_text().startswith("# Netscape")
            assert (cookie_file.stat().st_mode & 0o777) == 0o600
            cleared = client.put("/api/settings", json={"cookies": ""}).json()
            assert cleared["cookies_set"] is False
            assert not cookie_file.exists()


class TestConcurrencySetting:
    def test_validation_and_persistence(self, tmp_path):
        config = make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            assert client.get("/api/settings").json()["concurrency"] == 2
            assert client.put("/api/settings", json={"concurrency": 9}).status_code == 422
            assert client.put("/api/settings", json={"concurrency": 0}).status_code == 422
            body = client.put("/api/settings", json={"concurrency": 4}).json()
            assert body["concurrency"] == 4
        # Persisted for the next startup.
        assert Store(config.db_path).get_settings()["concurrency"] == "4"

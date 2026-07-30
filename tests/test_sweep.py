"""Handoff watching: inbox_state transitions driven by the inbox folder."""

import shutil
import sqlite3

from trackpull.db import Store
from trackpull.events import Broadcaster
from trackpull.jobs import JobManager


def make_manager(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    return store, JobManager(store, Broadcaster(), lambda: None)


def delivered_job(store, tmp_path, name="Artist - Title"):
    folder = tmp_path / "inbox" / name
    folder.mkdir(parents=True)
    job = store.create_job("vid-1", "opus", "192")
    store.update_job(job["id"], stage="done", progress=100.0,
                     inbox_path=str(folder), inbox_state="waiting")
    return job["id"], folder


class TestSweep:
    def test_young_folder_stays_waiting(self, tmp_path):
        store, manager = make_manager(tmp_path)
        job_id, _ = delivered_job(store, tmp_path)
        manager.sweep_inbox(review_after=3600)
        assert store.get_job(job_id)["inbox_state"] == "waiting"

    def test_folder_past_grace_flags_review(self, tmp_path):
        store, manager = make_manager(tmp_path)
        job_id, _ = delivered_job(store, tmp_path)
        manager.sweep_inbox(review_after=-1)
        assert store.get_job(job_id)["inbox_state"] == "review"

    def test_removed_folder_is_picked_up(self, tmp_path):
        store, manager = make_manager(tmp_path)
        job_id, folder = delivered_job(store, tmp_path)
        shutil.rmtree(folder)
        manager.sweep_inbox(review_after=3600)
        assert store.get_job(job_id)["inbox_state"] == "picked_up"

    def test_review_still_resolves_to_picked_up(self, tmp_path):
        store, manager = make_manager(tmp_path)
        job_id, folder = delivered_job(store, tmp_path)
        manager.sweep_inbox(review_after=-1)
        assert store.get_job(job_id)["inbox_state"] == "review"
        shutil.rmtree(folder)
        manager.sweep_inbox(review_after=-1)
        assert store.get_job(job_id)["inbox_state"] == "picked_up"

    def test_picked_up_is_terminal(self, tmp_path):
        store, manager = make_manager(tmp_path)
        job_id, folder = delivered_job(store, tmp_path)
        shutil.rmtree(folder)
        manager.sweep_inbox(review_after=-1)
        # Folder reappearing (unrelated new grab with the same name) must
        # not flip a finished job back.
        folder.mkdir(parents=True)
        manager.sweep_inbox(review_after=-1)
        assert store.get_job(job_id)["inbox_state"] == "picked_up"

    def test_failed_and_running_jobs_ignored(self, tmp_path):
        store, manager = make_manager(tmp_path)
        job = store.create_job("vid-2", "opus", "192")
        store.update_job(job["id"], stage="failed", error="boom")
        manager.sweep_inbox(review_after=-1)
        assert store.get_job(job["id"])["inbox_state"] == ""


class TestMigration:
    def test_old_database_gains_inbox_state(self, tmp_path):
        path = tmp_path / "state.sqlite3"
        db = sqlite3.connect(str(path))
        db.execute(
            "CREATE TABLE jobs ("
            " id TEXT PRIMARY KEY, video_id TEXT NOT NULL,"
            " title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',"
            " format TEXT NOT NULL, bitrate TEXT NOT NULL,"
            " stage TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0,"
            " error TEXT NOT NULL DEFAULT '', inbox_path TEXT NOT NULL DEFAULT '',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO jobs (id, video_id, format, bitrate, created_at, updated_at)"
            " VALUES ('old1', 'v', 'opus', '192', 1.0, 1.0)"
        )
        db.commit()
        db.close()

        store = Store(path)
        job = store.get_job("old1")
        assert job["inbox_state"] == ""
        assert store.update_job("old1", inbox_state="waiting")["inbox_state"] == "waiting"

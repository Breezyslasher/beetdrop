"""Job and settings store."""

from trackpull.db import Store


def make_store(tmp_path):
    return Store(tmp_path / "state.sqlite3")


class TestJobs:
    def test_create_and_get(self, tmp_path):
        store = make_store(tmp_path)
        job = store.create_job("vid-1", "opus", "192")
        assert job["stage"] == "queued"
        assert job["progress"] == 0
        assert store.get_job(job["id"]) == job

    def test_update(self, tmp_path):
        store = make_store(tmp_path)
        job = store.create_job("vid-1", "opus", "192")
        updated = store.update_job(job["id"], stage="downloading", progress=42.5)
        assert updated["stage"] == "downloading"
        assert updated["progress"] == 42.5
        assert updated["updated_at"] >= job["updated_at"]

    def test_update_ignores_unknown_and_id_fields(self, tmp_path):
        store = make_store(tmp_path)
        job = store.create_job("vid-1", "opus", "192")
        updated = store.update_job(job["id"], id="hacked", nonsense="x", stage="done")
        assert updated["id"] == job["id"]
        assert updated["stage"] == "done"

    def test_list_newest_first(self, tmp_path):
        store = make_store(tmp_path)
        first = store.create_job("vid-1", "opus", "192")
        second = store.create_job("vid-2", "opus", "192")
        listed = store.list_jobs()
        assert [j["id"] for j in listed] == [second["id"], first["id"]]

    def test_get_missing(self, tmp_path):
        assert make_store(tmp_path).get_job("nope") is None

    def test_interrupted_jobs(self, tmp_path):
        store = make_store(tmp_path)
        running = store.create_job("vid-1", "opus", "192")
        store.update_job(running["id"], stage="downloading")
        finished = store.create_job("vid-2", "opus", "192")
        store.update_job(finished["id"], stage="done")
        interrupted = store.interrupted_jobs()
        assert [j["id"] for j in interrupted] == [running["id"]]


class TestSettings:
    def test_roundtrip(self, tmp_path):
        store = make_store(tmp_path)
        assert store.get_settings() == {}
        store.set_settings({"output_format": "m4a", "bitrate": "256"})
        assert store.get_settings() == {"output_format": "m4a", "bitrate": "256"}

    def test_unknown_keys_dropped(self, tmp_path):
        store = make_store(tmp_path)
        store.set_settings({"output_format": "mp3", "evil": "x"})
        assert store.get_settings() == {"output_format": "mp3"}

    def test_persists_across_reopen(self, tmp_path):
        store = make_store(tmp_path)
        store.set_settings({"inbox": "/somewhere"})
        store.close()
        reopened = make_store(tmp_path)
        assert reopened.get_settings() == {"inbox": "/somewhere"}

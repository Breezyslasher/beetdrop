"""MusicBrainz saying "busy" (503): wait and ask again, and if it is still
busy, file the grab unverified rather than throw the download away."""

import pytest

import beetdrop.grab as grab_module
import beetdrop.mb as mb
from beetdrop.config import Config
from beetdrop.download import LogCollector
from beetdrop.search import Result

SEARCH = "https://musicbrainz.org/ws/2/recording?query=x&fmt=json"


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self._body = body if body is not None else {}

    def json(self):
        return self._body


@pytest.fixture
def client(tmp_path, monkeypatch):
    waits = []
    clock = [1000.0]
    # No real sleeping: the gate's waits are recorded and the clock moved.
    monkeypatch.setattr(mb.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        waits.append(round(seconds, 2))
        clock[0] += seconds

    monkeypatch.setattr(mb.time, "sleep", sleep)
    c = mb.MusicBrainzClient(tmp_path / "cache.sqlite3")
    c.waits = waits
    yield c
    c.close()


def answers(monkeypatch, *responses):
    queue = list(responses)
    asked = []

    def get(url, **kwargs):
        asked.append(url)
        return queue.pop(0)

    monkeypatch.setattr(mb.requests, "get", get)
    return asked


class TestAskingAgain:
    def test_a_503_is_asked_again_and_the_answer_used(self, client, monkeypatch):
        asked = answers(monkeypatch, Response(503), Response(200, {"recordings": [1]}))
        assert client._cached_get(SEARCH) == {"recordings": [1]}
        assert len(asked) == 2

    def test_retry_after_is_honoured(self, client, monkeypatch):
        answers(monkeypatch, Response(503, headers={"Retry-After": "7"}),
                Response(200, {}))
        client._cached_get(SEARCH)
        assert 7.0 in client.waits

    def test_an_absurd_retry_after_is_capped(self, client, monkeypatch):
        answers(monkeypatch, Response(503, headers={"Retry-After": "86400"}),
                Response(200, {}))
        client._cached_get(SEARCH)
        assert max(client.waits) == mb.MAX_BACKOFF

    def test_still_busy_after_the_retries_says_busy(self, client, monkeypatch):
        asked = answers(monkeypatch, *[Response(503)] * (len(mb.BACKOFF) + 1))
        with pytest.raises(mb.MBBusy, match="busy"):
            client._cached_get(SEARCH)
        assert len(asked) == len(mb.BACKOFF) + 1
        # About a minute in all, not forever.
        assert sum(client.waits) <= 60

    def test_the_pause_holds_every_request_not_just_this_one(self, client, monkeypatch):
        # It limits by address: another job asking during the pause would
        # only be refused too.
        client._back_off(Response(503, headers={"Retry-After": "9"}), 2.0)
        # Whichever request goes next - any job's - waits out the pause.
        asked = answers(monkeypatch, Response(200, {}))
        client._cached_get(SEARCH + "&another-job")
        assert client.waits == [9.0] and len(asked) == 1

    def test_a_404_is_not_retried(self, client, monkeypatch):
        asked = answers(monkeypatch, Response(404))
        with pytest.raises(mb.MBError) as caught:
            client._cached_get(SEARCH)
        assert not isinstance(caught.value, mb.MBBusy)
        assert len(asked) == 1

    def test_a_busy_answer_is_not_cached(self, client, monkeypatch):
        answers(monkeypatch, *[Response(503)] * (len(mb.BACKOFF) + 1))
        with pytest.raises(mb.MBBusy):
            client._cached_get(SEARCH)
        asked = answers(monkeypatch, Response(200, {"recordings": []}))
        assert client._cached_get(SEARCH) == {"recordings": []}
        assert len(asked) == 1


class TestTheGrabSurvives:
    def run(self, tmp_path, monkeypatch, error):
        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")

        def fake_download(video_id, scratch_dir, fmt="opus", **kwargs):
            scratch_dir.mkdir(parents=True, exist_ok=True)
            path = scratch_dir / ("%s.%s" % (video_id, fmt))
            path.write_bytes(b"audio")
            return path

        def busy(*args):
            raise error

        class FakeMB:
            def fetch_cover(self, **kwargs):
                return None

        monkeypatch.setattr(grab_module, "download_audio", fake_download)
        monkeypatch.setattr(grab_module, "verify_audio", lambda p: None)
        monkeypatch.setattr(grab_module, "write_full_tags",
                            lambda p, t, c=None, m="image/jpeg": None)
        monkeypatch.setattr(grab_module, "get_mb_client", lambda c: FakeMB())
        monkeypatch.setattr(grab_module, "resolve_track", busy)
        monkeypatch.setattr(grab_module, "lookup_video", lambda v: Result(
            video_id=v, title="9 to 5", raw_title="9 to 5",
            artists=["Dolly Parton"], duration_seconds=161))
        log = LogCollector()
        return grab_module.run_grab("vid", config, logger=log), log, config

    def test_a_busy_musicbrainz_files_it_to_review(self, tmp_path, monkeypatch):
        outcome, log, config = self.run(
            tmp_path, monkeypatch, mb.MBBusy("MusicBrainz is busy (answered 503)"))
        assert outcome.verified is False
        assert outcome.inbox_path.exists()
        assert "_review" in outcome.inbox_path.relative_to(config.music_root).parts
        assert "503" in outcome.note and "_review" in outcome.note
        assert "filed unverified" in log.text()

    def test_an_unreachable_musicbrainz_does_the_same(self, tmp_path, monkeypatch):
        outcome, _log, _config = self.run(
            tmp_path, monkeypatch, mb.MBError("MusicBrainz unreachable: ConnectionError"))
        assert outcome.verified is False and outcome.inbox_path.exists()

    def test_any_other_failure_still_fails(self, tmp_path, monkeypatch):
        # Only MusicBrainz trouble is forgiven; a bug is not hidden.
        with pytest.raises(KeyError):
            self.run(tmp_path, monkeypatch, KeyError("bug"))

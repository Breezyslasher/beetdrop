"""Background job execution.

Downloads run in a dedicated thread pool capped at 2 workers; excess jobs
sit in its queue at stage "queued". Searches never touch this pool, so
search never blocks on a download. Worker threads report through the
event loop into the SSE broadcaster.
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .config import Config, check_storage
from .db import Store
from .download import LogCollector
from .events import Broadcaster
from .grab import failures_text, run_album_grab, run_grab

DEFAULT_CONCURRENCY = 2
PROGRESS_MIN_INTERVAL = 0.5  # seconds between persisted progress updates


class JobCancelled(Exception):
    """Raised inside a worker when the user cancels the job; propagates
    out through yt-dlp's hooks and the pipeline's callbacks."""

MAINTENANCE_INTERVAL = 60  # seconds between history-pruning passes


class JobManager:
    def __init__(self, store: Store, broadcaster: Broadcaster,
                 config_provider: Callable[[], Config],
                 max_workers: int = DEFAULT_CONCURRENCY):
        self._store = store
        self._broadcaster = broadcaster
        self._config_provider = config_provider
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(4, max_workers)), thread_name_prefix="grab"
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sweep_task: Optional[asyncio.Task] = None
        self._cancel_requested: set = set()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Jobs that never started can simply run now; jobs that were
        # mid-flight when the process died cannot finish and are surfaced
        # honestly instead of showing them stuck.
        for job in self._store.interrupted_jobs():
            if job["stage"] == "queued":
                self._executor.submit(self._run, job["id"])
            else:
                self._update(job["id"], stage="failed",
                             error="interrupted by restart; retry to run again")
        self._sweep_task = self._loop.create_task(self._sweep_loop())

    def shutdown(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(MAINTENANCE_INTERVAL)
            try:
                config = self._config_provider()
                self._store.prune(config.keep_jobs, config.keep_days)
            except Exception:
                pass  # a failed pass just means the next one tries again

    def active_count(self) -> int:
        return sum(
            1 for job in self._store.list_jobs()
            if job["stage"] not in ("done", "failed", "cancelled")
        )

    def cancel(self, job_id: str) -> Optional[dict]:
        """Request cancellation. Queued jobs cancel before starting;
        running jobs stop at their next progress or stage callback."""
        job = self._store.get_job(job_id)
        if job is None:
            return None
        if job["stage"] in ("done", "failed", "cancelled"):
            return job
        self._cancel_requested.add(job_id)
        return job

    def _checkpoint(self, job_id: str) -> None:
        if job_id in self._cancel_requested:
            raise JobCancelled()

    def enqueue(self, video_id: str, fmt: str = "", bitrate: str = "",
                kind: str = "track") -> dict:
        config = self._config_provider()
        job = self._store.create_job(
            video_id, fmt or config.output_format, bitrate or config.bitrate,
            kind=kind,
        )
        self._publish(job)
        self._executor.submit(self._run, job["id"])
        return job

    def retry(self, job_id: str) -> Optional[dict]:
        job = self._store.get_job(job_id)
        if job is None:
            return None
        if job["stage"] in ("failed", "cancelled"):
            job = self._update(job_id, stage="queued", progress=0.0, error="",
                               detail="", log="", failed_tracks="",
                               inbox_path="", inbox_state="")
            self._executor.submit(self._run, job_id)
            return job
        if (job["stage"] == "done" and job["kind"] == "album"
                and job["failed_tracks"]):
            # Retry only the tracks that failed; failed_tracks stays so
            # the run knows which track numbers to fetch. Recovered files
            # join the existing album folder.
            job = self._update(job_id, stage="queued", progress=0.0,
                               error="", detail="", log="")
            self._executor.submit(self._run, job_id)
            return job
        return job

    # -- worker thread side --------------------------------------------------

    def _run(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        if job is None:
            return
        if job_id in self._cancel_requested:
            # Cancelled while still queued.
            self._cancel_requested.discard(job_id)
            self._update(job_id, stage="cancelled", error="cancelled before start")
            return
        config = self._config_provider()
        last_progress = {"at": 0.0, "value": -1.0}
        collector = LogCollector()

        def on_stage(stage: str) -> None:
            self._checkpoint(job_id)
            collector.add("stage: %s" % stage)
            self._update(job_id, stage=stage)

        def on_progress(pct: float) -> None:
            self._checkpoint(job_id)
            now = time.monotonic()
            if (now - last_progress["at"] < PROGRESS_MIN_INTERVAL
                    and pct - last_progress["value"] < 5.0 and pct < 100.0):
                return
            last_progress["at"] = now
            last_progress["value"] = pct
            self._update(job_id, progress=round(pct, 1))

        def on_detail(text: str) -> None:
            self._checkpoint(job_id)
            collector.add(text)
            self._update(job_id, detail=text)

        def audio_state(verified: bool) -> str:
            return "filed" if verified else "unverified"

        try:
            check_storage(config.music_root, config.min_free_mb)
            if job["kind"] == "album":
                only_tracks = None
                if job["failed_tracks"]:
                    # This run is a targeted retry of previously failed
                    # tracks; recovered files join the album folder.
                    try:
                        previous = json.loads(job["failed_tracks"])
                    except ValueError:
                        previous = []
                    only_tracks = {f.get("n") for f in previous if f.get("n")}
                outcome = run_album_grab(
                    job["video_id"], config,
                    fmt=job["format"], bitrate=job["bitrate"],
                    on_stage=on_stage, on_progress=on_progress,
                    on_resolved=lambda title, artist: self._update(
                        job_id, title=title, artist=artist),
                    on_detail=on_detail,
                    logger=collector,
                    only_tracks=only_tracks,
                )
                if only_tracks is not None:
                    detail = "retry recovered %d of %d failed tracks" % (
                        outcome.delivered, len(only_tracks))
                else:
                    total = outcome.delivered + len(outcome.failed)
                    detail = "delivered %d/%d tracks" % (outcome.delivered, total)
                if outcome.failed:
                    detail += "; still failing: " if only_tracks is not None else "; failed: "
                    detail += failures_text(outcome.failed)
                self._update(job_id, stage="done", progress=100.0,
                             detail=detail[:2000], log=collector.text()[:20000],
                             failed_tracks=json.dumps(outcome.failed) if outcome.failed else "",
                             inbox_path=str(outcome.inbox_path),
                             inbox_state=audio_state(outcome.verified))
            else:
                outcome = run_grab(
                    job["video_id"], config,
                    fmt=job["format"], bitrate=job["bitrate"],
                    on_stage=on_stage, on_progress=on_progress,
                    on_resolved=lambda result: self._update(
                        job_id, title=result.title, artist=result.artist_display),
                    logger=collector,
                )
                self._update(job_id, stage="done", progress=100.0,
                             log=collector.text()[:20000],
                             inbox_path=str(outcome.inbox_path),
                             inbox_state=audio_state(outcome.verified))
        except JobCancelled:
            collector.add("cancelled by user")
            self._update(job_id, stage="cancelled", error="cancelled by user",
                         log=collector.text()[:20000])
        except Exception as exc:
            if job_id in self._cancel_requested:
                # yt-dlp can wrap the cancellation raised inside its
                # hooks; a failure while cancellation was requested is a
                # cancellation, not an error.
                collector.add("cancelled by user")
                self._update(job_id, stage="cancelled", error="cancelled by user",
                             log=collector.text()[:20000])
            else:
                collector.add("ERROR: %s" % exc)
                self._update(job_id, stage="failed", error=str(exc)[:2000],
                             log=collector.text()[:20000])
        finally:
            self._cancel_requested.discard(job_id)

    # -- plumbing --------------------------------------------------------------

    def _update(self, job_id: str, **fields) -> Optional[dict]:
        job = self._store.update_job(job_id, **fields)
        if job is not None:
            self._publish(job)
        return job

    def _publish(self, job: dict) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._broadcaster.publish, job)
        except RuntimeError:
            pass  # loop shut down mid-job

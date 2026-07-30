"""Background job execution.

Downloads run in a dedicated thread pool capped at 2 workers; excess jobs
sit in its queue at stage "queued". Searches never touch this pool, so
search never blocks on a download. Worker threads report through the
event loop into the SSE broadcaster.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .config import Config, check_inbox
from .db import Store
from .events import Broadcaster
from .grab import run_grab

MAX_CONCURRENT_DOWNLOADS = 2
PROGRESS_MIN_INTERVAL = 0.5  # seconds between persisted progress updates


class JobManager:
    def __init__(self, store: Store, broadcaster: Broadcaster, config_provider: Callable[[], Config]):
        self._store = store
        self._broadcaster = broadcaster
        self._config_provider = config_provider
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_DOWNLOADS, thread_name_prefix="grab"
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Jobs that were mid-flight when the process last died can never
        # finish; surface that honestly instead of showing them stuck.
        for job in self._store.interrupted_jobs():
            self._update(job["id"], stage="failed",
                         error="interrupted by restart; retry to run again")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def active_count(self) -> int:
        return sum(
            1 for job in self._store.list_jobs()
            if job["stage"] not in ("done", "failed")
        )

    def enqueue(self, video_id: str, fmt: str = "", bitrate: str = "") -> dict:
        config = self._config_provider()
        job = self._store.create_job(
            video_id, fmt or config.output_format, bitrate or config.bitrate
        )
        self._publish(job)
        self._executor.submit(self._run, job["id"])
        return job

    def retry(self, job_id: str) -> Optional[dict]:
        job = self._store.get_job(job_id)
        if job is None:
            return None
        if job["stage"] != "failed":
            return job
        job = self._update(job_id, stage="queued", progress=0.0, error="", inbox_path="")
        self._executor.submit(self._run, job_id)
        return job

    # -- worker thread side --------------------------------------------------

    def _run(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        if job is None:
            return
        config = self._config_provider()
        last_progress = {"at": 0.0, "value": -1.0}

        def on_stage(stage: str) -> None:
            self._update(job_id, stage=stage)

        def on_progress(pct: float) -> None:
            now = time.monotonic()
            if (now - last_progress["at"] < PROGRESS_MIN_INTERVAL
                    and pct - last_progress["value"] < 5.0 and pct < 100.0):
                return
            last_progress["at"] = now
            last_progress["value"] = pct
            self._update(job_id, progress=round(pct, 1))

        def on_resolved(result) -> None:
            self._update(job_id, title=result.title, artist=result.artist_display)

        try:
            check_inbox(config.inbox)
            outcome = run_grab(
                job["video_id"], config,
                fmt=job["format"], bitrate=job["bitrate"],
                on_stage=on_stage, on_progress=on_progress, on_resolved=on_resolved,
            )
            self._update(job_id, stage="done", progress=100.0,
                         inbox_path=str(outcome.inbox_path))
        except Exception as exc:
            self._update(job_id, stage="failed", error=str(exc)[:2000])

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

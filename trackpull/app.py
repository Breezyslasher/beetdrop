"""FastAPI application.

Endpoints:

    GET  /api/search?q=&limit=        songs-filtered search, normalised
    POST /api/grab                    {video_id, format, bitrate}
    GET  /api/jobs                    recent jobs from SQLite
    POST /api/jobs/{id}/retry
    GET  /api/settings
    PUT  /api/settings
    GET  /api/health                  inbox writability and yt-dlp version
    GET  /events                      SSE stream of job state changes

Auth is an optional single shared password, LAN-tool grade: when set,
requests carry it in an X-Trackpull-Password header or a password query
parameter (EventSource cannot set headers). /api/health stays open so
container healthchecks work.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import __version__
from .config import SUPPORTED_FORMATS, Config, inbox_problem
from .db import Store
from .download import ytdlp_version
from .events import Broadcaster, sse_format
from .jobs import JobManager
from .search import search_songs

SSE_KEEPALIVE_SECONDS = 15


class GrabRequest(BaseModel):
    video_id: str
    format: str = ""
    bitrate: str = ""


class SettingsUpdate(BaseModel):
    output_format: Optional[str] = None
    bitrate: Optional[str] = None
    inbox: Optional[str] = None
    password: Optional[str] = None


def create_app(base_config: Optional[Config] = None) -> FastAPI:
    base = base_config or Config()
    store = Store(base.db_path)
    broadcaster = Broadcaster()

    def effective_config() -> Config:
        """Environment defaults, overridden by settings stored in SQLite."""
        stored = store.get_settings()
        config = replace(base)
        if stored.get("output_format"):
            config.output_format = stored["output_format"]
        if stored.get("bitrate"):
            config.bitrate = stored["bitrate"]
        if stored.get("inbox"):
            config.inbox = Path(stored["inbox"])
        if stored.get("password"):
            config.password = stored["password"]
        return config

    manager = JobManager(store, broadcaster, effective_config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start()
        problem = inbox_problem(effective_config().inbox)
        if problem:
            # Loud at startup, and again in /api/health; the process still
            # serves so the problem is visible over HTTP, not just in logs.
            print("WARNING: %s" % problem)
        yield
        manager.shutdown()
        store.close()

    app = FastAPI(title="trackpull", version=__version__, lifespan=lifespan)

    async def require_password(request: Request) -> None:
        password = effective_config().password
        if not password:
            return
        supplied = (
            request.headers.get("x-trackpull-password")
            or request.query_params.get("password")
            or ""
        )
        if supplied != password:
            raise HTTPException(status_code=401, detail="password required")

    protected = Depends(require_password)

    # -- endpoints -----------------------------------------------------------

    @app.get("/api/search", dependencies=[protected])
    async def api_search(q: str, limit: int = 8):
        limit = max(1, min(limit, 20))
        # to_thread keeps the loop free; downloads run in their own pool,
        # so a search never waits behind one.
        results = await asyncio.to_thread(search_songs, q, limit)
        return {"results": [asdict(r) for r in results]}

    @app.post("/api/grab", status_code=202, dependencies=[protected])
    async def api_grab(body: GrabRequest):
        if body.format and body.format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        if not body.video_id.strip():
            raise HTTPException(status_code=422, detail="video_id is required")
        job = manager.enqueue(body.video_id.strip(), body.format, body.bitrate)
        return job

    @app.get("/api/jobs", dependencies=[protected])
    async def api_jobs(limit: int = 50):
        return {"jobs": store.list_jobs(max(1, min(limit, 200)))}

    @app.post("/api/jobs/{job_id}/retry", dependencies=[protected])
    async def api_retry(job_id: str):
        job = manager.retry(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.get("/api/settings", dependencies=[protected])
    async def api_get_settings():
        config = effective_config()
        return {
            "output_format": config.output_format,
            "bitrate": config.bitrate,
            "inbox": str(config.inbox),
            "password_set": bool(config.password),
            "ytdlp_version": ytdlp_version(),  # read-only
        }

    @app.put("/api/settings", dependencies=[protected])
    async def api_put_settings(body: SettingsUpdate):
        if body.output_format is not None and body.output_format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        store.set_settings(updates)
        return await api_get_settings()

    @app.get("/api/health")
    async def api_health():
        config = effective_config()
        problem = inbox_problem(config.inbox)
        return {
            "status": "ok" if not problem else "degraded",
            "inbox": str(config.inbox),
            "inbox_writable": not problem,
            "inbox_problem": problem,
            "ytdlp_version": ytdlp_version(),
            "version": __version__,
            "active_jobs": manager.active_count(),
        }

    @app.get("/events", dependencies=[protected])
    async def events(request: Request):
        queue = broadcaster.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=SSE_KEEPALIVE_SECONDS
                        )
                        yield sse_format(event)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                broadcaster.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    return app

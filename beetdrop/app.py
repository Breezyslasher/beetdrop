"""FastAPI application.

Endpoints:

    GET  /api/search?q=&limit=        songs-filtered search, normalised
    POST /api/grab                    {video_id, format, bitrate}
    GET  /api/jobs                    recent jobs from SQLite
    POST /api/jobs/{id}/retry
    GET  /api/settings
    PUT  /api/settings
    GET  /api/health                  library writability and yt-dlp version
    GET  /events                      SSE stream of job state changes

Auth is an optional single shared password, LAN-tool grade: when set,
requests carry it in an X-Beetdrop-Password header or a password query
parameter (EventSource cannot set headers). /api/health stays open so
container healthchecks work.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets as _secrets
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, apple, applesignin, lyrics, musixmatch, updater
from .auth import (
    LoginThrottle,
    check_session_token,
    hash_password,
    is_hashed,
    load_or_create_secret,
    make_session_token,
    verify_password,
)
from .config import SUPPORTED_FORMATS, Config, storage_free_mb, storage_problem
from .db import Store
from .download import ytdlp_version
from .events import Broadcaster, sse_format
from .jobs import JobManager
from .lyrics import PROVIDERS as LYRICS_PROVIDERS
from .search import search_albums, search_songs, search_videos
from .settings import apply_stored_settings

SSE_KEEPALIVE_SECONDS = 15
STATIC_DIR = Path(__file__).parent / "static"


class GrabRequest(BaseModel):
    video_id: str  # a videoId for kind=track, an album browseId for kind=album
    kind: str = "track"
    format: str = ""
    bitrate: str = ""
    force: bool = False  # grab even when it was already grabbed
    # Also fetch the track's music video and file it beside the track,
    # named the way Plex links a video to a track. Tracks only.
    with_video: bool = False


class SettingsUpdate(BaseModel):
    output_format: Optional[str] = None
    bitrate: Optional[str] = None
    password: Optional[str] = None
    concurrency: Optional[int] = None
    cookies: Optional[str] = None  # cookies.txt content; "" clears
    music_root: Optional[str] = None
    lyrics: Optional[bool] = None
    lyrics_provider: Optional[str] = None  # lrclib | musixmatch | apple
    musixmatch_token: Optional[str] = None  # "" clears
    video_root: Optional[str] = None
    video_max_height: Optional[int] = None
    apple_token: Optional[str] = None  # "" clears
    apple_storefront: Optional[str] = None
    word_lyrics: Optional[bool] = None
    plex_url: Optional[str] = None
    plex_token: Optional[str] = None  # "" clears; never sent back
    plex_music_path: Optional[str] = None
    plex_section: Optional[str] = None  # "" = automatic


class LoginRequest(BaseModel):
    password: str


class AppleSignInRequest(BaseModel):
    apple_id: str
    password: str  # used for SRP only; never stored or logged


class AppleVerifyRequest(BaseModel):
    flow_id: str
    code: str


class ReviewChoice(BaseModel):
    path: str
    song_id: str = ""    # "" means leave this track alone


class MatchChoice(BaseModel):
    """A recording picked by hand for a track that matched wrong.

    Module level on purpose: `from __future__ import annotations` makes
    every annotation a string, and FastAPI resolves those against module
    globals. Declared inside create_app it was invisible, and the
    endpoint took `body` as a query parameter and rejected every request.
    """
    path: str
    recording_id: str
    title: str = ""      # what to search MusicBrainz with
    artist: str = ""
    album: str = ""      # optional: narrows the search, picks the release
    # A wrong match means a wrong folder too, since the path was built
    # from the same tags - so the file moves by default. False re-tags it
    # where it stands, for a library whose layout is not Beetdrop's.
    move: bool = True


class PlexServerChoice(BaseModel):
    """The server picked after a Plex PIN sign-in. Module level for the
    same reason as MatchChoice."""
    server_id: str


SESSION_COOKIE = "beetdrop_session"


def secrets_token() -> str:
    return _secrets.token_urlsafe(24)


def create_app(base_config: Optional[Config] = None) -> FastAPI:
    base = base_config or Config()
    store = Store(base.db_path)
    broadcaster = Broadcaster()

    uploaded_cookies = base.config_dir / "cookies.txt"
    # When MUSIC_PATH is set (always true in the Docker image), the library
    # location is the container mount and must NOT be editable from the UI:
    # setting an unmounted host path there just breaks writes. The env value
    # then wins over any stored override.
    music_locked = "MUSIC_PATH" in os.environ
    # Same reasoning for the video library: when VIDEO_PATH pins it to a
    # container mount, the UI must not offer to repoint it off the mount.
    video_locked = "VIDEO_PATH" in os.environ

    def effective_config() -> Config:
        """Environment defaults, overridden by settings stored in SQLite.

        The merge lives in settings.py so `python -m beetdrop` applies the
        same one. While it was a closure here the CLI saw environment
        variables only, so every command ran as though the tokens on the
        Settings page were not configured.
        """
        config = apply_stored_settings(base, store.get_settings(),
                                       music_locked=music_locked,
                                       video_locked=video_locked)
        # Cookies uploaded through Settings win over the mounted file.
        if uploaded_cookies.is_file() and uploaded_cookies.stat().st_size > 0:
            config.cookies_file = str(uploaded_cookies)
        return config

    secret = load_or_create_secret(base.config_dir)
    throttle = LoginThrottle()
    # A yt-dlp updated through the UI persists in /config; load it now
    # if it is at least as new as the bundled copy.
    try:
        updater.activate(base.config_dir)
    except Exception as exc:
        print("WARNING: could not activate persisted yt-dlp update: %s" % exc)
    # Worker pool size comes from settings at startup; changing the
    # setting applies on the next restart.
    manager = JobManager(store, broadcaster, effective_config,
                         max_workers=effective_config().concurrency)

    # A plaintext password stored by an earlier version is hashed in
    # place on startup; it never needs to exist in plaintext again.
    stored_settings = store.get_settings()
    if stored_settings.get("password") and not is_hashed(stored_settings["password"]):
        store.set_settings({"password": hash_password(stored_settings["password"])})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start()
        problem = storage_problem(effective_config().music_root)
        if problem:
            # Loud at startup, and again in /api/health; the process still
            # serves so the problem is visible over HTTP, not just in logs.
            print("WARNING: %s" % problem)
        if not store.get_settings().get("companion_names_v2"):
            # Once: music videos grabbed by 0.65-0.67 were named for the
            # title, which Plex does not link to the track. In the
            # background, since it walks the whole music folder.
            def rename_old_companions():
                from .companion import rename_title_named
                try:
                    count = rename_title_named(effective_config().music_root)
                    if count:
                        print("renamed %d music video(s) so Plex links them" % count)
                    store.set_settings({"companion_names_v2": "1"})
                except Exception as exc:
                    # Shut down mid-walk, or the folder is unreadable: it
                    # is tried again on the next start.
                    print("WARNING: renaming music videos for Plex: %s" % exc)
            threading.Thread(target=rename_old_companions, daemon=True,
                             name="companion-rename").start()
        yield
        manager.shutdown()
        store.close()

    app = FastAPI(title="beetdrop", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def client_key(request: Request) -> str:
        # Behind a Cloudflare tunnel every connection arrives from
        # cloudflared, so the real client is in CF-Connecting-IP.
        return (
            request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )

    async def require_password(request: Request) -> None:
        password = effective_config().password
        if not password:
            return
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if cookie and check_session_token(cookie, secret, password):
            return
        # Header auth for scripts and curl. Failed attempts count toward
        # the same throttle as login attempts. The password is never
        # accepted in a query string - query strings end up in logs.
        # The pre-rename header name is still accepted.
        header = (request.headers.get("x-beetdrop-password", "")
                  or request.headers.get("x-trackpull-password", ""))
        if header:
            key = client_key(request)
            if throttle.retry_after(key):
                raise HTTPException(status_code=429, detail="too many attempts; try later")
            if verify_password(header, password):
                throttle.record_success(key)
                return
            throttle.record_failure(key)
        raise HTTPException(status_code=401, detail="password required")

    protected = Depends(require_password)

    @app.post("/api/login")
    async def api_login(body: LoginRequest, request: Request, response: Response):
        password = effective_config().password
        if not password:
            return {"ok": True, "password_required": False}
        key = client_key(request)
        wait = throttle.retry_after(key)
        if wait:
            raise HTTPException(
                status_code=429,
                detail="too many attempts; try again in %d seconds" % wait,
                headers={"Retry-After": str(wait)},
            )
        if not verify_password(body.password, password):
            throttle.record_failure(key)
            raise HTTPException(status_code=401, detail="wrong password")
        throttle.record_success(key)
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        response.set_cookie(
            SESSION_COOKIE,
            make_session_token(secret, password),
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
            secure=(request.url.scheme == "https" or forwarded_proto == "https"),
        )
        return {"ok": True, "password_required": True}

    # -- endpoints -----------------------------------------------------------

    @app.get("/api/search", dependencies=[protected])
    async def api_search(q: str, limit: int = 8, type: str = "songs"):
        if type not in ("songs", "albums", "videos"):
            raise HTTPException(status_code=422,
                                detail="type must be songs, albums, or videos")
        limit = max(1, min(limit, 20))
        # to_thread keeps the loop free; downloads run in their own pool,
        # so a search never waits behind one.
        search = {"albums": search_albums, "videos": search_videos}.get(
            type, search_songs)
        results = await asyncio.to_thread(search, q, limit)
        return {"type": type, "results": [asdict(r) for r in results]}

    @app.post("/api/grab", status_code=202, dependencies=[protected])
    async def api_grab(body: GrabRequest):
        if body.format and body.format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        if body.kind not in ("track", "album", "musicvideo"):
            raise HTTPException(status_code=422,
                                detail="kind must be track, album, or musicvideo")
        if not body.video_id.strip():
            raise HTTPException(status_code=422, detail="video_id is required")
        video_id = body.video_id.strip()
        if not body.force:
            duplicate = store.find_duplicate(video_id, body.kind)
            if duplicate is not None:
                active = duplicate["stage"] not in ("done",)
                raise HTTPException(status_code=409, detail={
                    "message": ("this %s is already being grabbed" % body.kind)
                               if active else
                               ("this %s was already grabbed" % body.kind),
                    "existing_job": {k: duplicate[k] for k in
                                     ("id", "title", "artist", "stage", "created_at")},
                })
        if body.with_video and body.kind != "track":
            # An album would mean a search per track, and most album
            # tracks have no video - the closest match would be wrong
            # more often than right.
            raise HTTPException(status_code=422,
                                detail="with_video is for single tracks only")
        job = manager.enqueue(video_id, body.format, body.bitrate, kind=body.kind,
                              with_video=body.with_video)
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

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[protected])
    async def api_cancel(job_id: str):
        job = manager.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.get("/api/settings", dependencies=[protected])
    async def api_get_settings():
        config = effective_config()
        return {
            "output_format": config.output_format,
            "bitrate": config.bitrate,
            "password_set": bool(config.password),
            "concurrency": config.concurrency,
            "workers_active": True,  # concurrency changes apply on restart
            "cookies_set": bool(config.cookies_file),
            "music_root": str(config.music_root),
            "music_root_locked": music_locked,  # env-controlled; hide field
            "lyrics": config.lyrics_enabled,
            "lyrics_provider": config.lyrics_provider,
            "musixmatch_token_set": bool(config.musixmatch_token),
            "apple_token_set": bool(config.apple_token),
            "plex_url": config.plex_url,
            # Like the Apple token, only whether one is set: a Plex token
            # is the whole account, and the page never needs to read it.
            "plex_token_set": bool(config.plex_token),
            "plex_music_path": config.plex_music_path,
            "plex_section": config.plex_section,
            "apple_storefront": config.apple_storefront,
            "word_lyrics": config.word_lyrics,
            "video_root": str(config.video_root),
            "video_root_locked": video_locked,  # env-controlled; hide field
            "video_max_height": config.video_max_height,
            "ytdlp_version": ytdlp_version(),  # read-only
        }

    @app.put("/api/settings", dependencies=[protected])
    async def api_put_settings(body: SettingsUpdate):
        if body.output_format is not None and body.output_format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        if body.concurrency is not None and not (1 <= body.concurrency <= 4):
            raise HTTPException(status_code=422, detail="concurrency must be 1-4")
        if body.music_root is not None and music_locked:
            raise HTTPException(
                status_code=422,
                detail="music library path is set by MUSIC_PATH and cannot be changed here")
        if body.video_root is not None and video_locked:
            raise HTTPException(
                status_code=422,
                detail="video library path is set by VIDEO_PATH and cannot be changed here")
        if body.video_max_height is not None and not (0 <= body.video_max_height <= 4320):
            raise HTTPException(status_code=422,
                                detail="video_max_height must be 0-4320")
        if body.cookies is not None:
            # Stored as a file because yt-dlp wants a cookiefile path;
            # kept out of the DB and readable only by the app user.
            if body.cookies.strip():
                uploaded_cookies.parent.mkdir(parents=True, exist_ok=True)
                uploaded_cookies.touch(mode=0o600, exist_ok=True)
                uploaded_cookies.write_text(body.cookies)
            elif uploaded_cookies.exists():
                uploaded_cookies.unlink()
        if body.lyrics_provider is not None and body.lyrics_provider not in LYRICS_PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail="lyrics_provider must be one of %s" % (LYRICS_PROVIDERS,))
        updates = {k: v for k, v in body.model_dump().items()
                   if v is not None and k not in ("cookies", "musixmatch_token",
                                                  "apple_token", "word_lyrics",
                                                  "plex_token")}
        if body.word_lyrics is not None:
            updates["word_lyrics"] = "1" if body.word_lyrics else "0"
        if body.apple_token is not None:
            updates["apple_token"] = body.apple_token.strip()
        if body.plex_token is not None:
            updates["plex_token"] = body.plex_token.strip()
        if body.plex_url is not None:
            url = body.plex_url.strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=422,
                    detail="the Plex address needs http:// or https://, "
                           "e.g. http://192.168.1.10:32400")
            updates["plex_url"] = url
        if body.plex_music_path is not None:
            updates["plex_music_path"] = body.plex_music_path.strip()
        if body.plex_section is not None:
            updates["plex_section"] = body.plex_section.strip()
        if "lyrics" in updates:
            updates["lyrics"] = "1" if updates["lyrics"] else "0"
        if body.musixmatch_token is not None:
            updates["mxm_token"] = body.musixmatch_token.strip()
        if updates.get("password"):
            # Hashed at rest; changing it also invalidates every session,
            # since the hash is part of the token signing key.
            updates["password"] = hash_password(updates["password"])
        store.set_settings(updates)
        return await api_get_settings()

    @app.get("/api/health")
    async def api_health():
        config = effective_config()
        problem = storage_problem(config.music_root, config.min_free_mb)
        return {
            "status": "ok" if not problem else "degraded",
            "library": str(config.music_root),
            "library_writable": not problem,
            "library_problem": problem,
            "library_free_mb": storage_free_mb(config.music_root),
            "min_free_mb": config.min_free_mb,
            "ytdlp_version": ytdlp_version(),
            "version": __version__,
            "active_jobs": manager.active_count(),
            # A wave of these usually means yt-dlp needs updating.
            "failures_last_hour": store.count_failed_since(3600),
        }

    @app.post("/api/ytdlp/update", dependencies=[protected])
    async def api_ytdlp_update():
        """Update yt-dlp into the config volume and hot-swap the loaded
        module - active immediately, no restart, and it persists across
        restarts. The server runs unprivileged, so the system copy is
        not touched."""
        try:
            result = await asyncio.to_thread(
                updater.update_and_reload, base.config_dir)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="update failed: %s" % exc)
        return {
            "loaded_version": result["old"],
            "installed_version": result["new"],
            "active": True,
            "restart_needed": False,
        }

    @app.post("/api/lyrics/musixmatch-token", dependencies=[protected])
    async def api_musixmatch_token():
        """Fetch a fresh Musixmatch usertoken and store it - the
        synced-lyrics fallback then works immediately and persists.
        Same shape as the yt-dlp update button."""
        try:
            token = await asyncio.to_thread(musixmatch.fetch_token)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        store.set_settings({"mxm_token": token})
        return {"ok": True, "token_set": True}

    # Sign-in flows live in memory only, between the sign-in call and the
    # 2FA call. The Apple ID password is never put in here: it is consumed
    # inside login() and dropped.
    apple_flows: dict = {}
    APPLE_FLOW_TTL = 600

    def _sweep_apple_flows() -> None:
        now = time.time()
        for key, (_, started) in list(apple_flows.items()):
            if now - started > APPLE_FLOW_TTL:
                apple_flows.pop(key, None)

    def _store_apple_token(flow) -> dict:
        """Mint and save the media-user-token for a signed-in flow."""
        try:
            developer_token = apple.fetch_developer_token(force=True)
        except apple.AppleError as exc:
            return {"status": "error", "detail": str(exc)}
        token = flow.mint_media_user_token(developer_token)
        if not token:
            return {"status": "error",
                    "detail": flow.error or "could not mint a media-user-token"}
        store.set_settings({"apple_token": token})
        flow.save_session()
        return {"status": "ok", "token_set": True,
                "detail": "Signed in - Apple Music lyrics are ready"}

    @app.post("/api/apple/signin", dependencies=[protected])
    async def api_apple_signin(body: AppleSignInRequest):
        """Sign in to Apple and mint a media-user-token.

        The password is used for the SRP exchange and then dropped: it is
        never written to disk, never logged, and SRP does not send it to
        Apple either. Only the session cookies persist (0600), which is
        what allows a later re-mint without signing in again.
        """
        _sweep_apple_flows()
        flow = applesignin.AppleSignIn(base.config_dir)
        status = await asyncio.to_thread(
            flow.login, body.apple_id.strip(), body.password)
        if status == applesignin.STATUS_NEEDS_2FA:
            flow_id = secrets_token()
            apple_flows[flow_id] = (flow, time.time())
            return {"status": "needs_2fa", "flow_id": flow_id,
                    "detail": "Apple sent a verification code"}
        if status != applesignin.STATUS_OK:
            raise HTTPException(status_code=401,
                                detail=flow.error or "Apple sign-in failed")
        return await asyncio.to_thread(_store_apple_token, flow)

    @app.post("/api/apple/verify", dependencies=[protected])
    async def api_apple_verify(body: AppleVerifyRequest):
        _sweep_apple_flows()
        entry = apple_flows.get(body.flow_id)
        if entry is None:
            raise HTTPException(
                status_code=410,
                detail="that sign-in expired - start again")
        flow = entry[0]
        status = await asyncio.to_thread(flow.submit_code, body.code.strip())
        if status != applesignin.STATUS_OK:
            raise HTTPException(status_code=401,
                                detail=flow.error or "verification failed")
        apple_flows.pop(body.flow_id, None)
        return await asyncio.to_thread(_store_apple_token, flow)

    @app.post("/api/apple/signout", dependencies=[protected])
    async def api_apple_signout():
        applesignin.AppleSignIn(base.config_dir).clear_session()
        store.set_settings({"apple_token": ""})
        return {"ok": True, "detail": "Apple session and token cleared"}

    from .plexauth import SignIn, new_client_id
    plex_signin = SignIn()

    def plex_client_id() -> str:
        """This install's identity to plex.tv, made once and kept, so the
        account's Authorized Devices shows one Beetdrop rather than a new
        one for every sign-in."""
        found = store.get_settings().get("plex_client_id")
        if not found:
            found = new_client_id()
            store.set_settings({"plex_client_id": found})
        return found

    def _plex_failed(exc: Exception):
        from .plex import PlexError
        if isinstance(exc, PlexError):
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        raise exc

    @app.post("/api/plex/pin", dependencies=[protected])
    async def api_plex_pin():
        """Start a Plex sign-in: a code to enter at plex.tv/link."""
        try:
            return await asyncio.to_thread(plex_signin.start, plex_client_id())
        except Exception as exc:
            _plex_failed(exc)

    @app.get("/api/plex/pin/{pin_id}", dependencies=[protected])
    async def api_plex_pin_poll(pin_id: str):
        """Polled by the page until the code has been entered. Then it
        lists the account's servers - never the token."""
        try:
            return await asyncio.to_thread(plex_signin.poll, pin_id, plex_client_id())
        except Exception as exc:
            _plex_failed(exc)

    @app.post("/api/plex/pin/{pin_id}/server", dependencies=[protected])
    async def api_plex_pin_server(pin_id: str, body: PlexServerChoice):
        """Settle on a server: find an address that answers from here,
        save it with the server's own token, and say where scans will go."""
        from . import plex
        try:
            chosen = await asyncio.to_thread(
                plex_signin.choose, pin_id, body.server_id, plex_client_id())
        except Exception as exc:
            _plex_failed(exc)
        store.set_settings({"plex_url": chosen["url"], "plex_token": chosen["token"],
                            "plex_section": ""})
        result = await asyncio.to_thread(plex.check, effective_config())
        result["url"] = chosen["url"]
        result["server"] = chosen["name"]
        return result

    @app.post("/api/plex/sign-out", dependencies=[protected])
    async def api_plex_sign_out():
        """Forget the server and its token. Grabs stop asking Plex to
        scan; where Plex sees the music folder is kept, since it is still
        true if the same server is chosen again."""
        store.set_settings({"plex_url": "", "plex_token": "", "plex_section": ""})
        return {"ok": True}

    @app.post("/api/plex/test", dependencies=[protected])
    async def api_plex_test():
        """Can Plex be reached, and which library will grabs be scanned
        into. Lists Plex's own folder paths, which is the quickest way to
        see what "where Plex sees the music folder" should say."""
        from . import plex
        return await asyncio.to_thread(plex.check, effective_config())

    @app.post("/api/lyrics/apple-test", dependencies=[protected])
    async def api_apple_test():
        """Check the stored Apple media-user-token still works. It is
        long-lived but not permanent, and an expired one otherwise shows
        up only as Apple silently never returning lyrics again."""
        config = effective_config()
        result = await asyncio.to_thread(
            apple.check_token, config.apple_token, config.apple_storefront)
        return result

    @app.get("/api/lyrics/stats", dependencies=[protected])
    async def api_lyrics_stats():
        """How much of the library has lyrics, and how much is word-level.
        Read-only: it walks the library and reads sidecars, nothing else."""
        from dataclasses import asdict

        from .backfill import lyrics_stats
        config = effective_config()
        stats = await asyncio.to_thread(lyrics_stats, config.music_root)
        body = asdict(stats)
        body["coverage_pct"] = round(stats.coverage_pct, 1)
        body["word_pct"] = round(stats.word_pct, 1)
        return body

    @app.get("/api/lyrics/reviews", dependencies=[protected])
    async def api_lyrics_reviews():
        """Tracks where Apple offered candidates and every one was refused.

        Only these: a track Apple returned nothing for has nothing to
        choose between, and listing it would be clicking through blanks.
        """
        return {"reviews": store.list_reviews(), "total": store.count_reviews()}

    @app.post("/api/lyrics/reviews", dependencies=[protected])
    async def api_lyrics_choose(body: ReviewChoice):
        """Record a decision and act on it straight away.

        Remembered, so a later scan uses it rather than matching afresh -
        and so correcting a wrong match sticks.
        """
        config = effective_config()
        # The path travels in the body, not the URL: a library path is
        # full of slashes and spaces, and routing on it turned every
        # decision into an encoding problem.
        track = Path(body.path)
        store.set_choice(str(track), body.song_id)
        store.drop_review(str(track))
        if not body.song_id:
            return {"ok": True, "detail": "left alone"}
        if not track.is_file():
            raise HTTPException(status_code=404, detail="that file is gone")
        # The decision is already saved, so a fetch that fails now is a
        # delay rather than a loss: say so instead of reporting an error
        # over a choice that was in fact recorded.
        try:
            from .backfill import _lyrics_by_choice
            lrc = await asyncio.to_thread(
                _lyrics_by_choice, config, body.song_id, config.word_lyrics)
        except Exception as exc:
            return {"ok": True, "detail":
                    "saved, but Apple could not be reached just now (%s) - "
                    "the next scan will use your pick" % str(exc)[:120]}
        if not lrc:
            return {"ok": True, "detail":
                    "saved, but Apple has no lyrics for that one"}
        from .library import write_lyrics_sidecar
        await asyncio.to_thread(write_lyrics_sidecar, track, lrc, True)
        return {"ok": True, "detail": "lyrics written"}

    @app.delete("/api/lyrics/reviews", dependencies=[protected])
    async def api_lyrics_clear_reviews():
        return {"removed": store.clear_reviews()}

    @app.get("/api/lyrics/unmatched", dependencies=[protected])
    async def api_lyrics_unmatched(limit: int = 50, offset: int = 0):
        """Tracks with no .lrc at all, for searching by hand.

        The review queue only holds tracks Apple offered something for.
        A track it returned nothing for has nothing to choose between and
        is deliberately left out - which also left it with no way for a
        person to intervene at all. This is that way.
        """
        config = effective_config()
        from .backfill import (iter_audio_missing_lyrics, meta_from_path,
                               read_track_meta, tidy_track_name)

        def collect():
            found, seen = [], 0
            for path in iter_audio_missing_lyrics(config.music_root):
                seen += 1
                if seen <= offset:
                    continue
                if len(found) >= max(1, min(limit, 200)):
                    # Keep counting so the total is honest, but stop
                    # reading tags - that is what costs.
                    continue
                artist, title, _album, duration = (
                    read_track_meta(path) or ("", "", "", None))
                if not artist or not title:
                    p_artist, p_title, _ = meta_from_path(path, config.music_root)
                    artist, title = artist or p_artist, title or p_title
                found.append({
                    "path": str(path),
                    "name": path.name,
                    "artist": artist,
                    "title": tidy_track_name(title, artist),
                    "duration": int(duration or 0),
                })
            return found, seen

        tracks, total = await asyncio.to_thread(collect)
        return {"tracks": tracks, "total": total,
                "offset": offset, "limit": limit}

    @app.get("/api/lyrics/search", dependencies=[protected])
    async def api_lyrics_search(q: str, limit: int = 10):
        """Free-text Apple search, unfiltered, for picking by hand.

        No relevance check: the person searching has already decided to
        look, and a track whose tags defeated matching is exactly the one
        where matching's opinion is worth least.
        """
        config = effective_config()
        if not q.strip():
            return {"results": []}

        def run():
            developer = apple.fetch_developer_token()
            rows = apple.search_catalog(
                developer, config.apple_storefront or "us", q,
                limit=max(1, min(limit, 25)))
            return [apple.describe_song(row) for row in rows]

        try:
            return {"results": await asyncio.to_thread(run)}
        except apple.AppleError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def _lyrics_for(song_id: str, word: bool):
        config = effective_config()
        lrc = apple.lyrics_for_song(
            apple.fetch_developer_token(), config.apple_token,
            config.apple_storefront or "us", song_id, word_by_word=word)
        # Tagged even though this one is downloaded rather than filed:
        # the file often ends up in a library later, and a sidecar that
        # cannot say where it came from is the problem being fixed.
        return lyrics.stamp_source(lrc, "apple") if lrc else lrc

    @app.get("/api/lyrics/preview", dependencies=[protected])
    async def api_lyrics_preview(song_id: str, word: bool = False):
        """The LRC for one catalog id, to read before saving it.

        No library track involved: this is for looking a song up and
        taking the file, which is a different job from filling a sidecar
        in and needs no matching at all.
        """
        if not song_id.strip():
            raise HTTPException(status_code=400, detail="no song chosen")
        try:
            lrc = await asyncio.to_thread(_lyrics_for, song_id.strip(), word)
        except apple.AppleError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not lrc:
            # Asking for words and getting none is not this: a track Apple
            # has only line timing for still comes back, rendered
            # line-level. This is Apple having no lyrics at all.
            raise HTTPException(status_code=404,
                                detail="Apple has no lyrics for that one")
        from .lyrics import has_word_timing, strip_source
        # word_level says what actually came back, which is not always
        # what was asked for - the page tells the person which they got.
        # The count is of the lyrics; the provenance header is part of
        # the file but is not a line of the song.
        return {"lrc": lrc, "word_level": has_word_timing(lrc),
                "lines": len(strip_source(lrc).splitlines())}

    @app.get("/api/lyrics/download", dependencies=[protected])
    async def api_lyrics_download(song_id: str, word: bool = False,
                                  name: str = ""):
        """The same LRC as a file the browser will save.

        Served as an attachment rather than handed to the page to turn
        into a blob: a download started by script is what mobile browsers
        are least reliable about, and this is a phone-first app.
        """
        if not song_id.strip():
            raise HTTPException(status_code=400, detail="no song chosen")
        try:
            lrc = await asyncio.to_thread(_lyrics_for, song_id.strip(), word)
        except apple.AppleError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not lrc:
            raise HTTPException(status_code=404, detail="no lyrics for that one")
        # The name comes from the page and ends up in a header, so it is
        # reduced to something that cannot carry a newline or a quote out.
        safe = re.sub(r"[^\w \-.()&',]+", "_", name or "lyrics").strip(" .")[:120]
        if not safe.lower().endswith(".lrc"):
            safe += ".lrc"
        return Response(
            content=lrc, media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="%s"' % safe})

    @app.get("/api/library", dependencies=[protected])
    async def api_library(view: str = "albums", filter: str = "",
                          sort: str = "added", q: str = "",
                          page: int = 1, per_page: int = 60):
        """What is on disk, as albums.

        Walked on demand rather than cached: at a real library size the
        walk plus a read of every sidecar is well under a second, and a
        second copy of the truth is a thing that can be wrong.
        """
        from .libraryview import matches_filter, scan_albums, sort_albums

        config = effective_config()

        def collect():
            albums = scan_albums(config.music_root)
            if view == "review":
                albums = [a for a in albums if not a.verified]
            needle = q.strip().lower()
            if needle:
                albums = [a for a in albums
                          if needle in ("%s %s" % (a.artist, a.album)).lower()]
            albums = [a for a in albums if matches_filter(a, filter)]
            albums = sort_albums(albums, sort)
            size = max(1, min(per_page, 200))
            start = max(0, (max(1, page) - 1) * size)
            window = albums[start:start + size]
            return len(albums), [dict(vars(a), incomplete=a.incomplete)
                                 for a in window]

        total, items = await asyncio.to_thread(collect)
        return {"total": total, "page": page, "items": items}

    @app.get("/api/library/counts", dependencies=[protected])
    async def api_library_counts():
        """How many albums each filter chip would show.

        The chips are the maintenance lists that used to live inside
        Settings; a chip with no count is a chip nobody presses.
        """
        from .libraryview import FILTERS, matches_filter, scan_albums

        config = effective_config()

        def collect():
            albums = scan_albums(config.music_root)
            counts = {name: sum(1 for a in albums if matches_filter(a, name))
                      for name in FILTERS}
            counts["all"] = len(albums)
            formats = {}
            for album in albums:
                formats[album.format] = formats.get(album.format, 0) + 1
            return counts, formats

        counts, formats = await asyncio.to_thread(collect)
        return {"counts": counts, "formats": formats}

    @app.get("/api/library/album/{ident}", dependencies=[protected])
    async def api_library_album(ident: str):
        """One album's tracks, each with its own lyrics state."""
        from .libraryview import album_path, album_tracks

        config = effective_config()
        folder = album_path(config.music_root, ident)
        if folder is None or not folder.is_dir():
            raise HTTPException(status_code=404, detail="no such album")
        tracks = await asyncio.to_thread(album_tracks, folder)
        return {"path": str(folder), "tracks": tracks}

    @app.get("/api/library/album/{ident}/cover", dependencies=[protected])
    async def api_library_album_cover(ident: str):
        """The cover.jpg in an album's folder.

        Served rather than linked. The file sits inside the library
        mount, which the browser has no path to, so Library rows had
        nothing to point an <img> at and always drew the placeholder.

        Only the three names filing writes are served, and the id is
        resolved through album_path, which refuses anything outside the
        library - the ident arrives over HTTP.
        """
        from .libraryview import COVER_NAMES, album_path

        config = effective_config()
        folder = album_path(config.music_root, ident)
        if folder is None or not folder.is_dir():
            raise HTTPException(status_code=404, detail="no such album")
        for name in COVER_NAMES:
            candidate = folder / name
            if candidate.is_file():
                return FileResponse(
                    candidate,
                    media_type="image/png" if name.endswith(".png")
                    else "image/jpeg",
                    # The page puts the album's mtime in the query, so a
                    # replaced cover arrives under a different URL and
                    # this can be cached hard. Private: a library's
                    # artwork is not for a shared proxy to keep.
                    headers={"Cache-Control": "private, max-age=604800"})
        raise HTTPException(status_code=404, detail="no cover")

    @app.get("/api/stats", dependencies=[protected])
    async def api_stats():
        """Library health, in one read of the tree plus the jobs table."""
        from .libraryview import (count_companion_videos, count_videos,
                                  grab_history, scan_albums, summarise)

        config = effective_config()

        def collect():
            albums = scan_albums(config.music_root)
            totals = summarise(albums)
            totals["videos"] = count_videos(config.video_root)
            # Videos filed beside their tracks for Plex, counted apart so
            # the card can say which kind is which.
            totals["companion_videos"] = count_companion_videos(
                config.music_root, config.video_root)
            totals["bytes_free"] = int(storage_free_mb(config.music_root) or 0) * 1024 * 1024
            # bad_timing and crowded come out of summarise now. They used
            # to need a second walk of the tree and a second read of
            # every .lrc, which parsed the same text this one already did.
            totals["reliability"] = grab_history(store.list_jobs(limit=1000))
            return totals

        stats = await asyncio.to_thread(collect)
        stats["scanned_at"] = time.time()
        return stats

    @app.get("/api/match/unverified", dependencies=[protected])
    async def api_match_unverified(limit: int = 100):
        """Grabs filed under _review/ - the ones nothing could verify.

        Every one has tags taken from YouTube rather than MusicBrainz,
        and nothing revisits them, so without this they stay wrong.
        """
        from .backfill import read_track_meta
        from .rematch import iter_unverified

        config = effective_config()

        def collect():
            found, seen = [], 0
            for path in iter_unverified(config.music_root):
                seen += 1
                if len(found) >= max(1, min(limit, 500)):
                    continue
                artist, title, album, duration = (
                    read_track_meta(path) or ("", "", "", None))
                found.append({"path": str(path), "name": path.name,
                              "artist": artist, "title": title,
                              "album": album, "duration": int(duration or 0)})
            return found, seen

        tracks, total = await asyncio.to_thread(collect)
        return {"tracks": tracks, "total": total}

    @app.get("/api/match/tracks", dependencies=[protected])
    async def api_match_tracks(q: str, limit: int = 50):
        """Any track in the library, found by what it is called now.

        _review/ only holds what matching knew it could not verify. A
        match that was confidently wrong is filed as verified and looks
        settled, so it has to be reachable by searching for the wrong
        name - which is the one showing in the player.
        """
        from .backfill import read_track_meta
        from .rematch import search_library

        config = effective_config()

        def collect():
            rows = []
            for path in search_library(config.music_root, q, limit=limit):
                artist, title, album, duration = (
                    read_track_meta(path) or ("", "", "", None))
                rows.append({"path": str(path), "name": path.name,
                             "artist": artist, "title": title, "album": album,
                             "duration": int(duration or 0)})
            return rows

        tracks = await asyncio.to_thread(collect)
        return {"tracks": tracks, "total": len(tracks)}

    @app.get("/api/match/candidates", dependencies=[protected])
    async def api_match_candidates(title: str, artist: str = "",
                                   limit: int = 10, album: str = ""):
        """What MusicBrainz has for this text, unscored.

        The automatic checks are what produced the wrong answer, so
        applying them again here would hide the right one.
        """
        from .grab import get_mb_client
        from .rematch import search_candidates

        if not title.strip():
            return {"candidates": []}
        config = effective_config()

        def run():
            return search_candidates(get_mb_client(config), title, artist,
                                     limit=max(1, min(limit, 25)), album=album)

        try:
            return {"candidates": await asyncio.to_thread(run)}
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail="MusicBrainz: %s" % exc) from exc

    @app.post("/api/match/apply", dependencies=[protected])
    async def api_match_apply(body: MatchChoice):
        """Re-tag a file as the chosen recording and file it properly."""
        from .grab import get_mb_client
        from .rematch import apply_choice

        config = effective_config()
        track = Path(body.path)
        # The path comes over HTTP, so it is held to the library.
        try:
            inside = track.resolve().is_relative_to(config.music_root.resolve())
        except OSError:
            inside = False
        if not inside:
            raise HTTPException(status_code=400,
                                detail="that path is not in the library")

        def run():
            return apply_choice(config, get_mb_client(config), track,
                                body.recording_id, body.title, body.artist,
                                move=body.move, album=body.album)

        try:
            outcome = await asyncio.to_thread(run)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        moved = outcome.new_path != outcome.old_path
        return {"ok": True, "path": str(outcome.new_path), "moved": moved,
                "moved_lyrics": outcome.moved_lyrics,
                "moved_video": outcome.moved_video,
                "detail": "%s as %s - %s" % (
                    "filed" if moved else "re-tagged in place",
                    outcome.tags.artist, outcome.tags.title)}

    @app.post("/api/lyrics/tag-sources", dependencies=[protected])
    async def api_lyrics_tag_sources():
        """Tag sidecars written before Beetdrop recorded a source.

        Offline and quick - it rewrites one header line and asks nothing
        of any provider - so it answers directly rather than going
        through the queue.

        It never guesses. Of the three sources only Apple has per-word
        timing, so a word-level sidecar Beetdrop wrote can only have come
        from Apple. A line-level one is left untagged: all three sources
        produce those and the file does not say which, and a plausible
        source written in would later be read as a fact. So is a file
        another tool converted - one that names its own writer, or has a
        .lrc.bak beside it from an in-place conversion.
        """
        from .backfill import tag_existing_sources

        config = effective_config()
        found = await asyncio.to_thread(tag_existing_sources, config.music_root)
        return {"total": found.total, "already": found.already,
                "tagged": found.tagged, "unknowable": found.unknowable,
                "foreign": found.foreign}

    @app.post("/api/lyrics/scan", status_code=202, dependencies=[protected])
    async def api_lyrics_scan(refresh: bool = False, upgrade: bool = False,
                              redo_words: bool = False):
        """Backfill synced lyrics for library tracks that have no .lrc yet.

        refresh=true first deletes placeholder sidecars (generated junk
        with perfectly uniform line timing, whatever wrote them) so those
        tracks get looked up fresh in the same run. Real lyrics, in any
        language, are never deleted.

        Runs as a normal cancellable job so the queue shows its progress.
        """
        marker = ("__rewords__" if redo_words else
                  "__upgrade__" if upgrade else
                  "__refresh__" if refresh else "__library__")
        for candidate in ("__library__", "__refresh__", "__upgrade__",
                          "__rewords__"):
            existing = store.find_duplicate(candidate, "lyricscan")
            if existing is not None and existing["stage"] not in (
                    "done", "failed", "cancelled"):
                raise HTTPException(
                    status_code=409,
                    detail={"message": "a library lyrics scan is already running",
                            "existing_job": {k: existing[k] for k in
                                             ("id", "title", "stage", "created_at")}})
        job = manager.enqueue(marker, kind="lyricscan")
        # Label it up front so the queue card reads cleanly from the start.
        title = ("Library word-by-word re-render" if redo_words else
                 "Library lyrics refresh" if refresh else
                 "Library lyrics scan")
        return store.update_job(job["id"], title=title) or job

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

    # -- UI shell ------------------------------------------------------------
    # The shell is unauthenticated by design: the password guards the API,
    # and the page itself is what asks for the password. The service worker
    # must live at the root so its scope covers the whole app.
    #
    # Every shell asset is served Cache-Control: no-cache so browsers
    # revalidate (cheap 304s via ETag) instead of heuristically caching -
    # a stale app.js against a fresh index.html breaks the UI silently.

    NO_CACHE = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest",
                            media_type="application/manifest+json",
                            headers=NO_CACHE)

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                            headers=NO_CACHE)

    class RevalidatedStaticFiles(StaticFiles):
        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/static", RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")

    return app

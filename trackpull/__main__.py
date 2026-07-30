"""CLI.

    python -m trackpull search "query"
    python -m trackpull grab <video_id> [--format opus|m4a|mp3]
    python -m trackpull serve [--host 0.0.0.0] [--port 8090]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import SUPPORTED_FORMATS, Config, InboxError, check_inbox
from .download import DownloadError, ytdlp_version
from .grab import run_album_grab, run_grab
from .search import search_albums, search_songs


def _format_duration(seconds) -> str:
    if seconds is None:
        return "?:??"
    return "%d:%02d" % divmod(int(seconds), 60)


def cmd_search(args, config: Config) -> int:
    if args.albums:
        albums = search_albums(args.query, limit=args.limit)
        if not albums:
            print("no results")
            return 1
        for album in albums:
            print("%s  %s - %s (%s%s)" % (
                album.browse_id,
                album.artist_display or "?",
                album.title,
                album.year or "?",
                (", " + album.album_type) if album.album_type else "",
            ))
        return 0
    results = search_songs(args.query, limit=args.limit)
    if not results:
        print("no results")
        return 1
    for result in results:
        album = result.album or "no album"
        print("%s  %-6s %s - %s [%s]" % (
            result.video_id,
            _format_duration(result.duration_seconds),
            result.artist_display or "?",
            result.title,
            album,
        ))
    return 0


def cmd_grab(args, config: Config) -> int:
    try:
        check_inbox(config.inbox, config.min_free_mb)
        if args.album:
            outcome = run_album_grab(
                args.video_id, config,
                fmt=args.format or "", bitrate=args.bitrate or "",
                on_stage=lambda stage: print("stage: %s" % stage),
                on_resolved=lambda title, artist: print("resolved: %s - %s" % (artist, title)),
                on_detail=lambda text: print(text),
            )
            if outcome.failed:
                for failure in outcome.failed:
                    print("failed: %s" % failure, file=sys.stderr)
            print("done: %d tracks handed off to %s" % (outcome.delivered, outcome.inbox_path))
            return 0
        outcome = run_grab(
            args.video_id, config,
            fmt=args.format or "", bitrate=args.bitrate or "",
            on_stage=lambda stage: print("stage: %s" % stage),
            on_resolved=lambda r: print("resolved: %s - %s (%ss)" % (
                r.artist_display or "?", r.title, r.duration_seconds)),
        )
    except (DownloadError, ValueError, InboxError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("done: handed off to %s" % outcome.inbox_path)
    return 0


def cmd_serve(args, config: Config) -> int:
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_version(args, config: Config) -> int:
    from . import __version__
    print("trackpull %s (yt-dlp %s)" % (__version__, ytdlp_version()))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trackpull")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="search YouTube Music songs or albums")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--albums", action="store_true", help="search albums instead of songs")
    p_search.set_defaults(func=cmd_search)

    p_grab = sub.add_parser("grab", help="download one video or album and hand it to the inbox")
    p_grab.add_argument("video_id", help="videoId, or an album browseId with --album")
    p_grab.add_argument("--album", action="store_true", help="treat the id as an album browseId")
    p_grab.add_argument("--format", choices=SUPPORTED_FORMATS, help="output format (default opus)")
    p_grab.add_argument("--bitrate", help="bitrate for mp3 transcodes")
    p_grab.add_argument("--inbox", help="inbox path (overrides INBOX_PATH)")
    p_grab.set_defaults(func=cmd_grab)

    p_serve = sub.add_parser("serve", help="run the web API")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8090)
    p_serve.set_defaults(func=cmd_serve)

    p_version = sub.add_parser("version", help="show trackpull and yt-dlp versions")
    p_version.set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    config = Config()
    if getattr(args, "inbox", None):
        config.inbox = Path(args.inbox).expanduser()
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())

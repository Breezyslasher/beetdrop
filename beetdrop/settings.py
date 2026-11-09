"""Merging the settings saved through the UI onto the environment config.

This lives apart from app.py because the CLI needs it too. It used to be
a closure inside create_app, which meant `python -m beetdrop` saw only
environment variables: the Apple and Musixmatch tokens, the provider
choice and the word-by-word setting are all stored in SQLite by the
Settings page, so every CLI lyrics command ran as though none of them
were configured. scan-lyrics --upgrade in particular became a silent
no-op - it asks Apple and nothing else, and without a token it never
asked anything.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Optional

from .config import Config
from .lyrics import PROVIDERS as LYRICS_PROVIDERS


def apply_stored_settings(base: Config, stored: dict,
                          music_locked: Optional[bool] = None,
                          video_locked: Optional[bool] = None) -> Config:
    """A copy of `base` with the stored settings layered on top.

    MUSIC_PATH/VIDEO_PATH in the environment pin the library to a
    container mount, so a stored override of those is ignored: pointing
    them at an unmounted host path only breaks writes. The caller may
    state those locks explicitly; by default they follow the environment.
    """
    if music_locked is None:
        music_locked = "MUSIC_PATH" in os.environ
    if video_locked is None:
        video_locked = "VIDEO_PATH" in os.environ
    config = replace(base)
    if stored.get("output_format"):
        config.output_format = stored["output_format"]
    if stored.get("bitrate"):
        config.bitrate = stored["bitrate"]
    if stored.get("password"):
        config.password = stored["password"]
    if stored.get("concurrency"):
        try:
            config.concurrency = int(stored["concurrency"])
        except ValueError:
            pass
    if stored.get("music_root") and not music_locked:
        config.music_root = Path(stored["music_root"])
    if stored.get("lyrics") in ("0", "1"):
        config.lyrics_enabled = stored["lyrics"] == "1"
    if stored.get("mxm_token"):
        config.musixmatch_token = stored["mxm_token"]
    if stored.get("lyrics_provider") in LYRICS_PROVIDERS:
        config.lyrics_provider = stored["lyrics_provider"]
    if stored.get("apple_token"):
        config.apple_token = stored["apple_token"]
    if stored.get("apple_storefront"):
        config.apple_storefront = stored["apple_storefront"]
    if stored.get("word_lyrics") in ("0", "1"):
        config.word_lyrics = stored["word_lyrics"] == "1"
    if stored.get("plex_url"):
        config.plex_url = stored["plex_url"]
    if stored.get("plex_token"):
        config.plex_token = stored["plex_token"]
    if "plex_music_path" in stored:
        # Clearable: blank means Plex sees the library where we do.
        config.plex_music_path = stored["plex_music_path"]
    if "plex_section" in stored:
        # Clearable: blank goes back to every library holding the folder.
        config.plex_section = stored["plex_section"]
    if stored.get("video_root") and not video_locked:
        config.video_root = Path(stored["video_root"])
    if stored.get("video_max_height"):
        try:
            config.video_max_height = int(stored["video_max_height"])
        except ValueError:
            pass
    return config


def config_with_settings(base: Config) -> Config:
    """`base` plus whatever the Settings page has saved, for the CLI.

    Read-only and best effort: with no database yet (a fresh install, or
    a config dir the CLI cannot read) the environment config stands.
    """
    try:
        from .db import Store
        return apply_stored_settings(base, Store(base.db_path).get_settings())
    except Exception:
        return base

#!/bin/sh
# Adjust the runtime user to PUID/PGID, then drop privileges. The inbox is
# shared with beets-flask and the host, so the effective UID must match
# whatever owns that share.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

groupmod -o -g "$PGID" beetdrop
usermod -o -u "$PUID" -g "$PGID" beetdrop

chown beetdrop:beetdrop /config

if [ "$BEETDROP_SELFUPDATE" = "1" ]; then
    echo "BEETDROP_SELFUPDATE=1: updating yt-dlp"
    pip install --no-cache-dir --upgrade yt-dlp || echo "yt-dlp self-update failed; continuing with the installed version"
fi

exec gosu beetdrop python3 -m beetdrop serve --host 0.0.0.0 --port 8090

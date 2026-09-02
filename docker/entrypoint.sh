#!/bin/sh
# Adjust the runtime user to PUID/PGID, then drop privileges. The music
# library is shared with the host (and whatever serves it), so the
# effective UID must match its owner.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

groupmod -o -g "$PGID" beetdrop
usermod -o -u "$PUID" -g "$PGID" beetdrop

chown beetdrop:beetdrop /config

exec gosu beetdrop python3 -m beetdrop serve --host 0.0.0.0 --port 8090

"""Ask the real MusicBrainz for one song every way Beetdrop does, and a
few ways it doesn't, and print what comes back - to see why Fix match
finds nothing for a song that is on MusicBrainz.

Run by .github/workflows/mb-probe.yml, since MusicBrainz is reachable
from GitHub but not from every machine this is developed on.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from beetdrop.matching import credit_name  # noqa: E402
from beetdrop.mb import MB_ROOT, USER_AGENT, MusicBrainzClient  # noqa: E402
from beetdrop.rematch import find_candidates  # noqa: E402

TITLE = os.environ.get("PROBE_TITLE") or "We Are the Zorg"
ARTIST = os.environ.get("PROBE_ARTIST") or "Kyle Gordon"
ALBUM = os.environ.get("PROBE_ALBUM") or "Kyle Gordon Is Everywhere"


def raw(path):
    time.sleep(1.2)
    url = "%s/%s" % (MB_ROOT, path)
    response = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    print("    GET %s -> %d" % (url, response.status_code))
    return response.json() if response.ok else {}


def show_recordings(rows, limit=10):
    if not rows:
        print("    (nothing)")
    for row in rows[:limit]:
        releases = row.get("releases") or []
        print("    - %r by %r, score %s, %d release(s): %s" % (
            row.get("title"), credit_name(row.get("artist-credit")), row.get("score"),
            len(releases), "; ".join(sorted({r.get("title", "?") for r in releases}))[:200]))


def query(text, limit=10):
    return raw("recording?query=%s&limit=%d&fmt=json" % (quote(text), limit))


print("Probing for %r by %r on %r\n" % (TITLE, ARTIST, ALBUM))

print("1. Beetdrop's own Fix match (find_candidates), fresh cache:")
with tempfile.TemporaryDirectory() as tmp:
    mb = MusicBrainzClient(Path(tmp) / "cache.db")
    for title, artist, album in ((TITLE, ARTIST, ALBUM), (TITLE, ARTIST, ""),
                                 (TITLE, "", ""), (TITLE.lower(), ARTIST.lower(), "")):
        rows, note = find_candidates(mb, title, artist, album=album)
        print("  title=%r artist=%r album=%r -> %d, note=%r" % (
            title, artist, album, len(rows), note))
        for row in rows[:5]:
            print("    - %s / %s / %s (%s) %ss" % (
                row["title"], row["artist"], row["album"], row["year"], row["duration"]))

print("\n2. Raw recording searches:")
for text in ('recording:"%s" AND artist:"%s"' % (TITLE, ARTIST),
             'recording:"%s"' % TITLE,
             "%s %s" % (TITLE, ARTIST),
             TITLE,
             'artist:"%s"' % ARTIST):
    print("  %s" % text)
    show_recordings(query(text).get("recordings", []))

print("\n3. The album:")
for text in ('release:"%s"' % ALBUM, 'release:"%s" AND artist:"%s"' % (ALBUM, ARTIST),
             "%s %s" % (ALBUM, ARTIST)):
    print("  %s" % text)
    data = raw("release?query=%s&limit=10&fmt=json" % quote(text))
    for rel in (data.get("releases") or [])[:10] or [{}]:
        if not rel:
            print("    (nothing)")
            continue
        print("    - %r by %r, %s, %s, id %s" % (
            rel.get("title"), credit_name(rel.get("artist-credit")), rel.get("date"),
            rel.get("status"), rel.get("id")))

print("\n4. The artist, and what is filed under them:")
artists = raw("artist?query=%s&limit=5&fmt=json" % quote('artist:"%s"' % ARTIST)).get("artists", [])
for artist in artists[:5]:
    print("  - %r (%s) score %s id %s" % (artist.get("name"), artist.get("disambiguation", ""),
                                          artist.get("score"), artist.get("id")))
if artists:
    top = artists[0]["id"]
    groups = raw("release-group?artist=%s&limit=100&fmt=json" % top).get("release-groups", [])
    print("  release groups of %s:" % artists[0].get("name"))
    for group in groups:
        print("    - %r (%s, %s) id %s" % (group.get("title"), group.get("primary-type"),
                                          group.get("first-release-date"), group.get("id")))
    recs = raw("recording?artist=%s&limit=100&fmt=json" % top).get("recordings", [])
    zorg = [r for r in recs if TITLE.split()[-1].lower() in (r.get("title") or "").lower()]
    print("  recordings browsed by artist (%d), matching %r:" % (len(recs), TITLE.split()[-1]))
    show_recordings(zorg)
    releases = raw("release?artist=%s&limit=100&fmt=json" % top).get("releases", [])
    for rel in releases:
        if ALBUM.split()[-1].lower() in (rel.get("title") or "").lower():
            print("  release %r id %s - its tracks:" % (rel.get("title"), rel.get("id")))
            full = raw("release/%s?inc=recordings&fmt=json" % rel["id"])
            for medium in full.get("media") or []:
                for track in medium.get("tracks") or []:
                    print("    %s. %r (recording %s)" % (
                        track.get("number"), track.get("title"),
                        (track.get("recording") or {}).get("id")))

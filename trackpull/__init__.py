"""Trackpull: search for a song, download the audio from YouTube Music, and
drop it into a beets-flask inbox folder so beets can tag and file it.

The line is the inbox. Upstream of it is Trackpull. Downstream of it is
beets. No MusicBrainz lookups, no release disambiguation, no cover art, no
library paths — beets does every one of those things better.
"""

__version__ = "0.1.0"
APP_NAME = "trackpull"

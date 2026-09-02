"""MusicBrainz matching for library mode. Pure functions.

Duration is the strongest signal: it separates the studio cut from a
live version, extended mix, or a video with an intro. Artist and title
compare after aggressive normalisation. MusicBrainz's own relevance
score is a weak tiebreak only.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from .cleaning import clean_title

DURATION_MAX = 40
ARTIST_MAX = 30
TITLE_MAX = 25
EXT_MAX = 5
MATCH_THRESHOLD = 60
DURATION_REJECT_SECONDS = 8

# Suffixes that change WHAT the recording is; present on exactly one
# side means a different recording, not a fuzzy near-match.
_SIGNIFICANT_RE = re.compile(
    r"\b(live|acoustic|remix|instrumental|demo|karaoke|cover|extended|edit|version|session)\b")
_PAREN_RE = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_artist(name: str) -> str:
    norm = normalize(name)
    if norm.startswith("the ") and len(norm) > 4:
        norm = norm[4:]
    return norm


def base_title(title: str) -> str:
    """Cleaned of noise brackets, then stripped of remaining brackets for
    the base comparison."""
    cleaned = clean_title(title)
    return normalize(_PAREN_RE.sub(" ", cleaned))


def significant_qualifiers(title: str) -> frozenset:
    """Qualifier words that mark a different recording (live, remix...)."""
    return frozenset(_SIGNIFICANT_RE.findall(normalize(clean_title(title))))


@dataclass
class MatchScore:
    total: float = 0.0
    rejected: bool = False
    reason: str = ""

    @property
    def matched(self) -> bool:
        return not self.rejected and self.total >= MATCH_THRESHOLD


def duration_score(yt_seconds: Optional[int], mb_length_ms) -> tuple:
    if not yt_seconds or not mb_length_ms:
        return DURATION_MAX * 0.25, False  # cannot confirm; never reject
    diff = abs(yt_seconds - int(mb_length_ms) / 1000.0)
    if diff > DURATION_REJECT_SECONDS:
        return 0.0, True
    if diff <= 3:
        return DURATION_MAX - 2.0 * diff, False
    knee = DURATION_MAX - 6.0
    return max(0.0, knee - (knee - 4.0) * (diff - 3) / (DURATION_REJECT_SECONDS - 3)), False


def credit_name(artist_credit: list) -> str:
    """Display artist from an MB artist-credit array, joinphrases intact."""
    parts = []
    for credit in artist_credit or []:
        if isinstance(credit, str):
            parts.append(credit)
        elif isinstance(credit, dict):
            parts.append(credit.get("name") or (credit.get("artist") or {}).get("name") or "")
            parts.append(credit.get("joinphrase") or "")
    return "".join(parts).strip()


def credit_ids(artist_credit: list) -> list[str]:
    return [c["artist"]["id"] for c in artist_credit or []
            if isinstance(c, dict) and c.get("artist", {}).get("id")]


def score_recording(yt_title: str, yt_artist: str, yt_duration: Optional[int],
                    recording: dict) -> MatchScore:
    score = MatchScore()
    d_score, rejected = duration_score(yt_duration, recording.get("length"))
    if rejected:
        return MatchScore(rejected=True, reason="duration off by more than %ds"
                          % DURATION_REJECT_SECONDS)
    if significant_qualifiers(yt_title) != significant_qualifiers(recording.get("title", "")):
        return MatchScore(rejected=True, reason="live/remix qualifier mismatch")

    yt_base, mb_base = base_title(yt_title), base_title(recording.get("title", ""))
    title_ratio = _ratio(yt_base, mb_base) if yt_base and mb_base else 0.0
    if title_ratio < 0.5:
        return MatchScore(rejected=True, reason="title mismatch")
    t_score = TITLE_MAX if yt_base == mb_base else title_ratio * (TITLE_MAX - 5)

    yt_norm = normalize_artist(yt_artist)
    mb_norm = normalize_artist(credit_name(recording.get("artist-credit")))
    if yt_norm and yt_norm == mb_norm:
        a_score = ARTIST_MAX
    else:
        a_score = _ratio(yt_norm, mb_norm) * (ARTIST_MAX - 8)

    try:
        e_score = min(int(recording.get("score", 0)), 100) / 100.0 * EXT_MAX
    except (TypeError, ValueError):
        e_score = 0.0

    score.total = d_score + t_score + a_score + e_score
    return score


def best_recording(yt_title: str, yt_artist: str, yt_duration: Optional[int],
                   recordings: list[dict]) -> tuple:
    """(recording, MatchScore) for the best non-rejected candidate that
    clears the threshold, else (None, best score seen)."""
    best, best_score = None, None
    for recording in recordings:
        score = score_recording(yt_title, yt_artist, yt_duration, recording)
        if score.rejected:
            continue
        if best_score is None or score.total > best_score.total:
            best, best_score = recording, score
    if best_score is not None and best_score.matched:
        return best, best_score
    return None, best_score


# -- release selection (which album a recording files under) ---------------

_PREFERRED_TYPES = ("Album", "EP", "Single")
_DEPRIORITISED = {"Compilation", "Live", "Soundtrack", "DJ-mix"}


def _release_group(release: dict) -> dict:
    return release.get("release-group") or {}


def select_release(releases: list[dict]) -> Optional[dict]:
    """Prefer the original studio release: Official status, Album/EP/
    Single groups, no Compilation/Live secondary types, earliest
    first-release-date."""
    official = [r for r in releases
                if (r.get("status") or "").casefold() == "official"]
    if not official:
        official = list(releases)
    if not official:
        return None
    typed = [r for r in official
             if _release_group(r).get("primary-type") in _PREFERRED_TYPES]
    pool = typed or official

    def sort_key(release):
        secondary = set(_release_group(release).get("secondary-types")
                        or _release_group(release).get("secondary-type-list") or [])
        date = (_release_group(release).get("first-release-date")
                or release.get("date") or "9999")
        return (1 if secondary & _DEPRIORITISED else 0, date)

    return sorted(pool, key=sort_key)[0]


def score_release(yt_album: str, yt_artist: str, yt_track_count: Optional[int],
                  release: dict) -> float:
    """0-100 score of an MB release search result against a YouTube album."""
    title = _ratio(normalize(yt_album), normalize(release.get("title", ""))) * 50
    artist = _ratio(normalize_artist(yt_artist),
                    normalize_artist(credit_name(release.get("artist-credit")))) * 35
    count = 0.0
    mb_count = release.get("track-count")
    if yt_track_count and mb_count:
        count = 15.0 if mb_count == yt_track_count else max(
            0.0, 15.0 - 5.0 * abs(mb_count - yt_track_count))
    return title + artist + count

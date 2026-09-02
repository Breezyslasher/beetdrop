"""MusicBrainz matching: normalisation, duration-first scoring, release
selection, and album release scoring."""

from beetdrop.matching import (
    best_recording,
    credit_ids,
    credit_name,
    duration_score,
    normalize,
    normalize_artist,
    score_recording,
    score_release,
    select_release,
    significant_qualifiers,
)


def mb_recording(title, artist, length_ms, mbid="r-1", score="100"):
    return {
        "id": mbid, "title": title, "length": length_ms, "score": score,
        "artist-credit": [{"name": artist, "artist": {"id": "a-1", "name": artist}}],
    }


class TestNormalise:
    def test_diacritics_and_case(self):
        assert normalize("Beyoncé") == "beyonce"
        assert normalize_artist("Sigur Rós") == "sigur ros"

    def test_leading_the_for_artists(self):
        assert normalize_artist("The Beatles") == "beatles"
        assert normalize("The Beatles") == "the beatles"

    def test_qualifiers(self):
        assert significant_qualifiers("Song (Live at Wembley)") == {"live"}
        assert significant_qualifiers("Song (Official Video)") == frozenset()
        assert significant_qualifiers("Song (Acoustic Version)") == {"acoustic", "version"}


class TestDuration:
    def test_exact_full_score(self):
        assert duration_score(200, 200000) == (40.0, False)

    def test_reject_past_eight(self):
        assert duration_score(200, 209000)[1] is True

    def test_missing_partial_credit(self):
        score, rejected = duration_score(200, None)
        assert not rejected and 0 < score < 40


class TestScoring:
    def test_clean_match(self):
        recording = mb_recording("Song", "Artist", 200000)
        assert score_recording("Song (Official Video)", "Artist", 200, recording).matched

    def test_live_video_rejected_against_studio(self):
        recording = mb_recording("Song", "Artist", 200000)
        assert score_recording("Song (Live at Wembley)", "Artist", 200, recording).rejected

    def test_matching_live_pair_accepted(self):
        recording = mb_recording("Song (Live)", "Artist", 200000)
        assert score_recording("Song (Live at Wembley)", "Artist", 200, recording).matched

    def test_unrelated_title_rejected(self):
        recording = mb_recording("Completely Different", "Artist", 200000)
        assert not score_recording("Song", "Artist", 200, recording).matched

    def test_best_prefers_duration_over_mb_score(self):
        long = mb_recording("Song", "Artist", 260000, mbid="r-long", score="100")
        studio = mb_recording("Song", "Artist", 200000, mbid="r-studio", score="60")
        best, _ = best_recording("Song", "Artist", 200, [long, studio])
        assert best["id"] == "r-studio"

    def test_no_match_returns_none(self):
        best, _ = best_recording("Song", "Artist", 200,
                                 [mb_recording("Other Thing", "Someone", 500000)])
        assert best is None


class TestCredits:
    def test_joinphrase(self):
        credit = [
            {"name": "A", "artist": {"id": "a", "name": "A"}, "joinphrase": " feat. "},
            {"name": "B", "artist": {"id": "b", "name": "B"}},
        ]
        assert credit_name(credit) == "A feat. B"
        assert credit_ids(credit) == ["a", "b"]


def release(mbid, status="Official", primary="Album", secondary=None,
            first_date="2000-01-01"):
    return {
        "id": mbid, "status": status,
        "release-group": {"id": "rg-" + mbid, "primary-type": primary,
                          "secondary-types": secondary or [],
                          "first-release-date": first_date},
    }


class TestSelectRelease:
    def test_earliest_official_album_wins(self):
        chosen = select_release([
            release("hits", first_date="2015-01-01"),
            release("original", first_date="1994-03-01"),
        ])
        assert chosen["id"] == "original"

    def test_compilation_deprioritised(self):
        chosen = select_release([
            release("comp", secondary=["Compilation"], first_date="1990-01-01"),
            release("studio", first_date="2005-01-01"),
        ])
        assert chosen["id"] == "studio"

    def test_bootleg_used_only_as_last_resort(self):
        assert select_release([release("b", status="Bootleg")])["id"] == "b"
        chosen = select_release([
            release("b", status="Bootleg", first_date="1990-01-01"),
            release("o", first_date="2005-01-01"),
        ])
        assert chosen["id"] == "o"

    def test_empty(self):
        assert select_release([]) is None


class TestScoreRelease:
    def test_exact_album_scores_high(self):
        candidate = {
            "title": "American Idiot", "track-count": 13,
            "artist-credit": [{"name": "Green Day", "artist": {"id": "g", "name": "Green Day"}}],
        }
        assert score_release("American Idiot", "Green Day", 13, candidate) > 90

    def test_tribute_album_scores_low(self):
        candidate = {
            "title": "American Idiot Karaoke Tribute", "track-count": 9,
            "artist-credit": [{"name": "Various Artists", "artist": {"id": "v", "name": "Various Artists"}}],
        }
        assert score_release("American Idiot", "Green Day", 13, candidate) < 60

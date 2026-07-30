"""Title cleaning: noise goes, meaning-changing qualifiers stay."""

from trackpull.cleaning import clean_title


class TestNoiseStripped:
    def test_official_video(self):
        assert clean_title("Song (Official Video)") == "Song"

    def test_official_audio(self):
        assert clean_title("Song (Official Audio)") == "Song"

    def test_lyrics(self):
        assert clean_title("Song (Lyrics)") == "Song"
        assert clean_title("Song [Lyric Video]") == "Song"

    def test_visualizer_both_spellings(self):
        assert clean_title("Song (Visualizer)") == "Song"
        assert clean_title("Song (Visualiser)") == "Song"

    def test_hq(self):
        assert clean_title("Song [HQ]") == "Song"

    def test_bare_bracketed_year(self):
        assert clean_title("Song (1994)") == "Song"
        assert clean_title("Song [2023]") == "Song"

    def test_multiple_noise_groups(self):
        assert clean_title("Song (Official Video) [HQ]") == "Song"

    def test_whitespace_collapsed_after_removal(self):
        assert clean_title("Song  (Official Video)  Extra") == "Song Extra"


class TestQualifiersSurvive:
    def test_live(self):
        assert clean_title("Song (Live at Wembley)") == "Song (Live at Wembley)"

    def test_acoustic(self):
        assert clean_title("Song (Acoustic)") == "Song (Acoustic)"

    def test_remix(self):
        assert clean_title("Song (Someone Remix)") == "Song (Someone Remix)"

    def test_extended(self):
        assert clean_title("Song (Extended Mix)") == "Song (Extended Mix)"

    def test_radio_edit(self):
        assert clean_title("Song (Radio Edit)") == "Song (Radio Edit)"

    def test_mixed_noise_and_qualifier(self):
        assert clean_title("Song (Acoustic) (Official Video)") == "Song (Acoustic)"

    def test_year_with_words_is_not_bare_year(self):
        assert clean_title("Song (1994 Demo)") == "Song (1994 Demo)"


class TestEdges:
    def test_plain_title_untouched(self):
        assert clean_title("Song") == "Song"

    def test_title_that_is_all_noise_falls_back_to_raw(self):
        assert clean_title("(Official Video)") == "(Official Video)"

    def test_empty_brackets_removed(self):
        assert clean_title("Song ()") == "Song"

    def test_case_insensitive(self):
        assert clean_title("Song (OFFICIAL VIDEO)") == "Song"

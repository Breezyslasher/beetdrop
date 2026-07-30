"""Path sanitisation and inbox folder naming."""

from trackpull.paths import (
    FALLBACK_SEGMENT,
    grab_folder_name,
    sanitize_segment,
    unique_folder,
)


class TestSanitizeSegment:
    def test_plain_passthrough(self):
        assert sanitize_segment("Artist - Title") == "Artist - Title"

    def test_illegal_characters_stripped(self):
        assert sanitize_segment('AC/DC: "T.N.T?"') == "ACDC T.N.T"

    def test_control_characters_stripped(self):
        assert sanitize_segment("bad\x00name\x1f\x7f") == "badname"

    def test_whitespace_collapsed(self):
        assert sanitize_segment("a    b\t c") == "a b c"

    def test_trailing_dots_and_spaces_trimmed(self):
        assert sanitize_segment("ends... ") == "ends"

    def test_dot_and_dotdot_rejected(self):
        assert sanitize_segment(".") == FALLBACK_SEGMENT
        assert sanitize_segment("..") == FALLBACK_SEGMENT
        assert sanitize_segment("...") == FALLBACK_SEGMENT

    def test_empty_after_sanitisation_handled(self):
        assert sanitize_segment("") == FALLBACK_SEGMENT
        assert sanitize_segment("???") == FALLBACK_SEGMENT
        assert sanitize_segment("  ") == FALLBACK_SEGMENT

    def test_cap_is_bytes_not_characters(self):
        segment = "—" * 100  # 300 bytes utf-8, 100 characters
        result = sanitize_segment(segment)
        assert len(result.encode("utf-8")) <= 200

    def test_cap_does_not_split_multibyte(self):
        segment = "a" * 199 + "éé"
        result = sanitize_segment(segment)
        encoded = result.encode("utf-8")
        assert len(encoded) <= 200
        encoded.decode("utf-8")  # round-trips cleanly

    def test_truncation_cannot_leave_trailing_dot(self):
        result = sanitize_segment("a" * 199 + ". tail")
        assert not result.endswith(".")
        assert not result.endswith(" ")


class TestGrabFolderName:
    def test_artist_dash_title(self):
        assert grab_folder_name("Artist", "Title") == "Artist - Title"

    def test_slash_and_colon_in_title(self):
        assert grab_folder_name("AC/DC", "Back: In Black") == "ACDC - Back In Black"

    def test_missing_artist(self):
        assert grab_folder_name("", "Title") == "Title"

    def test_missing_both(self):
        assert grab_folder_name("", "") == FALLBACK_SEGMENT

    def test_long_name_capped_at_200_bytes(self):
        name = grab_folder_name("Artist", "x" * 500)
        assert len(name.encode("utf-8")) <= 200


class TestUniqueFolder:
    def test_no_collision_uses_plain_name(self, tmp_path):
        assert unique_folder(tmp_path, "Artist - Title") == tmp_path / "Artist - Title"

    def test_collision_gets_numbered_suffix(self, tmp_path):
        (tmp_path / "Artist - Title").mkdir()
        assert unique_folder(tmp_path, "Artist - Title") == tmp_path / "Artist - Title (2)"

    def test_second_collision_increments(self, tmp_path):
        (tmp_path / "Artist - Title").mkdir()
        (tmp_path / "Artist - Title (2)").mkdir()
        assert unique_folder(tmp_path, "Artist - Title") == tmp_path / "Artist - Title (3)"

    def test_suffix_survives_byte_cap(self, tmp_path):
        name = sanitize_segment("y" * 300)
        (tmp_path / name).mkdir()
        result = unique_folder(tmp_path, name)
        assert result.name.endswith("(2)")
        assert len(result.name.encode("utf-8")) <= 200

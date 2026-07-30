"""Handoff and seed-tag behaviour that can be verified offline."""

from pathlib import Path

import pytest

from trackpull.handoff import deliver, same_filesystem
from trackpull.seedtags import SeedTags, seed_from_result, verify_audio
from trackpull.search import Result


def make_result(**overrides):
    base = dict(
        video_id="vid-1",
        title="Title",
        raw_title="Title (Official Video)",
        artists=["Artist A", "Artist B"],
        album="The Album",
        duration_seconds=200,
    )
    base.update(overrides)
    return Result(**base)


class TestSeedFromResult:
    def test_full_result(self):
        seed = seed_from_result(make_result())
        assert seed.title == "Title"
        assert seed.artist == "Artist A, Artist B"
        assert seed.albumartist == "Artist A"
        assert seed.album == "The Album"
        assert seed.tracknumber is None  # never invented

    def test_album_falls_back_to_title(self):
        seed = seed_from_result(make_result(album=None))
        assert seed.album == "Title"

    def test_no_artists(self):
        seed = seed_from_result(make_result(artists=[]))
        assert seed.artist == "Unknown Artist"
        assert seed.albumartist == "Unknown Artist"


class TestVerifyAudio:
    def test_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="missing"):
            verify_audio(tmp_path / "nope.opus")

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.opus"
        empty.touch()
        with pytest.raises(ValueError, match="empty"):
            verify_audio(empty)

    def test_garbage_file(self, tmp_path):
        garbage = tmp_path / "garbage.opus"
        garbage.write_bytes(b"this is not audio at all")
        with pytest.raises(ValueError, match="not recognisable"):
            verify_audio(garbage)


class TestDeliver:
    def stage(self, tmp_path, name="Artist - Title") -> Path:
        staged = tmp_path / "scratch" / "staged" / name
        staged.mkdir(parents=True)
        (staged / (name + ".opus")).write_bytes(b"audio")
        return staged

    def test_same_fs_rename(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        staged = self.stage(tmp_path)
        final = deliver(staged, inbox, "Artist - Title", cross_fs=False)
        assert final == inbox / "Artist - Title"
        assert (final / "Artist - Title.opus").read_bytes() == b"audio"
        assert not staged.exists()

    def test_collision_never_overwrites(self, tmp_path):
        inbox = tmp_path / "inbox"
        existing = inbox / "Artist - Title"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("keep")
        staged = self.stage(tmp_path)
        final = deliver(staged, inbox, "Artist - Title", cross_fs=False)
        assert final == inbox / "Artist - Title (2)"
        assert (existing / "keep.txt").read_text() == "keep"

    def test_cross_fs_copy_path(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        staged = self.stage(tmp_path)
        final = deliver(staged, inbox, "Artist - Title", cross_fs=True)
        assert final == inbox / "Artist - Title"
        assert (final / "Artist - Title.opus").read_bytes() == b"audio"
        # No hidden temp directory left behind.
        assert not list(inbox.glob(".*"))

    def test_same_filesystem_probe(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert same_filesystem(a, b)

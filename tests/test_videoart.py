"""Embedding the artwork already beside music videos into the videos."""

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.videoart import backfill_video_art, has_cover, sidecar_image

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def make_mp4(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(path),
         "-y", "-loglevel", "error"],
        check=True, timeout=60)


class TestFindingTheImage:
    def album(self, tmp_path, *names):
        folder = tmp_path / "Album"
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"x")
        return folder

    def test_a_thumb_named_for_the_video(self, tmp_path):
        # The user's case: a name another tool cut short, "-thumb.jpg" beside.
        stem = "02 Go!!! _Fighting Dreamers_ (From _"
        folder = self.album(tmp_path, stem + ".mp4", stem + "-thumb.jpg")
        assert sidecar_image(folder / (stem + ".mp4")).name == stem + "-thumb.jpg"

    def test_a_thumb_named_for_the_track_beside_a_plex_video(self, tmp_path):
        folder = self.album(tmp_path, "02 Go!!!-video.mp4", "02 Go!!!-thumb.jpg")
        assert sidecar_image(folder / "02 Go!!!-video.mp4").name == "02 Go!!!-thumb.jpg"

    def test_the_videos_own_name_comes_first(self, tmp_path):
        folder = self.album(tmp_path, "A-video.mp4", "A-video-thumb.jpg", "A-thumb.jpg")
        assert sidecar_image(folder / "A-video.mp4").name == "A-video-thumb.jpg"

    def test_poster_and_bare_names_and_png(self, tmp_path):
        folder = self.album(tmp_path, "B.mp4", "B-poster.png", "C.mp4", "C.jpg")
        assert sidecar_image(folder / "B.mp4").name == "B-poster.png"
        assert sidecar_image(folder / "C.mp4").name == "C.jpg"

    def test_an_uppercase_extension(self, tmp_path):
        folder = self.album(tmp_path, "D.mp4", "D-thumb.JPG")
        assert sidecar_image(folder / "D.mp4").name == "D-thumb.JPG"

    def test_nothing_beside_it(self, tmp_path):
        folder = self.album(tmp_path, "E.mp4", "cover.jpg")
        assert sidecar_image(folder / "E.mp4") is None


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestEmbedding:
    def test_it_embeds_once_and_keeps_the_image(self, tmp_path):
        from mutagen.mp4 import MP4

        video = tmp_path / "music" / "Artist" / "Album" / "01 - Song-video.mp4"
        make_mp4(video)
        image = video.with_name("01 - Song-thumb.jpg")
        image.write_bytes(JPEG)

        first = backfill_video_art([tmp_path / "music"])
        assert (first.total, first.embedded) == (1, 1)
        assert bytes(MP4(str(video))["covr"][0]) == JPEG
        assert image.read_bytes() == JPEG
        # No temp copy left behind.
        assert [p.name for p in video.parent.iterdir()] and not any(
            p.name.startswith(".") for p in video.parent.iterdir())

        again = backfill_video_art([tmp_path / "music"])
        assert (again.embedded, again.already) == (0, 1)

    def test_existing_artwork_is_never_replaced(self, tmp_path):
        from beetdrop.videoart import embed_cover
        from mutagen.mp4 import MP4

        video = tmp_path / "v.mp4"
        make_mp4(video)
        embed_cover(video, PNG, "image/png")
        (tmp_path / "v-thumb.jpg").write_bytes(JPEG)
        result = backfill_video_art([tmp_path])
        assert result.already == 1 and result.embedded == 0
        assert bytes(MP4(str(video))["covr"][0]) == PNG

    def test_what_cannot_be_used_is_counted_not_written(self, tmp_path):
        make_mp4(tmp_path / "a.mp4")
        (tmp_path / "a-thumb.jpg").write_bytes(WEBP)    # misnamed WebP
        make_mp4(tmp_path / "b.mp4")                     # nothing beside it
        (tmp_path / "c.mp4").write_bytes(b"not a video")
        (tmp_path / "c-thumb.jpg").write_bytes(JPEG)
        result = backfill_video_art([tmp_path])
        assert (result.total, result.unusable, result.no_image, result.failed,
                result.embedded) == (3, 1, 1, 1, 0)
        assert not has_cover(tmp_path / "a.mp4")
        assert (tmp_path / "c.mp4").read_bytes() == b"not a video"

    def test_a_video_folder_inside_the_music_folder_is_done_once(self, tmp_path):
        music = tmp_path / "music"
        make_mp4(music / "Music Videos" / "x.mp4")
        (music / "Music Videos" / "x-poster.jpg").write_bytes(JPEG)
        result = backfill_video_art([music, music / "Music Videos"])
        assert (result.total, result.embedded) == (1, 1)


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestTheJob:
    def test_it_runs_from_the_queue_and_says_what_it_did(self, tmp_path):
        music = tmp_path / "music"
        video = music / "Artist" / "Album" / "01 - Song-video.mp4"
        make_mp4(video)
        video.with_name("01 - Song-thumb.jpg").write_bytes(JPEG)
        make_mp4(music / "Artist" / "Album" / "02 - Other-video.mp4")
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)) as client:
            job = client.post("/api/videos/embed-art").json()
            deadline = time.time() + 10
            while time.time() < deadline:
                done = next(j for j in client.get("/api/jobs").json()["jobs"]
                            if j["id"] == job["id"])
                if done["stage"] in ("done", "failed"):
                    break
                time.sleep(0.05)
        assert done["stage"] == "done", done
        assert done["kind"] == "videoart"
        assert "embedded artwork in 1 of 2" in done["detail"]
        assert "1 with no image beside them" in done["detail"]
        assert has_cover(video)


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestExplainingItself:
    def test_a_shortened_image_name_is_paired(self, tmp_path):
        # A tool that limits filename length cut the image's name short.
        video = tmp_path / '02 Go!!! _Fighting Dreamers_ (From _Naruto_).mp4'
        make_mp4(video)
        (tmp_path / "02 Go!!! _Fighting Dreamers_ (From _-thumb.jpg").write_bytes(JPEG)
        result = backfill_video_art([tmp_path])
        assert (result.embedded, result.by_prefix) == (1, 1)
        assert has_cover(video)

    def test_a_shortened_name_that_fits_two_videos_is_not_guessed(self, tmp_path):
        make_mp4(tmp_path / "01 Fighting Dreamers (TV Size).mp4")
        make_mp4(tmp_path / "02 Fighting Dreamers (Full).mp4")
        (tmp_path / "Fighting Dreamers-thumb.jpg").write_bytes(JPEG)
        # Neither name starts with the other once the numbers differ, and
        # with them equal it would fit both; either way, nothing is guessed.
        make_mp4(tmp_path / "Fighting Dreamers (TV Size).mp4")
        make_mp4(tmp_path / "Fighting Dreamers (Full).mp4")
        result = backfill_video_art([tmp_path])
        assert result.embedded == 0 and result.no_image == 4

    def test_an_image_named_exactly_for_one_video_is_not_lent_to_another(self, tmp_path):
        make_mp4(tmp_path / "01 - Song Title.mp4")
        make_mp4(tmp_path / "01 - Song Title (Live).mp4")
        (tmp_path / "01 - Song Title-thumb.jpg").write_bytes(JPEG)
        result = backfill_video_art([tmp_path])
        assert result.embedded == 1 and result.no_image == 1
        assert not has_cover(tmp_path / "01 - Song Title (Live).mp4")

    def test_the_misses_are_shown_with_the_images_beside_them(self, tmp_path):
        make_mp4(tmp_path / "Some Video.mp4")
        (tmp_path / "Completely Different Name.jpg").write_bytes(JPEG)
        result = backfill_video_art([tmp_path])
        assert result.missing == [("Some Video.mp4", ["Completely Different Name.jpg"])]
        assert "images in that folder: Completely Different Name.jpg" in result.report()

    def test_failures_say_why(self, tmp_path, monkeypatch):
        import beetdrop.videoart as videoart

        (tmp_path / "broken.mp4").write_bytes(b"not a video")
        make_mp4(tmp_path / "locked.mp4")
        (tmp_path / "locked-thumb.jpg").write_bytes(JPEG)

        def denied(src, dst, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(videoart.shutil, "copy2", denied)
        result = backfill_video_art([tmp_path])
        assert result.failed == 2
        assert result.reasons["permission denied - Beetdrop cannot write in that folder"] == 1
        assert any(why.startswith("not a readable MP4") for why in result.reasons)
        assert "permission denied" in result.report()


def tag_comment(video: Path, text: str):
    from mutagen.mp4 import MP4
    tagged = MP4(str(video))
    if tagged.tags is None:
        tagged.add_tags()
    tagged.tags["\xa9cmt"] = [text]
    tagged.save()


class TestFindingTheYouTubeVideo:
    def test_from_a_link_in_the_tags(self):
        from beetdrop.videoart import youtube_id
        for link in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                     "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x",
                     "https://youtu.be/dQw4w9WgXcQ",
                     "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ"):
            assert youtube_id(Path("a.mp4"), {"\xa9cmt": [link]}) == "dQw4w9WgXcQ", link

    def test_from_yt_dlps_default_filename(self):
        from beetdrop.videoart import youtube_id
        assert youtube_id(Path("Never Gonna Give You Up [dQw4w9WgXcQ].mp4")) == "dQw4w9WgXcQ"

    def test_nothing_to_go_on(self):
        from beetdrop.videoart import youtube_id
        assert youtube_id(Path("01 - Ed Sheeran - One.mp4"), {"\xa9nam": ["One"]}) == ""


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestFetchingWhenNothingIsBeside:
    def test_the_linked_videos_thumbnail_is_embedded(self, tmp_path):
        from mutagen.mp4 import MP4
        video = tmp_path / "Rick Astley - Never Gonna Give You Up (Official Music Video).mp4"
        make_mp4(video)
        tag_comment(video, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        asked = []
        result = backfill_video_art(
            [tmp_path], fetch_art=lambda vid, url: asked.append(vid) or (JPEG, "image/jpeg"))
        assert asked == ["dQw4w9WgXcQ"]
        assert (result.embedded, result.from_youtube, result.no_image) == (1, 1, 0)
        assert bytes(MP4(str(video))["covr"][0]) == JPEG
        # The link survives the rewrite.
        assert MP4(str(video))["\xa9cmt"] == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]

    def test_an_image_beside_it_comes_before_the_link(self, tmp_path):
        video = tmp_path / "v.mp4"
        make_mp4(video)
        tag_comment(video, "https://youtu.be/dQw4w9WgXcQ")
        (tmp_path / "v-thumb.jpg").write_bytes(JPEG)
        result = backfill_video_art(
            [tmp_path], fetch_art=lambda vid, url: pytest.fail("fetched anyway"))
        assert result.embedded == 1 and result.from_youtube == 0

    def test_no_thumbnail_to_fetch_is_counted(self, tmp_path):
        video = tmp_path / "v.mp4"
        make_mp4(video)
        tag_comment(video, "https://youtu.be/dQw4w9WgXcQ")
        result = backfill_video_art([tmp_path], fetch_art=lambda vid, url: None)
        assert (result.no_thumbnail, result.embedded, result.no_image) == (1, 0, 0)

    def test_without_a_link_nothing_is_fetched(self, tmp_path):
        make_mp4(tmp_path / "01 - Ed Sheeran - One.mp4")
        result = backfill_video_art(
            [tmp_path], fetch_art=lambda vid, url: pytest.fail("fetched anyway"))
        assert result.no_image == 1


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestNotReallyMp4:
    def test_webm_named_mp4_is_named_for_what_it_is(self, tmp_path):
        subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=1",
             "-f", "lavfi", "-i", "sine=duration=1", "-shortest",
             "-c:v", "libvpx-vp9", "-c:a", "libopus", "-f", "webm",
             str(tmp_path / "x.mp4"), "-y", "-loglevel", "error"],
            check=True, timeout=60)
        before = (tmp_path / "x.mp4").read_bytes()
        result = backfill_video_art([tmp_path])
        assert (result.not_mp4, result.failed) == (1, 0)
        assert (tmp_path / "x.mp4").read_bytes() == before


def make_matroska(path: Path, vcodec="libvpx-vp9", acodec="libopus", fmt="webm",
                  comment=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = ["-pix_fmt", "yuv420p"] if vcodec == "libx264" else []
    meta = ["-metadata", "comment=" + comment] if comment else []
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=2",
         "-f", "lavfi", "-i", "sine=duration=2", "-shortest",
         "-c:v", vcodec, *extra, "-c:a", acodec, *meta, "-f", fmt, str(path),
         "-y", "-loglevel", "error"],
        check=True, timeout=60)


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestConvertingWebmNamedMp4:
    def test_it_is_only_done_when_asked(self, tmp_path):
        make_matroska(tmp_path / "x.mp4")
        (tmp_path / "x-thumb.jpg").write_bytes(JPEG)
        before = (tmp_path / "x.mp4").read_bytes()
        result = backfill_video_art([tmp_path])
        assert (result.not_mp4, result.converted) == (1, 0)
        assert (tmp_path / "x.mp4").read_bytes() == before

    def test_converted_then_given_its_artwork(self, tmp_path):
        from mutagen.mp4 import MP4
        video = tmp_path / "Song (Official Video).mp4"
        make_matroska(video)
        video.with_name("Song (Official Video)-thumb.jpg").write_bytes(JPEG)
        result = backfill_video_art([tmp_path], convert=True)
        assert (result.not_mp4, result.converted, result.embedded) == (1, 1, 1)
        tagged = MP4(str(video))                     # a real MP4 now
        assert bytes(tagged["covr"][0]) == JPEG
        assert 1.5 < tagged.info.length < 2.5        # the whole thing
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "Song (Official Video)-thumb.jpg", "Song (Official Video).mp4"]

    def test_its_youtube_link_survives_and_is_used(self, tmp_path):
        video = tmp_path / "v.mp4"
        make_matroska(video, comment="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        asked = []
        result = backfill_video_art(
            [tmp_path], convert=True,
            fetch_art=lambda vid, url: asked.append(vid) or (JPEG, "image/jpeg"))
        assert asked == ["dQw4w9WgXcQ"] and result.from_youtube == 1

    def test_mkv_with_h264_converts_too(self, tmp_path):
        make_matroska(tmp_path / "m.mp4", vcodec="libx264", acodec="aac", fmt="matroska")
        result = backfill_video_art([tmp_path], convert=True)
        assert result.converted == 1 and has_cover(tmp_path / "m.mp4") is False

    def test_vorbis_audio_is_left_as_it_is(self, tmp_path):
        # It would go into an MP4, then play in few players.
        video = tmp_path / "old.mp4"
        make_matroska(video, acodec="libvorbis")
        before = video.read_bytes()
        result = backfill_video_art([tmp_path], convert=True)
        assert result.converted == 0
        assert video.read_bytes() == before
        assert any("vorbis" in why for why in result.left)
        assert "WebM/MKV left as they were" in result.report()
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

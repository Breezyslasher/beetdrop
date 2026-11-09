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


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
@pytest.mark.usefixtures("loopback_only")
class TestTellingPlex:
    def run_job(self, tmp_path, with_image):
        import beetdrop.plex as plex
        from test_plex import TOKEN, FakePlex

        plex._cache.clear()
        music = tmp_path / "music"
        video = music / "Artist" / "Album" / "01 - Song-video.mp4"
        make_mp4(video)
        if with_image:
            video.with_name("01 - Song-thumb.jpg").write_bytes(JPEG)
        server = FakePlex([("3", "Music", "artist", ["/data/music"])])
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        try:
            with TestClient(create_app(config)) as client:
                client.put("/api/settings", json={
                    "plex_url": server.url, "plex_token": TOKEN,
                    "plex_music_path": "/data/music"})
                job = client.post("/api/videos/embed-art").json()
                deadline = time.time() + 10
                while time.time() < deadline:
                    done = next(j for j in client.get("/api/jobs").json()["jobs"]
                                if j["id"] == job["id"])
                    if done["stage"] == "done" and (
                            "Plex" in done["detail"] or not with_image):
                        break
                    time.sleep(0.05)
            return done, server.refreshes
        finally:
            server.close()
            plex._cache.clear()

    def test_changed_videos_are_scanned_into_plex(self, tmp_path):
        done, refreshes = self.run_job(tmp_path, with_image=True)
        assert refreshes == [("3", "/data/music")]
        assert 'Plex is scanning it into "Music"' in done["detail"]

    def test_nothing_changed_asks_plex_nothing(self, tmp_path):
        done, refreshes = self.run_job(tmp_path, with_image=False)
        assert refreshes == [] and "Plex" not in done["detail"]


def yt(title, seconds, artists=("Rick Astley",), vid="dQw4w9WgXcQ"):
    from beetdrop.search import Result
    return Result(video_id=vid, title=title, raw_title=title,
                  artists=list(artists), duration_seconds=seconds,
                  thumbnail_url="https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid)


class TestTheSearchRules:
    def test_what_is_searched_for(self):
        from beetdrop.videoart import search_query
        assert search_query(Path("07 - Caleb Hyles - Idolize.mp4")) == "Caleb Hyles - Idolize"
        assert search_query(Path("2-03 - Song-video.mp4")) == "Song"
        assert search_query(Path("Rick Astley - Never Gonna Give You Up "
                                 "(Official Music Video).mp4")).startswith("Rick Astley")

    def test_title_and_length_both_matching_is_taken(self):
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube(
            "Rick Astley - Never Gonna Give You Up (Official Music Video)", 212.4,
            [yt("Rick Astley - Never Gonna Give You Up (Official Music Video)", 213)])
        assert found is not None and found.video_id == "dQw4w9WgXcQ"

    def test_the_artist_can_come_from_the_result(self):
        # "Ed Sheeran - One" against a video titled just "One" by Ed Sheeran.
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube("Ed Sheeran - One", 255.0,
                                    [yt("One [Official Video]", 255, ("Ed Sheeran",))])
        assert found is not None

    def test_the_right_title_at_the_wrong_length_is_refused(self):
        # A lyric video, a live cut, a re-upload with an intro.
        from beetdrop.videoart import match_on_youtube
        found, closest = match_on_youtube(
            "Rick Astley - Never Gonna Give You Up", 212.0,
            [yt("Rick Astley - Never Gonna Give You Up", 240)])
        assert found is None and "+28s" in closest

    def test_the_right_length_with_another_title_is_refused(self):
        from beetdrop.videoart import match_on_youtube
        found, closest = match_on_youtube(
            "Rick Astley - Never Gonna Give You Up", 212.0,
            [yt("Rick Astley - Together Forever", 212)])
        assert found is None and "Together Forever" in closest

    def test_of_several_the_closest_length_wins(self):
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube("Song", 200.0, [
            yt("Song", 202, vid="aaaaaaaaaaa"), yt("Song", 200, vid="bbbbbbbbbbb")])
        assert found.video_id == "bbbbbbbbbbb"

    def test_no_length_is_no_match(self):
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube("Song", 200.0, [yt("Song", None)])
        assert found is None


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestSearchingInARun:
    """make_mp4 files are one second long; the fake results are timed to
    match or not."""

    def test_found_for_certain_is_embedded(self, tmp_path):
        from mutagen.mp4 import MP4
        video = tmp_path / "07 - Caleb Hyles - Idolize.mp4"
        make_mp4(video)
        asked, fetched, slept = [], [], []
        result = backfill_video_art(
            [tmp_path],
            search=lambda q: asked.append(q) or [yt("Caleb Hyles - Idolize", 1,
                                                    ("Caleb Hyles",), "ccccccccccc")],
            fetch_art=lambda vid, url: fetched.append(vid) or (JPEG, "image/jpeg"),
            sleep=slept.append)
        assert asked == ["Caleb Hyles - Idolize"] and fetched == ["ccccccccccc"]
        assert (result.searched, result.from_search, result.embedded) == (1, 1, 1)
        assert bytes(MP4(str(video))["covr"][0]) == JPEG
        assert slept == []      # no pause before the first search

    def test_not_certain_is_skipped_and_shown_in_the_log(self, tmp_path):
        make_mp4(tmp_path / "Song A.mp4")
        make_mp4(tmp_path / "Song B.mp4")
        slept = []
        result = backfill_video_art(
            [tmp_path], search=lambda q: [yt(q, 300)],
            fetch_art=lambda vid, url: pytest.fail("fetched anyway"),
            sleep=slept.append)
        assert (result.searched, result.no_match, result.embedded) == (2, 2, 0)
        assert slept == [1.0]   # paced between searches
        assert "Searched YouTube, nothing certain enough" in result.report()
        assert "length +299s" in result.report()

    def test_an_image_or_link_means_no_search(self, tmp_path):
        make_mp4(tmp_path / "a.mp4")
        (tmp_path / "a-thumb.jpg").write_bytes(JPEG)
        result = backfill_video_art(
            [tmp_path], search=lambda q: pytest.fail("searched anyway"))
        assert result.embedded == 1 and result.searched == 0

    def test_it_gives_up_when_youtube_keeps_refusing(self, tmp_path):
        from beetdrop.videoart import GIVE_UP_AFTER
        for n in range(GIVE_UP_AFTER + 3):
            make_mp4(tmp_path / ("Song %d.mp4" % n))
        calls = []

        def refusing(query):
            calls.append(query)
            raise RuntimeError("HTTP 429")

        result = backfill_video_art([tmp_path], search=refusing, sleep=lambda s: None)
        assert len(calls) == GIVE_UP_AFTER
        assert "HTTP 429" in result.search_stopped
        assert "Searching YouTube stopped" in result.report()

    def test_without_asking_nothing_is_searched(self, tmp_path):
        make_mp4(tmp_path / "Song.mp4")
        result = backfill_video_art([tmp_path])
        assert result.searched == 0 and result.no_image == 1


class TestTheJobOptions:
    def test_markers_and_titles(self):
        from beetdrop.jobs import videoart_marker, videoart_title
        assert videoart_marker(False, False) == "__videoart__"
        assert videoart_marker(True, True) == "__videoart_convert_search__"
        assert videoart_title(True, True) == (
            "Convert WebM/MKV and search YouTube, then embed music video artwork")
        assert videoart_title(False, False) == "Embed music video artwork"


class TestYourLibrary:
    """Cases from a real run: each was wrongly taken or wrongly skipped."""

    def match(self, filename, length, title, seconds, artists=("Ed Sheeran",)):
        from beetdrop.videoart import match_on_youtube, search_query
        return match_on_youtube(search_query(Path(filename)), length,
                                [yt(title, seconds, artists)])

    # Wrongly taken before: a picture of something else.
    def test_a_live_session_is_not_the_music_video(self):
        found, closest = self.match(
            "My Chemical Romance - Helena [Official Music Video].mp4", 210.0,
            "Helena (Live for BBC Radio 1's \"The Lock Up\", 2005)", 211,
            ("My Chemical Romance",))
        assert found is None and "a different version (live)" in closest

    def test_a_lyric_video_is_not_the_video(self):
        found, closest = self.match(
            "Weezer - Pork And Beans (Closed Captioned).mp4", 190.0,
            "Weezer - Pork and Beans [Lyrics]", 190, ("Weezer",))
        assert found is None and "lyric/audio upload" in closest

    def test_a_numbered_version_is_not_a_remaster(self):
        found, _ = self.match("Weezer - Say It Ain't So (Version 3).mp4", 258.0,
                              "Say It Ain't So (2024 Remaster)", 258, ("Weezer",))
        assert found is None

    # Wrongly skipped before.
    def test_another_upload_a_few_seconds_off_with_the_same_title(self):
        found, _ = self.match("11 - Ed Sheeran - Thinking Out Loud.mp4", 288.0,
                              "Thinking out Loud", 281)
        assert found is not None

    def test_an_audio_upload_for_a_file_that_is_one(self):
        found, _ = self.match("Ed Sheeran - Happier [Official Audio.mp4", 215.0,
                              "Ed Sheeran   Happier Official Audio", 207)
        assert found is not None

    def test_a_name_cut_off_mid_bracket(self):
        found, _ = self.match("Ed Sheeran - Galway Girl [Official A.mp4", 170.0,
                              "Galway Girl", 171)
        assert found is not None
        found, _ = self.match("Ed Sheeran - Perfect [Official Audio.mp4", 263.0,
                              "Perfect", 265)
        assert found is None    # an audio file against an ordinary video
        found, _ = self.match("Ed Sheeran - Perfect [Official Video.mp4", 263.0,
                              "Perfect", 265)
        assert found is not None

    # Rightly skipped, and still are.
    def test_too_far_off_stays_skipped(self):
        assert self.match("Crocodile Rock (Remastered).mp4", 240.0,
                          "Crocodile Rock(Remastered)", 251, ("Elton John",))[0] is None
        assert self.match("Elton John - Bennie and the Jets.mp4", 362.0,
                          "Elton John - Bennie and the Jets", 323, ("Elton John",))[0] is None
        assert self.match("07 - Caleb Hyles - Idolize.mp4", 200.0,
                          "CALEB HYLES COVER ▶ Idolize", 203, ("Caleb Hyles",))[0] is None

    def test_names_are_cleaned_for_searching(self):
        from beetdrop.videoart import search_query
        assert search_query(Path("Weezer - Thank God For Girls-Video.mp4")) == \
            "Weezer - Thank God For Girls"
        assert search_query(Path("Weezer - Thank God for Girls-Lyric.mp4")) == \
            "Weezer - Thank God for Girls (Lyrics)"
        assert search_query(Path("01 We Are-lyrics.mp4")) == "We Are (Lyrics)"
        assert search_query(Path("Ed Sheeran - Galway Girl [Official A.mp4")) == \
            "Ed Sheeran - Galway Girl [Official A]"
        assert search_query(Path("99 Luftballons.mp4")) == "99 Luftballons"
        assert search_query(Path("Artist - Song - Live.mp4")) == "Artist - Song - Live"


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_artwork_found_by_searching_is_marked(tmp_path):
    from mutagen.mp4 import MP4

    from beetdrop.videoart import SEARCH_MARK
    video = tmp_path / "Song.mp4"
    make_mp4(video)
    backfill_video_art([tmp_path], search=lambda q: [yt("Song", 1, vid="eeeeeeeeeee")],
                       fetch_art=lambda vid, url: (JPEG, "image/jpeg"),
                       sleep=lambda s: None)
    assert bytes(MP4(str(video))[SEARCH_MARK][0]).startswith(b"youtube search: eeeeeeeeeee")


def test_labels_are_set_aside_but_song_words_are_not():
    from beetdrop.videoart import match_on_youtube
    # "Full Moon" is not "Moon", and "Music" is a title, not a label.
    assert match_on_youtube("The Black Ghosts - Full Moon", 200.0,
                            [yt("Moon", 200, ("The Black Ghosts",))])[0] is None
    assert match_on_youtube("Madonna - Music (Official Music Video)", 225.0,
                            [yt("Music", 225, ("Madonna",))])[0] is not None


class TestSecondRealRun:
    """From a second real run: near misses, and searches with no artist."""

    def test_a_version_label_on_youtubes_side_at_the_exact_length(self):
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube("Stan", 404.0,
                                    [yt("Stan (Long Version) (feat. Dido)", 404, ("Eminem",))])
        assert found is not None
        found, _ = match_on_youtube("The Way I Am", 290.0,
                                    [yt("Eminem - The Way I Am (Dirty Version)", 290, ("Eminem",))])
        assert found is not None

    def test_but_not_a_second_or_two_off(self):
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube("Stan", 404.0,
                                    [yt("Stan (Long Version)", 406, ("Eminem",))])
        assert found is None

    def test_a_version_the_file_asks_for_is_still_required(self):
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube("Weezer - Say It Ain't So (Version 3)", 258.0,
                                    [yt("Say It Ain't So (2024 Remaster)", 258, ("Weezer",))])
        assert found is None

    def test_live_and_lyric_uploads_get_no_such_allowance(self):
        from beetdrop.videoart import match_on_youtube
        assert match_on_youtube("Song", 200.0, [yt("Song (Live Version)", 200)])[0] is None
        assert match_on_youtube("Song", 200.0, [yt("Song (Lyrics)", 200)])[0] is None

    def test_an_artist_in_front_without_a_dash(self):
        from beetdrop.videoart import match_on_youtube
        found, _ = match_on_youtube(
            "Crocodile Rock (Remastered)", 236.0,
            [yt("Elton John Crocodile Rock (digitally remastered)", 239, ("Elton John",))])
        assert found is not None

    def test_a_track_number_without_a_dash_in_a_numbered_folder(self, tmp_path):
        from beetdrop.videoart import search_query
        for name in ("10 FAITH.mp4", "11 A to Z.mp4", "12 Tsuki to Taiyou.mp4"):
            (tmp_path / name).write_bytes(b"x")
        assert search_query(tmp_path / "10 FAITH.mp4") == "FAITH"
        alone = tmp_path / "solo"
        alone.mkdir()
        (alone / "99 Luftballons.mp4").write_bytes(b"x")
        assert search_query(alone / "99 Luftballons.mp4") == "99 Luftballons"

    def test_the_artist_comes_from_the_folder_when_the_name_has_none(self, tmp_path):
        from beetdrop.videoart import video_artist, with_artist
        video = tmp_path / "music" / "Hiroshi Kitadani" / "One Piece" / "We Are.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"not an mp4")
        assert video_artist(video, [tmp_path / "music"]) == "Hiroshi Kitadani"
        assert with_artist("We Are", "Hiroshi Kitadani") == "Hiroshi Kitadani - We Are"
        assert with_artist("Weezer - Photograph", "Weezer") == "Weezer - Photograph"

    def test_a_title_alone_needs_the_length_to_the_second(self):
        # Searched as "One Piece - We Are"; the result is by the singer.
        from beetdrop.videoart import match_on_youtube
        hit = [yt("We Are!", 240, ("Hiroshi Kitadani",))]
        assert match_on_youtube("One Piece - We Are", 240.3, hit, bare="We Are")[0] is not None
        assert match_on_youtube("One Piece - We Are", 242.0, hit, bare="We Are")[0] is None


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_the_search_carries_the_artist_from_the_videos_tags(tmp_path):
    from mutagen.mp4 import MP4
    video = tmp_path / "music" / "Clips" / "fish.mp4"
    make_mp4(video)
    tagged = MP4(str(video))
    if tagged.tags is None:
        tagged.add_tags()
    tagged.tags["\xa9ART"] = ["Kana-Boon"]
    tagged.save()
    asked = []
    backfill_video_art([tmp_path / "music"], search=lambda q: asked.append(q) or [],
                       sleep=lambda s: None)
    assert asked == ["Kana-Boon - fish"]


class TestTitlesFromTags:
    def test_a_disc_and_track_number_is_dropped(self):
        from beetdrop.videoart import search_query
        assert search_query(Path("1-13 From The D 2 The LBC.mp4")) == "From The D 2 The LBC"
        assert search_query(Path("1-800-273-8255.mp4")) == "1-800-273-8255"

    def test_the_song_beside_it_gives_the_title(self, tmp_path):
        from beetdrop.fulltags import FullTags, write_full_tags
        from beetdrop.videoart import video_title
        if not have_ffmpeg():
            pytest.skip("ffmpeg unavailable")
        song = tmp_path / "1-13 From The D 2 The LBC (feat. Snoop.opus"
        subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "sine=duration=1", "-c:a",
                        "libopus", str(song), "-y", "-loglevel", "error"],
                       check=True, timeout=60)
        write_full_tags(song, FullTags(title="From The D 2 The LBC", artist="Eminem",
                                       album_artist="Eminem", album="Curtain Call 2"))
        video = song.with_suffix(".mp4")
        video.write_bytes(b"x")
        assert video_title(video) == "From The D 2 The LBC"

    def test_the_kind_still_comes_from_the_file_name(self):
        # Titled cleanly from tags, but the file says it is the lyric video.
        from beetdrop.videoart import match_on_youtube
        hits = [yt("Thank God for Girls (Official Video)", 200, ("Weezer",)),
                yt("Thank God for Girls [Lyrics]", 200, ("Weezer",), "lllllllllll")]
        found, _ = match_on_youtube("Weezer - Thank God for Girls", 200.0, hits,
                                    kind_from="Weezer - Thank God for Girls (Lyrics)")
        assert found.video_id == "lllllllllll"


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_the_search_uses_the_title_from_tags(tmp_path):
    from mutagen.mp4 import MP4
    video = tmp_path / "music" / "Eminem" / "1-13 From The D 2 The LBC (feat. Sn.mp4"
    make_mp4(video)
    tagged = MP4(str(video))
    if tagged.tags is None:
        tagged.add_tags()
    tagged.tags["\xa9nam"] = ["From The D 2 The LBC"]
    tagged.save()
    asked = []
    backfill_video_art([tmp_path / "music"], search=lambda q: asked.append(q) or [],
                       sleep=lambda s: None)
    assert asked == ["Eminem - From The D 2 The LBC"]


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestChoosingByHand:
    def library(self, tmp_path):
        music = tmp_path / "music"
        bare = music / "Kana-Boon" / "fish.mp4"
        make_mp4(bare)
        done = music / "Weezer" / "Buddy Holly.mp4"
        make_mp4(done)
        from beetdrop.videoart import embed_cover
        embed_cover(done, JPEG, "image/jpeg")
        webm = music / "Old" / "x.mp4"
        webm.parent.mkdir(parents=True)
        webm.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
        return music, bare

    def test_only_videos_with_no_artwork_are_listed(self, tmp_path):
        from beetdrop.videoart import list_missing_art
        music, bare = self.library(tmp_path)
        videos, total = list_missing_art([music])
        assert total == 1 and [v["name"] for v in videos] == ["fish.mp4"]
        assert videos[0]["folder"] == "Kana-Boon"
        assert videos[0]["query"] == "Kana-Boon - fish"
        assert videos[0]["duration"] == 1

    def test_through_the_api(self, tmp_path, monkeypatch):
        import beetdrop.search as search_module
        import beetdrop.videoart as videoart
        from mutagen.mp4 import MP4

        music, bare = self.library(tmp_path)
        monkeypatch.setattr(search_module, "search_videos",
                            lambda q, limit=10: [yt("fish", 1, ("Kana-Boon",), "fffffffffff")])
        monkeypatch.setattr(videoart, "fetch_thumbnail",
                            lambda vid, url="": (JPEG, "image/jpeg"))
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)) as client:
            listed = client.get("/api/videos/missing-art").json()
            assert listed["total"] == 1
            found = client.get("/api/videos/art-candidates?q=Kana-Boon fish").json()
            assert found["candidates"][0]["video_id"] == "fffffffffff"
            assert found["candidates"][0]["thumbnail"].endswith("/fffffffffff/mqdefault.jpg")
            done = client.post("/api/videos/art", json={
                "path": listed["videos"][0]["path"], "video_id": "fffffffffff"})
            assert done.status_code == 200, done.text
            assert client.get("/api/videos/missing-art").json()["total"] == 0
        assert bytes(MP4(str(bare))["covr"][0]) == JPEG

    def test_a_path_outside_the_library_is_refused(self, tmp_path):
        music, _bare = self.library(tmp_path)
        outside = tmp_path / "elsewhere.mp4"
        make_mp4(outside)
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)) as client:
            for path in (str(outside), "/etc/passwd"):
                response = client.post("/api/videos/art", json={
                    "path": path, "video_id": "fffffffffff"})
                assert response.status_code == 400, path
            bad_id = client.post("/api/videos/art", json={
                "path": str(music / "Kana-Boon" / "fish.mp4"), "video_id": "../../x"})
            assert bad_id.status_code == 400


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_a_video_that_cannot_be_written_says_why(tmp_path, monkeypatch):
    import beetdrop.videoart as videoart
    music = tmp_path / "music"
    video = music / "Both" / "Ed Sheeran" / "Perfect.mp4"
    make_mp4(video)
    monkeypatch.setattr(videoart, "fetch_thumbnail", lambda vid, url="": (JPEG, "image/jpeg"))

    def denied(src, dst, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(videoart.shutil, "copy2", denied)
    config = Config(music_root=music, scratch_root=tmp_path / "s", config_dir=tmp_path / "c")
    with TestClient(create_app(config)) as client:
        response = client.post("/api/videos/art", json={"path": str(video),
                                                        "video_id": "fffffffffff"})
    assert response.status_code == 500
    assert response.json()["detail"] == (
        "could not write Perfect.mp4: permission denied - Beetdrop cannot write "
        "in that folder")


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_the_list_says_which_cannot_be_written(tmp_path, monkeypatch):
    import beetdrop.videoart as videoart
    from beetdrop.videoart import list_missing_art
    music = tmp_path / "music"
    mine = music / "explo" / "a.mp4"
    theirs = music / "Both" / "b.mp4"
    make_mp4(mine)
    make_mp4(theirs)
    real = videoart.os.access
    # Tests run as root, which chmod cannot stop; ask os.access instead.
    monkeypatch.setattr(videoart.os, "access",
                        lambda path, mode: "Both" not in str(path) and real(path, mode))
    videos, _ = list_missing_art([music])
    assert {v["name"]: v["writable"] for v in videos} == {"a.mp4": True, "b.mp4": False}


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
def test_no_thumbnail_at_all_is_a_404_with_the_reason(tmp_path, monkeypatch):
    # A 502 was swapped for a proxy's own page on the way, reason and all.
    import beetdrop.videoart as videoart
    music = tmp_path / "music"
    video = music / "Green Day" / "21st Century Breakdown.mp4"
    make_mp4(video)
    monkeypatch.setattr(videoart, "fetch_thumbnail", lambda vid, url="": None)
    config = Config(music_root=music, scratch_root=tmp_path / "s", config_dir=tmp_path / "c")
    with TestClient(create_app(config)) as client:
        response = client.post("/api/videos/art", json={"path": str(video),
                                                        "video_id": "fffffffffff"})
    assert response.status_code == 404
    assert response.json()["detail"] == "YouTube has no thumbnail for that video"


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg unavailable")
class TestChangingArtworkAlreadyThere:
    def library(self, tmp_path):
        from beetdrop.videoart import embed_cover
        music = tmp_path / "music"
        done = music / "Green Day" / "Holiday.mp4"
        make_mp4(done)
        embed_cover(done, JPEG, "image/jpeg")
        bare = music / "Green Day" / "Boulevard of Broken Dreams.mp4"
        make_mp4(bare)
        make_mp4(music / "Weezer" / "Holiday.mp4")
        return music, done

    def test_found_by_name_with_or_without_artwork(self, tmp_path):
        from beetdrop.videoart import find_videos
        music, _done = self.library(tmp_path)
        videos, total = find_videos([music], "green day")
        assert total == 2
        assert {v["name"]: v["has_art"] for v in videos} == {
            "Holiday.mp4": True, "Boulevard of Broken Dreams.mp4": False}
        # Every word must be there, in the name or the folder.
        assert find_videos([music], "holiday weezer")[1] == 1
        assert find_videos([music], "   ")[1] == 0

    def test_through_the_api_the_picture_now_and_a_new_one(self, tmp_path, monkeypatch):
        import beetdrop.videoart as videoart
        music, done = self.library(tmp_path)
        monkeypatch.setattr(videoart, "fetch_thumbnail", lambda vid, url="": (PNG, "image/png"))
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")
        with TestClient(create_app(config)) as client:
            found = client.get("/api/videos/find?q=green day holiday").json()
            assert found["total"] == 1 and found["videos"][0]["has_art"] is True
            now = client.get("/api/videos/artwork", params={"path": str(done)})
            assert now.status_code == 200 and now.content == JPEG
            assert now.headers["content-type"] == "image/jpeg"
            changed = client.post("/api/videos/art", json={"path": str(done),
                                                            "video_id": "fffffffffff"})
            assert changed.status_code == 200
            assert client.get("/api/videos/artwork",
                              params={"path": str(done)}).content == PNG
            none = client.get("/api/videos/artwork", params={
                "path": str(music / "Green Day" / "Boulevard of Broken Dreams.mp4")})
            assert none.status_code == 404
            outside = client.get("/api/videos/artwork", params={"path": "/etc/passwd"})
            assert outside.status_code == 400

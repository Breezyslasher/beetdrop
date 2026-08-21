"""Live yt-dlp updates: version comparison, activation policy, and the
module hot-swap."""

import importlib
import sys

import pytest

import beetdrop.download as download_module
from beetdrop import updater


def write_fake_ytdlp(target, version):
    package = target / "yt_dlp"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        "from . import version, utils\n"
        "class YoutubeDL:\n"
        "    def __init__(self, *a, **k): pass\n"
    )
    (package / "version.py").write_text("__version__ = '%s'\n" % version)
    (package / "utils.py").write_text("class DownloadError(Exception): pass\n")


@pytest.fixture
def restore_real_ytdlp(tmp_path):
    yield
    target = str(updater.target_dir(tmp_path / "config"))
    sys.path[:] = [p for p in sys.path if p != target]
    for name in list(sys.modules):
        if name == "yt_dlp" or name.startswith("yt_dlp."):
            del sys.modules[name]
    download_module.yt_dlp = importlib.import_module("yt_dlp")


class TestVersionLogic:
    def test_version_tuple(self):
        assert updater._version_tuple("2026.07.04") == (2026, 7, 4)
        assert updater._version_tuple("2026.07.04") > updater._version_tuple("2025.12.30")
        assert updater._version_tuple("garbage") == (0,)

    def test_target_version_read_without_import(self, tmp_path):
        target = tmp_path / "t"
        write_fake_ytdlp(target, "2030.01.01")
        assert updater._target_version(target) == "2030.01.01"
        assert updater._target_version(tmp_path / "missing") == ""


class TestActivate:
    def test_missing_target_is_noop(self, tmp_path):
        before = list(sys.path)
        updater.activate(tmp_path / "config")
        assert sys.path == before

    def test_older_persisted_copy_not_activated(self, tmp_path):
        config_dir = tmp_path / "config"
        write_fake_ytdlp(updater.target_dir(config_dir), "1999.01.01")
        real_version = updater.installed_version()
        updater.activate(config_dir)
        assert str(updater.target_dir(config_dir)) not in sys.path
        assert updater.installed_version() == real_version

    def test_newer_persisted_copy_activated(self, tmp_path, restore_real_ytdlp):
        config_dir = tmp_path / "config"
        write_fake_ytdlp(updater.target_dir(config_dir), "2099.01.01")
        updater.activate(config_dir)
        assert updater.installed_version() == "2099.01.01"
        # The download module now uses the new module for future grabs.
        assert download_module.yt_dlp.version.__version__ == "2099.01.01"


class TestUpdateAndReload:
    def test_update_swaps_live_module(self, tmp_path, monkeypatch, restore_real_ytdlp):
        config_dir = tmp_path / "config"

        def fake_pip(cmd, **kwargs):
            assert "--target" in cmd
            write_fake_ytdlp(updater.target_dir(config_dir), "2099.02.02")

            class R:
                returncode = 0
                stderr = ""
                stdout = ""
            return R()

        monkeypatch.setattr(updater.subprocess, "run", fake_pip)
        old_version = updater.installed_version()
        result = updater.update_and_reload(config_dir)
        assert result["old"] == old_version
        assert result["new"] == "2099.02.02"
        assert updater.installed_version() == "2099.02.02"

    def test_pip_failure_raises(self, tmp_path, monkeypatch):
        def fake_pip(cmd, **kwargs):
            class R:
                returncode = 1
                stderr = "no network"
                stdout = ""
            return R()

        monkeypatch.setattr(updater.subprocess, "run", fake_pip)
        with pytest.raises(RuntimeError, match="no network"):
            updater.update_and_reload(tmp_path / "config")

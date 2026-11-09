"""In-place yt-dlp updates that survive privilege drop and restarts.

The server runs as an unprivileged user, so upgrading the system
site-packages copy is impossible. Updates instead pip-install into
/config/yt-dlp (writable, persistent), which is put at the front of
sys.path. After an update the loaded yt_dlp module is swapped live -
sys.modules entries are purged and the download module's reference is
rebound - so the new version serves the very next grab, no restart.

At startup the persisted copy is activated only if it is at least as
new as the image's own: a rebuilt image with a newer bundled yt-dlp
must not be shadowed by a stale update.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

PIP_TIMEOUT = 300


def target_dir(config_dir: Path) -> Path:
    return config_dir / "yt-dlp"


def _version_tuple(text: str) -> tuple:
    parts = []
    for piece in re.split(r"[.\-]", text.strip()):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _target_version(target: Path) -> str:
    version_file = target / "yt_dlp" / "version.py"
    try:
        match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]",
                          version_file.read_text())
    except OSError:
        return ""
    return match.group(1) if match else ""


def installed_version() -> str:
    import yt_dlp
    return yt_dlp.version.__version__


def _unwrap_urllib3() -> None:
    """Undo yt-dlp's patch of urllib3 before yt-dlp is imported again.

    Importing yt_dlp.networking._requests wraps urllib3's _PERCENT_RE in
    a proxy object. urllib3 is not reloaded with yt-dlp, so a second
    import wraps the proxy in another - and the outer proxy's attribute
    pass-through (`self.re.__getattribute__`) skips the inner one's, so
    `.sub` is lost and every request through urllib3 fails ("object has
    no attribute 'sub'"): YouTube Music search, MusicBrainz, everything.
    Peeling back to the real pattern lets the fresh import wrap it once.
    """
    try:
        import urllib3.util.url as url
    except ImportError:
        return
    pattern = getattr(url, "_PERCENT_RE", None)
    while pattern is not None and not isinstance(pattern, re.Pattern):
        inner = getattr(pattern, "__dict__", {}).get("re")
        if inner is None:
            return  # not yt-dlp's wrapper; leave it alone
        pattern = inner
    if pattern is not None:
        url._PERCENT_RE = pattern


def _swap_loaded_module() -> str:
    """Purge the loaded yt_dlp and rebind the download module to the
    fresh import. Objects held by an in-flight download keep working -
    they reference the old module's objects directly."""
    _unwrap_urllib3()
    for name in list(sys.modules):
        if name == "yt_dlp" or name.startswith("yt_dlp."):
            del sys.modules[name]
    fresh = importlib.import_module("yt_dlp")
    from . import download
    download.yt_dlp = fresh
    return fresh.version.__version__


def activate(config_dir: Path) -> None:
    """Put a previously persisted update on sys.path (front) and load
    it, but only when it is at least as new as the bundled copy."""
    target = target_dir(config_dir)
    persisted = _target_version(target)
    if not persisted:
        return
    if _version_tuple(persisted) < _version_tuple(installed_version()):
        return  # the image ships something newer; leave it in charge
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    _swap_loaded_module()


def update_and_reload(config_dir: Path) -> dict:
    """pip-install the latest yt-dlp into the config volume and make it
    the live module. Returns old/new versions."""
    old = installed_version()
    target = target_dir(config_dir)
    target.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir",
         "--upgrade", "--target", str(target), "yt-dlp"],
        capture_output=True, text=True, timeout=PIP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-500:] or "pip failed")
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    new = _swap_loaded_module()
    return {"old": old, "new": new}

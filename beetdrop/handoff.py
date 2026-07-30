"""Atomic handoff into the inbox.

beets-flask may be watching the inbox, so it must never observe a partially
written folder. The sequence: everything happens in a scratch directory
outside the inbox, and the finished per-grab folder is os.replace'd in as
the final step. When scratch and inbox sit on different filesystems (where
os.replace cannot work), the fallback copies into a dot-prefixed temp
directory inside the inbox — which beets-flask ignores as hidden — and
renames within it. The filesystem topology is detected once at startup,
not per job.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .paths import unique_folder


def same_filesystem(a: Path, b: Path) -> bool:
    """Detect once at startup whether a rename can cross from a to b."""
    return a.stat().st_dev == b.stat().st_dev


def deliver(staged_dir: Path, inbox: Path, folder_name: str, cross_fs: bool) -> Path:
    """Move a fully staged per-grab directory into the inbox atomically.

    Returns the final inbox path. Never overwrites: name collisions get a
    " (2)", " (3)" suffix.
    """
    destination = unique_folder(inbox, folder_name)
    if not cross_fs:
        os.replace(staged_dir, destination)
        return destination

    # Different filesystems: copy into a hidden temp dir inside the inbox,
    # then rename within the same filesystem.
    hidden = inbox / (".%s.beetdrop-tmp" % destination.name)
    if hidden.exists():
        shutil.rmtree(hidden)
    try:
        shutil.copytree(staged_dir, hidden)
        # Re-check for a collision that appeared during the copy.
        destination = unique_folder(inbox, folder_name)
        os.replace(hidden, destination)
    except BaseException:
        if hidden.exists():
            shutil.rmtree(hidden, ignore_errors=True)
        raise
    shutil.rmtree(staged_dir, ignore_errors=True)
    return destination

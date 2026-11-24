"""Owned temporary directories that can be reclaimed after a process crash."""

import logging
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import lifecycle
from .sessions import atomic_json, process_alive, process_identity, read_record

FORMAT = "BlockHarborStaging/1"


def _redirected(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def create_directory(root, prefix):
    root = Path(root)
    if _redirected(root) or not re.fullmatch(r"[a-z]+(?:-[a-z]+)*-", prefix):
        raise ValueError("Папка подготовки недоступна или перенаправлена.")
    with lifecycle.guard():
        directory = Path(tempfile.mkdtemp(dir=root, prefix=prefix))
        try:
            atomic_json(directory / "owner.json", {"format": FORMAT, "prefix": prefix, "owner": process_identity(os.getpid())})
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return directory


@contextmanager
def owned_directory(root, prefix):
    directory = create_directory(root, prefix)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def prune_abandoned(root, prefix):
    """Never remove active-window, redirected, old unmarked or unknown folders."""
    root = Path(root)
    if _redirected(root) or not re.fullmatch(r"[a-z]+(?:-[a-z]+)*-", prefix):
        raise ValueError("Папка подготовки недоступна или перенаправлена.")
    removed = 0
    for directory in root.glob(prefix + "*"):
        if (not re.fullmatch(re.escape(prefix) + r"[a-z0-9_]{8}", directory.name)
                or not directory.is_dir() or _redirected(directory)):
            continue
        marker = read_record(directory / "owner.json")
        owner = marker.get("owner")
        if (marker.get("format") != FORMAT or marker.get("prefix") != prefix
                or not isinstance(owner, dict) or type(owner.get("pid")) is not int
                or not isinstance(owner.get("birth"), str) or process_alive(owner)):
            continue
        try:
            shutil.rmtree(directory)
            removed += 1
        except OSError:
            # A locked temporary file must not stop the launcher from starting.
            # Keep its marker for the next recovery pass and log no private path.
            logging.getLogger(__name__).warning("Не удалось очистить временную папку %s; повтор при следующем запуске", directory.name)
    return removed

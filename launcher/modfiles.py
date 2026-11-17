"""Inspect game archives and publish verified files without replacing user content."""

import hashlib
import json
import os
import tomllib
import zipfile
from functools import lru_cache


def file_stamp(file):
    st = file.stat()
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns


@lru_cache(maxsize=1024)
def file_hash(file, stamp, algorithm):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


DESCRIPTORS = (
    "fabric.mod.json",
    "quilt.mod.json",
    "META-INF/mods.toml",
    "META-INF/neoforge.mods.toml",
)


def read_descriptor(jar, label):
    """Read at most one MiB of declarative metadata without extracting the archive."""
    try:
        entry = jar.getinfo(label)
        if entry.file_size > 1024 * 1024:
            return {}
        text = jar.read(entry).decode("utf-8-sig")
        data = tomllib.loads(text) if label.endswith(".toml") else json.loads(text)
        return data if isinstance(data, dict) else {}
    except (KeyError, ValueError, RuntimeError, RecursionError, zipfile.BadZipFile):
        return {}


@lru_cache(maxsize=2048)
def mod_info(file, stamp):
    """Identify all declared mod IDs for duplicate detection across loaders."""
    ids = set()
    version = ""
    try:
        with zipfile.ZipFile(file) as jar:
            for label in DESCRIPTORS:
                data = read_descriptor(jar, label)
                if not data:
                    continue
                if label.endswith(".toml"):
                    for m in data.get("mods", []):
                        if isinstance(m, dict) and isinstance(m.get("modId"), str):
                            ids.add(m["modId"].lower())
                else:
                    if label == "quilt.mod.json":
                        data = data.get("quilt_loader", {})
                    if not isinstance(data, dict):
                        continue
                    ids.add(str(data.get("id", "")).lower())
                    version = str(data.get("version", ""))
                    for provided in data.get("provides", []):
                        key = (
                            provided.get("id")
                            if isinstance(provided, dict)
                            else provided
                        )
                        if isinstance(key, str):
                            ids.add(key.lower())
    except (OSError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError):
        pass
    return frozenset(ids), version


def publish_new(source, target):
    if os.name == "nt":
        os.rename(
            source, target
        )  # On Windows rename does not overwrite an existing file.
    else:
        os.link(source, target)

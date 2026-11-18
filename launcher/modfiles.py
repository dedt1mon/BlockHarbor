"""Inspect game archives and publish verified files without replacing user content."""

import hashlib
import json
import os
import struct
import tomllib
import zipfile
import zlib
from contextlib import contextmanager
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


def _bounded_directory(stream):
    """Bound ZIP metadata allocation before constructing ZipInfo objects."""
    length = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, length - 65557))
    tail = stream.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        return False
    end = length - len(tail) + offset
    if end >= 20:
        stream.seek(end - 20)
        if stream.read(4) == b"PK\x06\x07":
            return False  # ZIP64 is not needed for a supported <=512 MiB JAR.
    _, disk, central_disk, here, count, size, location, comment = struct.unpack_from("<4s4H2IH", tail, offset)
    if not (disk == central_disk == 0 and here == count and 0 < count <= 32768 and size <= 16 * 1024**2 and location + size <= end and offset + 22 + comment == len(tail)):
        return False
    stream.seek(end - size)
    raw, position, actual = stream.read(size), 0, 0
    while position < len(raw):
        if actual >= count or position + 46 > len(raw) or raw[position:position + 4] != b"PK\x01\x02":
            return False
        name, extra, comment = struct.unpack_from("<3H", raw, position + 28)
        position += 46 + name + extra + comment
        actual += 1
    return position == size and actual == count


@contextmanager
def metadata_zip(source):
    owned = not hasattr(source, "read")
    stream = open(source, "rb") if owned else source
    try:
        if not _bounded_directory(stream):
            raise zipfile.BadZipFile("Archive metadata exceeds the inspection limits")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            yield archive
    finally:
        if owned:
            stream.close()


def read_archive_entry(jar, label, limit):
    """Read a bounded entry, including its compressed input, without extraction."""
    try:
        entry = jar.getinfo(label)
        if (entry.file_size > limit or entry.compress_size > limit + 65536 or entry.flag_bits & 1
                or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            return None
        with jar.open(entry) as source:
            data = source.read(limit + 1)
        return data if len(data) <= limit else None
    except (KeyError, OSError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error):
        return None


def read_descriptor(jar, label):
    """Read at most one MiB of declarative metadata without extracting the archive."""
    try:
        raw = read_archive_entry(jar, label, 1024 * 1024)
        if raw is None:
            return {}
        text = raw.decode("utf-8-sig")
        data = tomllib.loads(text) if label.endswith(".toml") else json.loads(text)
        return data if isinstance(data, dict) else {}
    except (KeyError, OSError, ValueError, RuntimeError, RecursionError, zipfile.BadZipFile, zlib.error):
        return {}


@lru_cache(maxsize=2048)
def mod_info(file, stamp):
    """Identify all declared mod IDs for duplicate detection across loaders."""
    ids = set()
    version = ""
    try:
        with metadata_zip(file) as jar:
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
    except (OSError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error):
        pass
    return frozenset(ids), version


def publish_new(source, target):
    if os.name == "nt":
        os.rename(
            source, target
        )  # On Windows rename does not overwrite an existing file.
    else:
        os.link(source, target)

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

from . import lifecycle


@lru_cache(maxsize=1)
def _windows_file_api():
    import ctypes
    import msvcrt

    query = ctypes.WinDLL("kernel32", use_last_error=True).DeviceIoControl
    query.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                     ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    query.restype = ctypes.c_int
    return ctypes, msvcrt, query


def _usn_stamp(raw):
    if len(raw) < 8:
        return None
    length, major, _minor = struct.unpack_from("<IHH", raw)
    if major not in (2, 3) or length > len(raw) or length < (60 if major == 2 else 76):
        return None
    reference_bytes = 8 if major == 2 else 16
    number = struct.unpack_from("<q", raw, 8 + 2 * reference_bytes)[0]
    if number <= 0:
        return None
    return f"usn:{major}:{raw[8:8 + reference_bytes].hex()}:{number}"


def _content_stamp(file):
    """Safe slower fallback for filesystems without a change-time query."""
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            lifecycle.check()
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _windows_change_token(file):
    # Windows creation time is not a change counter, and even FILE_BASIC_INFO
    # ChangeTime collided for rapid same-size writes in native Windows CI.
    # Read a per-file journal sequence and file reference where supported;
    # never create/modify a volume journal or require elevated permissions.
    try:
        ctypes, msvcrt, query = _windows_file_api()
        with file.open("rb") as stream:
            request = ctypes.create_string_buffer(struct.pack("<HH", 2, 3))
            response = ctypes.create_string_buffer(4096)
            returned = ctypes.c_uint32()
            if query(msvcrt.get_osfhandle(stream.fileno()), 0x000900EB,
                     request, 4, response, len(response), ctypes.byref(returned), None):
                token = _usn_stamp(response.raw[:returned.value])
                if token is not None:
                    return token
    except (OSError, ImportError, AttributeError):
        pass
    # A content fingerprint is real evidence, not a guessed replacement time.
    # Keeping it stable also preserves before/after file-change guards.
    return _content_stamp(file)


def file_stamp(file):
    st = file.stat()
    change = _windows_change_token(file) if os.name == "nt" else st.st_ctime_ns
    return st.st_size, st.st_mtime_ns, change


@lru_cache(maxsize=1024)
def file_hash(file, stamp, algorithm):
    with file.open("rb") as stream:
        if lifecycle.current_scope() is None:
            return hashlib.file_digest(stream, algorithm).hexdigest()
        digest = hashlib.new(algorithm)
        while True:
            lifecycle.check()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


DESCRIPTORS = (
    "fabric.mod.json",
    "quilt.mod.json",
    "META-INF/mods.toml",
    "META-INF/neoforge.mods.toml",
    "mcmod.info",
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


def legacy_mods(data):
    """Forge writers use either a raw array or the versioned modList envelope."""
    rows = data.get("modList") if isinstance(data, dict) else data
    return [row for row in rows[:512] if isinstance(row, dict)] if isinstance(rows, list) else []


def read_descriptor(jar, label):
    """Read at most one MiB of declarative metadata without extracting the archive."""
    try:
        raw = read_archive_entry(jar, label, 1024 * 1024)
        if raw is None:
            return {}
        text = raw.decode("utf-8-sig")
        data = tomllib.loads(text) if label.endswith(".toml") else json.loads(text)
        if label == "mcmod.info":
            return {"modList": legacy_mods(data)}
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
                if label == "mcmod.info":
                    for mod in data["modList"]:
                        key = mod.get("modid")
                        if isinstance(key, str) and key:
                            ids.add(key.lower())
                            if isinstance(mod.get("version"), str):
                                version = mod["version"]
                elif label.endswith(".toml"):
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

"""Bounded archive artwork and persistent, content-addressed thumbnails."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import tempfile
import threading
import tomllib
import zipfile
import zlib
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from PIL import Image

from .modfiles import DESCRIPTORS, file_stamp

MAX_IMAGE = 2 * 1024**2
MAX_THUMBNAIL = 64 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def thumbnail(data):
    """Decode one bounded raster frame, strip metadata, and emit a small PNG."""
    if not 0 < len(data) <= MAX_IMAGE:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            if (
                source.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP", "ICO"}
                or not 0 < width <= 4096
                or not 0 < height <= 4096
                or width * height > 4 * 1024**2
            ):
                return None
            source.thumbnail((96, 96), Image.Resampling.LANCZOS)
            pixels = source.convert("RGBA")
            clean = Image.new("RGBA", (96, 96))
            clean.paste(pixels, ((96 - pixels.width) // 2, (96 - pixels.height) // 2))
            result = io.BytesIO()
            clean.save(result, "PNG", compress_level=6)
            data = result.getvalue()
            return data if len(data) <= MAX_THUMBNAIL else None
    except (OSError, ValueError, SyntaxError, zlib.error, Image.DecompressionBombError):
        return None


def _small_directory(stream):
    """Limit central-directory allocation before handing an archive to zipfile."""
    length = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, length - 65557))
    tail = stream.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        return False
    end = length - len(tail) + offset
    # zipfile follows this locator even when the classic fields are not sentinels.
    # Reject ZIP64 before it can override our allocation limits.
    if end >= 20:
        stream.seek(end - 20)
        if stream.read(4) == b"PK\x06\x07":
            return False
    _, disk, central_disk, entries_here, entries, size, location, comment = (
        struct.unpack_from("<4s4H2IH", tail, offset)
    )
    if not (
        disk == central_disk == 0
        and entries_here == entries
        and 0 < entries <= 8192
        and size <= 4 * 1024**2
        and location + size <= end
        and offset + 22 + comment == len(tail)
    ):
        return False
    # Match zipfile's handling of a possible archive prefix. Count real records;
    # forged EOCD entry counts must not bypass the entry limit either.
    stream.seek(end - size)
    directory = stream.read(size)
    position = 0
    count = 0
    while position < len(directory):
        if (
            count >= entries
            or position + 46 > len(directory)
            or directory[position : position + 4] != b"PK\x01\x02"
        ):
            return False
        name, extra, comment = struct.unpack_from("<3H", directory, position + 28)
        position += 46 + name + extra + comment
        count += 1
    return position == size and count == entries


def _read(archive, name, limit):
    if not isinstance(name, str) or not name or len(name) > 1024:
        return None
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or str(path) != name
        or any(part in (".", "..") for part in path.parts)
        or any(c in name for c in ("\\", ":", "\0"))
    ):
        return None
    try:
        entry = archive.getinfo(name)
        if (
            not 0 < entry.file_size <= limit
            or entry.compress_size > limit
            or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or entry.flag_bits & 1
            or (entry.external_attr >> 16) & 0o170000 == 0o120000
        ):
            return None
        with archive.open(entry) as source:
            data = source.read(limit + 1)
        return data if len(data) <= limit else None
    except (
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        return None


def _paths(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        sized = []
        for size, name in value.items():
            if (
                isinstance(size, str)
                and size.isdecimal()
                and len(size) <= 4
                and isinstance(name, str)
            ):
                number = int(size)
                sized.append((number < 96, abs(number - 96), name))
        return [item[2] for item in sorted(sized)[:16]]
    return []


def archive_thumbnail(file):
    try:
        if file.is_symlink():
            return None
        with file.open("rb") as source:
            if not _small_directory(source):
                return None
            with zipfile.ZipFile(source) as archive:
                paths = []
                for descriptor in DESCRIPTORS:
                    raw = _read(archive, descriptor, 1024**2)
                    if raw is None:
                        continue
                    try:
                        data = (
                            tomllib.loads(raw.decode("utf-8-sig"))
                            if descriptor.endswith(".toml")
                            else json.loads(raw)
                        )
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if descriptor == "quilt.mod.json":
                        data = data.get("quilt_loader", {})
                        data = (
                            data.get("metadata", {}) if isinstance(data, dict) else {}
                        )
                    if not isinstance(data, dict):
                        continue
                    paths.extend(_paths(data.get("icon")))
                    paths.extend(_paths(data.get("logoFile")))
                    mods = data.get("mods", [])
                    if isinstance(mods, list):
                        for mod in mods[:64]:
                            if isinstance(mod, dict):
                                paths.extend(_paths(mod.get("logoFile")))
                paths = list(dict.fromkeys(paths))[:16]
                paths.extend(("pack.png", "icon.png", "logo.png"))
                for name in dict.fromkeys(paths):
                    raw = _read(archive, name, MAX_IMAGE)
                    if raw is not None and (image := thumbnail(raw)):
                        return image
    except (
        OSError,
        ValueError,
        RuntimeError,
        zipfile.BadZipFile,
        RecursionError,
        zlib.error,
    ):
        return None
    return None


class IconCache:
    def __init__(self, root):
        self.directory = Path(root).resolve() / "content-icons-v1"
        self.memory = OrderedDict()
        self.lock = threading.RLock()

    def _path(self, name):
        path = self.directory / name
        if self.directory.is_symlink() or path.is_symlink():
            raise ValueError("Каталог изображений перенаправлен.")
        return path

    def _write(self, name, data):
        target = self._path(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _key(file, stamp):
        return hashlib.sha256((str(file) + "\0" + repr(stamp)).encode()).hexdigest()

    def _remember(self, key, digest):
        self.memory[key] = digest
        self.memory.move_to_end(key)
        if len(self.memory) > 2048:
            self.memory.popitem(last=False)

    def _save(self, key, data, **provenance):
        digest = hashlib.sha256(data).hexdigest() if data else ""
        if data:
            try:
                self.read(digest)
            except (OSError, ValueError):
                self._write(digest + ".png", data)
        self._write(
            "ref-" + key + ".json", json.dumps({"image": digest, **provenance}).encode()
        )
        self._remember(key, digest)
        return digest

    def icon(self, file, stamp=None):
        try:
            stamp = stamp or file_stamp(file)
            key = self._key(file, stamp)
            with self.lock:
                if key in self.memory:
                    digest = self.memory[key]
                else:
                    digest = None
                    try:
                        with self._path("ref-" + key + ".json").open("rb") as source:
                            raw = source.read(1025)
                        record = json.loads(raw) if len(raw) <= 1024 else {}
                        value = (
                            record.get("image") if isinstance(record, dict) else None
                        )
                        if (
                            value == ""
                            or isinstance(value, str)
                            and _HASH.fullmatch(value)
                            and self.read(value)
                        ):
                            digest = value
                    except (OSError, ValueError, TypeError):
                        pass
                    if digest is None:
                        image = archive_thumbnail(file)
                        if file_stamp(file) != stamp:
                            return ""
                        digest = self._save(key, image)
                    self._remember(key, digest)
                return "/api/content-icon/" + digest + ".png" if digest else ""
        except (OSError, ValueError):
            return ""

    def remember_project(self, file, stamp, data, project, version):
        image = thumbnail(data)
        if image is None:
            return
        with self.lock:
            if file_stamp(file) == stamp:
                self._save(
                    self._key(file, stamp), image, project=project, version=version
                )

    def read(self, digest):
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ValueError("Изображение не найдено.")
        with self._path(digest + ".png").open("rb") as source:
            data = source.read(MAX_THUMBNAIL + 1)
        if (
            len(data) > MAX_THUMBNAIL
            or not data.startswith(b"\x89PNG\r\n\x1a\n")
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("Изображение не найдено.")
        return data

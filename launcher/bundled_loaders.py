"""Publisher-verified NeoForge inputs shipped inside the Windows application.

The shipped lock is the authority for this optional application component. No
path or digest from an imported build can substitute for it. Artifacts retain
their original bytes and are copied atomically only after both locked hashes
and the caller's expected official SHA1 match.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from . import lifecycle

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("neoforge_bundle.json")
MAX_MANIFEST = 2 * 1024**2
MAX_FILES = 1024
MAX_FILE = 512 * 1024**2
MAX_TOTAL = 2 * 1024**3
_SHA1 = re.compile(r"[a-f0-9]{40}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_PART = re.compile(r"[A-Za-z0-9_.+-]{1,180}\Z")
_RUNTIME_KINDS = {"installer", "library"}
_ORIGIN = "https://maven.neoforged.net/releases/"


class BundleError(RuntimeError):
    """A present application component is incomplete or fails its trusted lock."""


def _error(detail):
    return BundleError(
        "Встроенный комплект NeoForge в BlockHarbor повреждён: " + detail
        + " Установи актуальный BlockHarbor в прежнюю папку приложения."
    )


def _regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error("не удалось прочитать встроенный файл.") from exc
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise _error("недопустимый тип файла.")
    return info


def _redirected(path):
    try:
        return (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or path.resolve() != path)
    except (OSError, RuntimeError) as exc:
        raise _error("не удалось проверить расположение встроенного файла.") from exc


def _relative(value):
    if not isinstance(value, str) or len(value) > 1024:
        return False
    parts = value.split("/")
    return len(parts) >= 3 and all(_PART.fullmatch(part) and part not in {".", ".."} for part in parts)


def load_manifest():
    """Read the fixed application lock; a source checkout may omit the bundle."""
    lifecycle.check()
    manifest = Path(MANIFEST).absolute()
    if _redirected(manifest):
        raise _error("список файлов перенаправлен.")
    info = _regular(manifest)
    if info is None:
        return None
    if not 0 < info.st_size <= MAX_MANIFEST:
        raise _error("неверный размер списка файлов.")
    try:
        with manifest.open("rb") as source:
            raw = source.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise _error("слишком большой список файлов.")
        data = json.loads(raw)
    except (ValueError, RecursionError, OSError) as exc:
        raise _error("не удалось прочитать список файлов.") from exc
    if (not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != 1
            or data.get("minecraft") != "1.21.1" or data.get("loader") != "neoforge"
            or data.get("loaderVersion") != "21.1.267" or data.get("directory") != "neoforge-21.1.267"
            or not isinstance(data.get("files"), list) or not 1 <= len(data["files"]) <= MAX_FILES):
        raise _error("неподдерживаемое описание комплекта.")
    urls, objects, paths = set(), {}, {}
    installers = 0
    for entry in data["files"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("kind"), str)
                or entry["kind"] not in {*_RUNTIME_KINDS, "source", "notice"}):
            raise _error("неверная запись списка файлов.")
        # Source distributions and notices remain inside the application for
        # redistribution compliance. They can never resolve a runtime request.
        if entry["kind"] not in _RUNTIME_KINDS:
            continue
        address = entry.get("url")
        size, sha1, sha256 = entry.get("size"), entry.get("sha1"), entry.get("sha256")
        if (not _relative(entry.get("path")) or address != _ORIGIN + entry["path"]
                or not isinstance(sha1, str) or not _SHA1.fullmatch(sha1)
                or not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)
                or entry.get("object") != sha256 or type(size) is not int or not 0 < size <= MAX_FILE):
            raise _error("неверный адрес, размер или контрольная сумма файла.")
        if address in urls:
            raise _error("повторяющийся адрес файла.")
        urls.add(address)
        identity = (size, sha1)
        previous = objects.setdefault(sha256, identity)
        if previous != identity:
            raise _error("противоречивые контрольные суммы файла.")
        if entry["path"].casefold() in paths:
            raise _error("совпадающие имена библиотек.")
        paths[entry["path"].casefold()] = sha256
        if entry["kind"] == "installer":
            installers += 1
            expected_path = "net/neoforged/neoforge/21.1.267/neoforge-21.1.267-installer.jar"
            if entry["path"] != expected_path or address != _ORIGIN + expected_path:
                raise _error("установщик не соответствует закреплённой версии.")
    if installers != 1 or sum(size for size, _ in objects.values()) > MAX_TOTAL:
        raise _error("неполный или слишком большой комплект.")
    return data


def entries():
    """Return locked runtime entries, excluding bundled source/notices."""
    manifest = load_manifest()
    return [row for row in manifest["files"] if row["kind"] in _RUNTIME_KINDS] if manifest else []


def _bundle_directory(manifest):
    root = Path(ROOT).resolve()
    base = root / "bundled" / manifest["directory"]
    # The repository intentionally stores locks rather than third-party binary
    # files. Development/source installations retain the network path.
    if not base.exists() and not base.is_symlink():
        return None
    for path in (root / "bundled", base):
        if _redirected(path):
            raise _error("папка файлов перенаправлена.")
        if not path.is_dir():
            raise _error("недопустимый тип папки файлов.")
    return base


def _object_source(base, entry):
    source = base / "objects" / entry["object"]
    for path in (base / "objects", source):
        if _redirected(path):
            raise _error("папка файлов перенаправлена.")
    info = _regular(source)
    if info is None:
        raise _error("не найден " + Path(entry["path"]).name + ".")
    if info.st_size != entry["size"]:
        raise _error("размер файла " + Path(entry["path"]).name + " не совпал.")
    return source, entry, info


def _entry_source(url, sha1):
    lifecycle.check()
    # All shipped runtime entries use this canonical Maven origin. In
    # particular, thousands of unrelated Mojang assets must not parse the lock.
    if (not isinstance(url, str) or not url.startswith(_ORIGIN)
            or not isinstance(sha1, str) or not _SHA1.fullmatch(sha1.lower())):
        return None
    manifest = load_manifest()
    if manifest is None:
        return None
    entry = next((row for row in manifest["files"]
                  if row["kind"] in _RUNTIME_KINDS and row["url"] == url and row["sha1"] == sha1.lower()), None)
    if entry is None:
        return None
    base = _bundle_directory(manifest)
    if base is None:
        return None
    return _object_source(base, entry)


def _consume(source, entry, info, output=None):
    """Verify actual opened/copied bytes, including a concurrent source change."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    sha1, sha256, count = hashlib.sha1(), hashlib.sha256(), 0
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise _error("не удалось открыть " + Path(entry["path"]).name + ".") from exc
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
            raise _error("файл изменился во время проверки.")
        while True:
            lifecycle.check()
            try:
                block = stream.read(1024 * 1024)
            except OSError as exc:
                raise _error("не удалось прочитать " + Path(entry["path"]).name + ".") from exc
            if not block:
                break
            count += len(block)
            if count > entry["size"]:
                raise _error("файл изменился во время проверки.")
            sha1.update(block)
            sha256.update(block)
            if output is not None:
                output.write(block)
    if count != entry["size"] or sha1.hexdigest() != entry["sha1"] or sha256.hexdigest() != entry["sha256"]:
        raise _error("контрольная сумма " + Path(entry["path"]).name + " не совпала.")


def verified_source(url, sha1):
    """Return a verified shipped file or None; never perform a network request."""
    matched = _entry_source(url, sha1)
    if matched is None:
        return None
    source, entry, info = matched
    _consume(source, entry, info)
    lifecycle.check()
    return source


def _document_integrity(entry):
    if (not isinstance(entry, dict) or type(entry.get("size")) is not int
            or not 0 < entry["size"] <= MAX_FILE
            or not isinstance(entry.get("sha1"), str) or not _SHA1.fullmatch(entry["sha1"])
            or not isinstance(entry.get("sha256"), str) or not _SHA256.fullmatch(entry["sha256"])):
        raise _error("неверное описание исходников или уведомлений об авторских правах.")


def distribution_sources():
    """Verify the complete source/notice set before an archive can redistribute it.

    These opaque documents are never runtime inputs, extracted by this module,
    or taken from an imported build's manifest. Names describe copies in the
    transfer archive; paths refer only to fixed, publisher-locked app files.
    """
    manifest = load_manifest()
    if manifest is None:
        return []
    base = _bundle_directory(manifest)
    if base is None:
        return []
    result, names, total = [], set(), 0
    for entry in manifest["files"]:
        if entry["kind"] not in {"source", "notice"}:
            continue
        _document_integrity(entry)
        if not _relative(entry.get("path")) or entry.get("object") != entry["sha256"]:
            raise _error("недопустимый путь исходников.")
        suffix = next((value for value in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".jar", ".tgz", ".txt", ".md")
                       if entry["path"].endswith(value)), None)
        project = entry.get("sourceProject", "notice")
        if (not isinstance(project, str) or not _PART.fullmatch(project)
                or project in {".", ".."} or len(project) > 80 or suffix is None):
            raise _error("неверное имя исходников.")
        name = project + "-" + entry["sha256"][:12] + suffix
        if name.casefold() in names:
            raise _error("повторяющееся имя исходников.")
        names.add(name.casefold())
        total += entry["size"]
        if total > MAX_TOTAL:
            raise _error("слишком большой комплект исходников.")
        source, entry, info = _object_source(base, entry)
        _consume(source, entry, info)
        result.append({"path": source, "name": name, "kind": entry["kind"],
                       **{key: entry[key] for key in ("sha1", "sha256", "size")}})
    if not any(row["kind"] == "source" for row in result):
        raise _error("отсутствуют исходники библиотек.")
    documentation = manifest.get("documentation")
    name = "THIRD-PARTY-NOTICES.txt"
    notice = documentation.get(name) if isinstance(documentation, dict) else None
    _document_integrity(notice)
    source = base / name
    if _redirected(source):
        raise _error("уведомления об авторских правах перенаправлены.")
    info = _regular(source)
    if info is None:
        raise _error("не найдены уведомления об авторских правах.")
    _consume(source, {**notice, "path": name}, info)
    result.append({"path": source, "name": name, "kind": "notice",
                   **{key: notice[key] for key in ("sha1", "sha256", "size")}})
    lifecycle.check()
    return result


def copy_file(url, target, sha1):
    """Atomically populate a normal download target from the locked component."""
    matched = _entry_source(url, sha1)
    if matched is None:
        return False
    source, entry, info = matched
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".blockharbor-download-", delete=False) as out:
            temporary = Path(out.name)
            _consume(source, entry, info, out)
        with lifecycle.guard():
            os.replace(temporary, target)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

"""Carry verified NeoForge inputs in a build ZIP without importing a ready runtime.

Archive hashes detect damage; they never authorize code. Before promoting any
input into the shared game cache, authenticate the installer independently and
authorize library hashes from its embedded, authenticated manifests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import bundled_loaders, lifecycle, preparation

SECTION = "loader-files"
STAGED = ".blockharbor-loader-files"
MANIFEST = "manifest.json"
MAX_MANIFEST = 1024 * 1024
MAX_FILES = 1024
MAX_TOTAL = 1024**3
MAX_FILE = 512 * 1024**2
_SHA1 = re.compile(r"[a-f0-9]{40}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]{0,149}\Z")
_DOCUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,179}\.(?:zip|jar|tar\.gz|tgz|tar\.xz|tar\.bz2|txt|md)\Z", re.I)
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
                 *("LPT" + str(i) for i in range(1, 10))}
# The original notice is retained beside the unmodified NeoForm artifact.
# Source: https://github.com/neoforged/NeoForm/blob/obfuscated/LICENSE
_NEOFORM_NOTICE = """Copyright (c) 2018 Forge Development LLC

This software is provided 'as-is', without any express or implied warranty. In no event will the authors be held liable for any damages arising from the use of this software or data.

Permission is granted to anyone to use this software and data for any purpose, including commercial applications, and to alter it and redistribute it freely, subject to the following restrictions:

1. The origin of this software and data must not be misrepresented; you must not claim that you wrote the original software or data. If you use this software or data in a product, an acknowledgment in the product documentation would be appreciated but is not required.

2. Altered versions must be plainly marked as such, and must not be misrepresented as being the original.

3. This notice may not be removed or altered from any distribution."""


def _regular(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400
    except OSError:
        return False


def _contained(root: Path, relative: str) -> Path:
    root = root.resolve()
    parts = PurePosixPath(relative).parts
    if (not parts or relative != "/".join(parts) or any(
            not re.fullmatch(r"[A-Za-z0-9_.+-]+", p) or p in {".", ".."}
            for p in parts)):
        raise ValueError("Недопустимый путь файлов загрузчика.")
    path = root.joinpath(*parts)
    for depth in range(1, len(parts) + 1):
        item = root.joinpath(*parts[:depth])
        if (item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                or item.resolve() != item):
            raise ValueError("Папка файлов загрузчика перенаправлена.")
    return path


def _hashes(path: Path, *, expected_size=None):
    if not _regular(path):
        return None
    info = path.stat()
    if not 0 < info.st_size <= MAX_FILE or expected_size is not None and info.st_size != expected_size:
        return None
    sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
    with path.open("rb") as source:
        if not os.path.samestat(info, os.fstat(source.fileno())):
            return None
        while block := source.read(1024 * 1024):
            lifecycle.check()
            size += len(block)
            if size > MAX_FILE:
                return None
            sha1.update(block)
            sha256.update(block)
    if size != info.st_size:
        return None
    return {"size": size, "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}


def installer_url(pin: str) -> str:
    if not isinstance(pin, str) or not _PIN.fullmatch(pin) or ".." in pin:
        raise ValueError("Недопустимая версия NeoForge.")
    return preparation._NEO_ORIGIN + f"net/neoforged/neoforge/{pin}/neoforge-{pin}-installer.jar"


def _installer_paths(cache: Path, url: str):
    prefix = "installer-cache/" + hashlib.sha256(url.encode()).hexdigest()
    return _contained(cache, prefix + "/installer.jar"), _contained(cache, prefix + "/source.json")


def _local_digest(cache: Path, url: str):
    known = preparation._NEO_INSTALLER_SHA1.get(preparation._neo_path(url))
    if known:
        return known
    _, record = _installer_paths(cache, url)
    return preparation.Download._cached_installer_digest(record, url) if _regular(record) else None


def _bundled_source(url, sha1, *, expected_size=None):
    """Read a shipped input using the same independent digest as the cache."""
    if not sha1:
        return None
    try:
        path = bundled_loaders.verified_source(url, sha1)
        if path is None:
            return None
        actual = _hashes(path, expected_size=expected_size)
    except OSError as error:
        raise bundled_loaders.BundleError("Не удалось прочитать встроенный файл NeoForge.") from error
    if not actual or actual["sha1"] != sha1:
        raise bundled_loaders.BundleError("Повреждён встроенный файл NeoForge. Переустанови BlockHarbor.")
    return path, actual


def _installer_source(cache: Path, url: str):
    installer, _ = _installer_paths(cache, url)
    trusted = _local_digest(cache, url)
    if not trusted:
        return None
    try:
        actual = _hashes(installer)
    except OSError:
        actual = None
    if actual and actual["sha1"] == trusted:
        return installer, actual
    return _bundled_source(url, trusted)


def _document_name(name):
    return (isinstance(name, str) and bool(_DOCUMENT.fullmatch(name))
            and name.split(".", 1)[0].upper() not in _DEVICE_NAMES)


def _distribution_documents(profile, pin):
    """The packaged distribution's documentation accompanies its exact runtime."""
    manifest = bundled_loaders.load_manifest()
    if not manifest or (manifest["minecraft"], manifest["loader"], manifest["loaderVersion"]) != (
            profile["version"], profile["loader"], pin):
        return []
    try:
        documents = bundled_loaders.distribution_sources()
        verified, names = [], set()
        for item in documents:
            lifecycle.check()
            name = item["name"]
            if (not _document_name(name) or item["kind"] not in {"source", "notice"}
                    or name.casefold() in names):
                raise bundled_loaders.BundleError("Некорректный список исходников и лицензий NeoForge.")
            expected = {key: item[key] for key in ("size", "sha1", "sha256")}
            actual = _hashes(item["path"], expected_size=expected["size"])
            if actual != expected:
                raise bundled_loaders.BundleError("Исходники или лицензии встроенного комплекта NeoForge повреждены.")
            names.add(name.casefold())
            verified.append((item["path"], {"name": name, "kind": item["kind"], **expected}))
        return verified
    except OSError as error:
        raise bundled_loaders.BundleError("Не удалось прочитать исходники и лицензии встроенного комплекта NeoForge.") from error


def _staged_documents(store, profile, pin, installer_digest):
    """Keep an imported archive's opaque documents when re-exporting its inputs.

    Imported hashes still confer no code authority. The already independently
    verified installer must match before accompanying documentation is retained,
    and these objects never participate in cache promotion or execution.
    """
    instance = store._profile_directory(store.instances, profile["id"])
    stage = _contained(instance, STAGED)
    manifest = _contained(stage, MANIFEST)
    if not _regular(manifest):
        return []
    with manifest.open("rb") as source:
        raw = source.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError("Список исходников и лицензий NeoForge превышает допустимый размер.")
    candidate = json.loads(raw)
    if (not isinstance(candidate, dict) or not candidate.get("documents")
            or (candidate.get("minecraft"), candidate.get("loader"), candidate.get("loaderVersion")) != (
                profile["version"], profile["loader"], pin)):
        return []
    metadata, _ = _metadata(raw, profile["version"], profile["loader"], pin)
    if metadata["installer"] != installer_digest:
        return []
    documents = []
    for item in metadata["documents"]:
        source = _contained(stage, "objects/" + item["sha256"])
        actual = _hashes(source, expected_size=item["size"])
        if actual != {key: item[key] for key in ("size", "sha1", "sha256")}:
            raise ValueError("Сохранённые исходники или лицензии NeoForge повреждены. Повтори импорт сборки.")
        documents.append((source, item))
    return documents


def _redistributable(entry):
    """Keep base Minecraft and generated/patched game files out of this bundle."""
    path = entry.get("path", "")
    host = (urlsplit(entry.get("url", "")).hostname or "").lower()
    return (path.endswith((".jar", ".zip")) and not path.startswith(("com/mojang/", "net/minecraft/"))
            and (not path.startswith("net/neoforged/neoform/") or path.startswith("net/neoforged/neoform/1."))
            and host not in {"piston-data.mojang.com", "launcher.mojang.com",
                             "resources.download.minecraft.net", "libraries.minecraft.net"}
            and not host.endswith(".mojang.com"))


def prepare_export(store, pid, status=lambda *_: None):
    """Find a shipped exact installer or warm one for an old completed build.

    The caller owns the installation lock and cancellation scope. This optional
    step never resolves a latest version, installs a game, or runs Java. Catalog
    exports do not call it. A shipped installer also supports a pin-only imported
    build, without networking or modifying its cache. Otherwise a network outage
    leaves ordinary ZIP export usable.
    """
    profile = store.profile(pid)
    if profile["loader"] != "neoforge":
        return False
    instance = store._profile_directory(store.instances, pid)
    cache = (store.root / "game-cache").resolve()
    try:
        stamp = _contained(instance, ".blockharbor-install.json")
        if not _regular(stamp) or stamp.stat().st_size > 65536:
            return False
        saved = json.loads(stamp.read_bytes())
        if not isinstance(saved, dict) or (saved.get("version"), saved.get("loader")) != (profile["version"], profile["loader"]):
            return False
        pin = saved.get("loader_version")
        url = installer_url(pin)
        installer, _ = _installer_paths(cache, url)
        if _installer_source(cache, url):
            return True
        installed = "neoforge-" + pin
        # An imported pin alone is not proof that the sender ever prepared this
        # build. Only a matching local completed stamp/profile authorizes warming.
        if saved.get("cache") != str(cache) or saved.get("id") != installed:
            return False
        manifest = _contained(cache, f"versions/{installed}/{installed}.json")
        if not _regular(manifest) or manifest.stat().st_size > preparation.MAX_METADATA:
            return False
        metadata = json.loads(manifest.read_bytes())
        if not isinstance(metadata, dict) or (metadata.get("id"), metadata.get("inheritsFrom")) != (installed, profile["version"]):
            return False
    except lifecycle.OperationCancelled:
        raise
    except (OSError, ValueError, RecursionError):
        return False
    status("Подготовка файлов NeoForge · добавляем установщик в ZIP сборки…")
    progress = preparation.Progress(status)
    downloader = preparation.Download(progress, cache=cache)
    try:
        with tempfile.TemporaryDirectory(dir=cache, prefix=".blockharbor-export-") as folder:
            downloader(url, Path(folder) / "installer.jar")
        trusted, actual = _local_digest(cache, url), _hashes(installer)
        ready = bool(trusted and actual and actual["sha1"] == trusted)
        if not ready:
            status("Сохранённые файлы NeoForge недоступны; выгружаем обычный ZIP сборки.")
        return ready
    except lifecycle.OperationCancelled:
        raise
    except (OSError, ValueError, preparation.DownloadError):
        status("Сохранённые файлы NeoForge недоступны; выгружаем обычный ZIP сборки.")
        return False
    finally:
        downloader.close()
        progress.close()


def export_plan(store, profile, pin):
    """Use verified cache or shipped inputs; never download or run Java on export."""
    if profile["loader"] != "neoforge" or not pin:
        return None
    cache = (store.root / "game-cache").resolve()
    url = installer_url(pin)
    try:
        source = _installer_source(cache, url)
        if source is None:
            return None
        installer, digest = source
        from .neoforge_install import installer_artifacts
        declared = installer_artifacts(installer, profile["version"], pin)
    except lifecycle.OperationCancelled:
        raise
    except (OSError, ValueError, KeyError, preparation.DownloadError):
        # Ordinary ZIP export remains available for a never-prepared build or
        # an unsupported/corrupt local installer. No claim of readiness is made.
        return None
    metadata = {"format": 1, "minecraft": profile["version"], "loader": "neoforge",
                "loaderVersion": pin, "installer": digest, "libraries": []}
    files = {digest["sha256"]: (installer, digest)}
    total = digest["size"]
    for entry in declared:
        lifecycle.check()
        if not _redistributable(entry):
            continue
        try:
            path = _contained(cache, "libraries/" + entry["path"])
            found = _hashes(path, expected_size=entry.get("size"))
        except OSError:
            found = None
        if not found or found["sha1"] != entry["sha1"]:
            source = _bundled_source(entry["url"], entry["sha1"], expected_size=entry.get("size"))
            if source is None:
                continue
            path, found = source
        if found["sha256"] not in files:
            if len(files) >= MAX_FILES or total + found["size"] > MAX_TOTAL:
                break
            files[found["sha256"]] = (path, found)
            total += found["size"]
        metadata["libraries"].append({"path": entry["path"], **found})
        if entry["path"].startswith("net/neoforged/neoform/"):
            metadata["notices"] = {"NeoForm": _NEOFORM_NOTICE}
    documents = _distribution_documents(profile, pin) or _staged_documents(store, profile, pin, digest)
    for path, item in documents:
        key = item["sha256"]
        if key not in files:
            if len(files) >= MAX_FILES or total + item["size"] > MAX_TOTAL:
                raise ValueError("Файлы NeoForge вместе с исходниками и лицензиями превышают лимиты ZIP.")
            files[key] = (path, {field: item[field] for field in ("size", "sha1", "sha256")})
            total += item["size"]
        metadata.setdefault("documents", []).append(item)
    raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_MANIFEST:
        raise ValueError("Список сохранённых файлов NeoForge слишком велик.")
    return {"manifest": raw, "files": files, "total": total + len(raw),
            "formatVersion": 3 if metadata.get("documents") else 2}


def allowed(parts, directory=False):
    if parts[0] != SECTION:
        return False
    if directory:
        return tuple(parts) in {(SECTION,), (SECTION, "objects")}
    return (tuple(parts) == (SECTION, MANIFEST)
            or len(parts) == 3 and parts[1] == "objects" and bool(_SHA256.fullmatch(parts[2])))


def write_bundle(archive, plan):
    archive.writestr(SECTION + "/" + MANIFEST, plan["manifest"])
    for key, (path, expected) in plan["files"].items():
        lifecycle.check()
        with path.open("rb") as source, archive.open(SECTION + "/objects/" + key, "w") as target:
            size, sha1, sha256 = 0, hashlib.sha1(), hashlib.sha256()
            while block := source.read(1024 * 1024):
                lifecycle.check()
                size += len(block)
                if size > expected["size"]:
                    raise ValueError("Файлы NeoForge изменились во время выгрузки.")
                sha1.update(block)
                sha256.update(block)
                target.write(block)
            if (size != expected["size"] or sha1.hexdigest() != expected["sha1"]
                    or sha256.hexdigest() != key):
                raise ValueError("Файлы NeoForge изменились во время выгрузки.")


def _metadata(raw, minecraft, loader, pin):
    if len(raw) > MAX_MANIFEST:
        raise ValueError("Список файлов NeoForge превышает допустимый размер.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("Повреждён список сохранённых файлов NeoForge.") from None
    if (not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != 1
            or loader != "neoforge" or not pin
            or (data.get("minecraft"), data.get("loader"), data.get("loaderVersion")) != (minecraft, loader, pin)
            or not isinstance(data.get("libraries"), list) or len(data["libraries"]) > MAX_FILES):
        raise ValueError("Сохранённые файлы NeoForge не соответствуют версии сборки.")
    documents = data.get("documents", [])
    if not isinstance(documents, list) or len(documents) > MAX_FILES:
        raise ValueError("Некорректный список исходников и лицензий NeoForge.")
    installer_url(pin)
    objects, paths, document_names = {}, set(), set()
    items = [("installer", data.get("installer")),
             *(("library", item) for item in data["libraries"]),
             *(("document", item) for item in documents)]
    for kind, item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("sha1"), str)
                or not _SHA1.fullmatch(item["sha1"]) or not isinstance(item.get("sha256"), str)
                or not _SHA256.fullmatch(item["sha256"]) or type(item.get("size")) is not int
                or not 0 < item["size"] <= MAX_FILE):
            raise ValueError("Некорректная запись сохранённого файла NeoForge.")
        if kind == "library":
            path = item.get("path")
            parts = PurePosixPath(path).parts if isinstance(path, str) else ()
            if (not parts or path != "/".join(parts) or len(path) > 512 or len(parts) > 16
                    or any(not re.fullmatch(r"[A-Za-z0-9_.+-]+", p) or p in {".", ".."} for p in parts)
                    or not path.endswith((".jar", ".zip")) or path.casefold() in paths):
                raise ValueError("Недопустимый путь библиотеки NeoForge.")
            paths.add(path.casefold())
        elif kind == "document":
            name = item.get("name")
            if (set(item) != {"name", "kind", "size", "sha1", "sha256"}
                    or not _document_name(name) or not isinstance(item.get("kind"), str)
                    or item["kind"] not in {"source", "notice"}
                    or name.casefold() in document_names):
                raise ValueError("Недопустимое имя исходников или лицензий NeoForge.")
            document_names.add(name.casefold())
        key = item["sha256"]
        old = objects.setdefault(key, {k: item[k] for k in ("size", "sha1", "sha256")})
        if any(old[k] != item[k] for k in ("size", "sha1")):
            raise ValueError("Противоречивые записи сохранённых файлов NeoForge.")
    if len(objects) > MAX_FILES or sum(item["size"] for item in objects.values()) > MAX_TOTAL:
        raise ValueError("Сохранённые файлы NeoForge превышают допустимый размер.")
    return data, objects


def import_plan(archive, meta):
    try:
        info = archive.getinfo(SECTION + "/" + MANIFEST)
    except KeyError:
        raise ValueError("В архиве отсутствует список сохранённых файлов NeoForge.") from None
    if info.file_size > MAX_MANIFEST:
        raise ValueError("Список файлов NeoForge превышает допустимый размер.")
    raw = archive.read(info)
    data, objects = _metadata(raw, meta["minecraft"], meta["loader"], meta.get("loaderVersion"))
    if (meta["formatVersion"] == 3) != bool(data.get("documents")):
        raise ValueError("Версия архива не соответствует списку исходников и лицензий NeoForge.")
    expected = {SECTION + "/" + MANIFEST, *(SECTION + "/objects/" + key for key in objects)}
    actual = {item.filename for item in archive.infolist()
              if item.filename.startswith(SECTION + "/") and not item.is_dir()}
    if actual != expected:
        raise ValueError("Состав сохранённых файлов NeoForge не совпадает с их списком.")
    for key, item in objects.items():
        if archive.getinfo(SECTION + "/objects/" + key).file_size != item["size"]:
            raise ValueError("Неверный размер сохранённого файла NeoForge.")
    return objects


def _copy_verified(source: Path, target: Path, expected):
    """Copy, hash, then atomically expose bytes; never overwrite with unchecked data."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with source.open("rb") as src, tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".blockharbor-download-", delete=False) as out:
            temporary = Path(out.name)
            sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
            while block := src.read(1024 * 1024):
                lifecycle.check()
                size += len(block)
                if size > expected["size"]:
                    raise ValueError("Сохранённый файл NeoForge изменился во время проверки.")
                sha1.update(block)
                sha256.update(block)
                out.write(block)
        if (size != expected["size"] or sha1.hexdigest() != expected["sha1"]
                or sha256.hexdigest() != expected["sha256"]):
            raise ValueError("Сохранённый файл NeoForge повреждён.")
        with lifecycle.guard():
            os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply(instance, cache, minecraft, loader, pin, status=lambda *_: None):
    """Seed independently verified inputs under the caller's game installation lock.

    A bundle is optional. A missing official checksum or a stale/corrupt staged
    object falls back to normal preparation; unverified bytes never leave it.
    No profile, Java runtime, generated game JAR or completion receipt is copied.
    """
    if loader != "neoforge" or not pin:
        return 0
    instance, cache = Path(instance).resolve(), Path(cache).resolve()
    stage = _contained(instance, STAGED)
    if not stage.is_dir():
        return 0
    manifest = _contained(stage, MANIFEST)
    if not _regular(manifest):
        return 0
    try:
        with manifest.open("rb") as source:
            raw = source.read(MAX_MANIFEST + 1)
        metadata, _ = _metadata(raw, minecraft, loader, pin)
        url = installer_url(pin)
        trusted = _local_digest(cache, url)
        if not trusted:
            status("Подготовка файлов NeoForge · проверяем сохранённые файлы из сборки…")
            trusted = preparation._installer_checksum(
                url, deadline=time.monotonic() + preparation.CHECKSUM_SECONDS)
        expected = metadata["installer"]
        source = _contained(stage, "objects/" + expected["sha256"])
        actual = _hashes(source, expected_size=expected["size"])
        if actual != expected or actual["sha1"] != trusted:
            return 0
        from .neoforge_install import installer_artifacts
        declared = {entry["path"]: entry for entry in installer_artifacts(source, minecraft, pin)
                    if _redistributable(entry)}
    except lifecycle.OperationCancelled:
        raise
    except (OSError, ValueError, KeyError, preparation.DownloadError):
        return 0
    status("Подготовка файлов NeoForge · используем проверенные файлы из сборки…")
    installer, record = _installer_paths(cache, url)
    copied = 0
    if _hashes(installer) != expected:
        _copy_verified(source, installer, expected)
        copied += 1
    preparation.Download._remember_installer_digest(record, url, trusted)
    for item in metadata["libraries"]:
        lifecycle.check()
        trusted_item = declared.get(item["path"])
        if (not trusted_item or trusted_item["sha1"] != item["sha1"]
                or trusted_item.get("size") is not None and trusted_item["size"] != item["size"]):
            continue
        source = _contained(stage, "objects/" + item["sha256"])
        if _hashes(source, expected_size=item["size"]) != {k: item[k] for k in ("sha1", "sha256", "size")}:
            continue
        target = _contained(cache, "libraries/" + item["path"])
        expected = {k: item[k] for k in ("sha1", "sha256", "size")}
        if _hashes(target) != expected:
            _copy_verified(source, target, expected)
            copied += 1
    return copied

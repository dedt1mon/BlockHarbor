"""Stage the reviewed NeoForge recovery inputs for a Windows release.

The lock is reviewed source, never generated from a user's game cache during a
release build. A cold build downloads exact upstream objects; a warm build may
reuse only objects matching all three locked size/SHA-1/SHA-256 values. Runtime
code uses the same lock and never needs these build-time downloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = PROJECT / "launcher" / "neoforge_bundle.json"
ORIGIN = "https://maven.neoforged.net/releases/"
MAX_FILE = 512 * 1024**2
MAX_TOTAL = 2 * 1024**3
_PART = re.compile(r"[A-Za-z0-9_+.-]{1,180}\Z")
_SHA1 = re.compile(r"[a-f0-9]{40}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_KINDS = {"installer", "library", "source", "notice"}
# These obligations belong to this reviewed release pin, not to downloadable
# metadata. Removing a source row from the JSON cannot silently waive them.
REQUIRED_SOURCE_PROJECTS = frozenset({
    "LegacyInstaller", "NeoForge", "FancyModLoader", "Bus", "CoreMods",
    "ModLauncher", "BootstrapLauncher", "MergeTool", "NightConfig", "JarJar",
    "Nashorn", "ForgeSrgUtils", "InstallerTools", "AutoRenamingTool",
    "NeoSrgUtils",
    "Trove", "Javadoctor", "InstallerTools-2.1.0", "InstallerTools-2.1.4",
})


class BundleBuildError(ValueError):
    """An incomplete or unauthenticated bundle must not be released."""


def _relative(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise BundleBuildError("Invalid bundle path")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
                *("LPT" + str(i) for i in range(1, 10))}
    if any(not _PART.fullmatch(part) or part in {".", ".."} or part.endswith(".")
           or part.split(".", 1)[0].upper() in reserved for part in parts):
        raise BundleBuildError("Invalid bundle path: " + value)
    return value


def _https(value, *, canonical=True):
    if not isinstance(value, str):
        raise BundleBuildError("Invalid bundle URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in {None, 443}
            or (canonical and (parsed.query or parsed.fragment)) or parsed.netloc != parsed.hostname
            or any(c.isspace() for c in value)):
        raise BundleBuildError("Bundle URLs must be canonical HTTPS: " + value)
    return parsed


def load_manifest(path=DEFAULT_MANIFEST):
    path = Path(path)
    if path.stat().st_size > 4 * 1024**2:
        raise BundleBuildError("Bundle lock is too large")
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BundleBuildError("Invalid bundle lock") from exc
    if (not isinstance(manifest, dict) or manifest.get("format") != 1
            or manifest.get("minecraft") != "1.21.1"
            or manifest.get("loader") != "neoforge"
            or manifest.get("loaderVersion") != "21.1.267"
            or manifest.get("directory") != "neoforge-21.1.267"):
        raise BundleBuildError("Unsupported bundled loader pin")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1024:
        raise BundleBuildError("Invalid bundle file list")
    paths, urls, objects, total, installers = set(), set(), {}, 0, 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in _KINDS:
            raise BundleBuildError("Invalid bundle entry kind")
        relative = _relative(entry.get("path"))
        address = entry.get("url")
        parsed = _https(address)
        if not parsed.path.endswith("/" + relative):
            raise BundleBuildError("Bundle URL does not match path: " + relative)
        sha1, sha256, size = entry.get("sha1"), entry.get("sha256"), entry.get("size")
        if (not isinstance(sha1, str) or not _SHA1.fullmatch(sha1)
                or not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)
                or entry.get("object") != sha256
                or type(size) is not int or not 0 < size <= MAX_FILE):
            raise BundleBuildError("Invalid bundle integrity record: " + relative)
        if relative.casefold() in paths or address in urls:
            raise BundleBuildError("Duplicate bundle entry: " + relative)
        paths.add(relative.casefold())
        urls.add(address)
        signature = (sha1, size)
        if sha256 in objects and objects[sha256] != signature:
            raise BundleBuildError("Conflicting bundle object: " + relative)
        objects[sha256] = signature
        total += size
        if entry["kind"] in {"installer", "library"}:
            if (address != ORIGIN + relative or not relative.endswith((".jar", ".zip"))
                    or relative.startswith(("com/mojang/", "net/minecraft/"))):
                raise BundleBuildError("Unsupported runtime artifact: " + relative)
        if entry["kind"] == "installer":
            installers += 1
            if relative != "net/neoforged/neoforge/21.1.267/neoforge-21.1.267-installer.jar":
                raise BundleBuildError("Installer path does not match the locked pin")
    if installers != 1 or total > MAX_TOTAL:
        raise BundleBuildError("Invalid bundle installer count or total size")
    notices = manifest.get("notices", [])
    if not isinstance(notices, list) or any(
            not isinstance(notice, dict)
            or any(not isinstance(notice.get(key), str) or not notice[key]
                   for key in ("name", "text", "source")) for notice in notices):
        raise BundleBuildError("Invalid bundle notices")
    notice_names = {notice["name"] for notice in notices}
    if len(notice_names) != len(notices):
        raise BundleBuildError("Duplicate bundle notice")
    for entry in entries:
        if entry["kind"] in {"installer", "library"}:
            references = entry.get("notices")
            if (not isinstance(references, list) or not references
                    or any(not isinstance(name, str) or name not in notice_names for name in references)):
                raise BundleBuildError("Missing license notice for runtime artifact: " + entry["path"])
    supplied = {entry.get("sourceProject") for entry in entries if entry["kind"] == "source"}
    missing = REQUIRED_SOURCE_PROJECTS - supplied
    if missing:
        raise BundleBuildError("Missing corresponding source archives: " + ", ".join(sorted(missing)))
    documents = manifest.get("documentation")
    if not isinstance(documents, dict) or set(documents) != {"THIRD-PARTY-NOTICES.txt"}:
        raise BundleBuildError("Missing locked redistribution documentation")
    notice = documents["THIRD-PARTY-NOTICES.txt"]
    rendered = _documents(manifest)["THIRD-PARTY-NOTICES.txt"].encode("utf-8")
    if (not isinstance(notice, dict) or type(notice.get("size")) is not int
            or notice["size"] != len(rendered)
            or notice.get("sha1") != hashlib.sha1(rendered).hexdigest()
            or notice.get("sha256") != hashlib.sha256(rendered).hexdigest()):
        raise BundleBuildError("Redistribution notice hashes do not match the locked text")
    return manifest


def verified(path, entry):
    """Check actual bytes every time; neither timestamps nor filenames are trust."""
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_size != entry["size"]):
            return False
        sha1, sha256, count = hashlib.sha1(), hashlib.sha256(), 0
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > entry["size"]:
                    return False
                sha1.update(block)
                sha256.update(block)
        return (count == entry["size"] and sha1.hexdigest() == entry["sha1"]
                and sha256.hexdigest() == entry["sha256"])
    except OSError:
        return False


class _HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Upstream GitHub source archives redirect to codeload. TLS remains
        # mandatory, and content still has to match the reviewed whole-file lock.
        _https(newurl, canonical=False)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, destination, expected_size):
    opener = build_opener(_HTTPSRedirects())
    request = Request(url, headers={"User-Agent": "BlockHarbor-release-builder/1",
                                    "Accept-Encoding": "identity"})
    with opener.open(request, timeout=60) as response, destination.open("wb") as out:
        _https(response.geturl(), canonical=False)
        length = response.headers.get("Content-Length")
        if length is not None and int(length) != expected_size:
            raise BundleBuildError("Upstream file size differs from the lock: " + url)
        count = 0
        while block := response.read(1024 * 1024):
            count += len(block)
            if count > expected_size:
                raise BundleBuildError("Upstream file exceeds locked size: " + url)
            out.write(block)
        if count != expected_size:
            raise BundleBuildError("Upstream download is incomplete: " + url)


def fetch(entry, objects):
    destination = objects / entry["object"]
    if verified(destination, entry):
        return destination
    # A damaged cached object is never reused. Fetch into a private file and
    # replace atomically only after both hashes and exact byte count agree.
    destination.unlink(missing_ok=True)
    failures = []
    for attempt in range(3):
        with tempfile.NamedTemporaryFile(dir=objects, prefix=".download-", delete=False) as out:
            partial = Path(out.name)
        try:
            _download(entry["url"], partial, entry["size"])
            if not verified(partial, entry):
                raise BundleBuildError("SHA-1/SHA-256 mismatch: " + entry["path"])
            os.replace(partial, destination)
            return destination
        except (OSError, ValueError) as exc:
            failures.append(str(exc))
        finally:
            partial.unlink(missing_ok=True)
    raise BundleBuildError("Cannot build a complete NeoForge bundle: " + entry["path"]
                           + "; " + failures[-1])


def _installer_coverage(manifest, objects):
    """A missing preparation library must fail CI instead of users' first launch."""
    installer = next(e for e in manifest["files"] if e["kind"] == "installer")
    try:
        with ZipFile(objects / installer["object"]) as archive:
            documents = []
            for name in ("install_profile.json", "version.json"):
                if archive.getinfo(name).file_size > 4 * 1024**2:
                    raise BundleBuildError("Installer metadata is too large")
                documents.append(json.loads(archive.read(name)))
    except (OSError, KeyError, ValueError, BadZipFile) as exc:
        raise BundleBuildError("Cannot validate bundled installer metadata") from exc
    profile, version = documents
    if (profile.get("minecraft") != manifest["minecraft"]
            or profile.get("version") != "neoforge-" + manifest["loaderVersion"]
            or version.get("inheritsFrom") != manifest["minecraft"]
            or version.get("id") != "neoforge-" + manifest["loaderVersion"]):
        raise BundleBuildError("Bundled installer metadata has a different pin")
    expected = {}
    for document in documents:
        for library in document.get("libraries", []):
            artifact = library.get("downloads", {}).get("artifact", {})
            if not artifact.get("url", "").startswith(ORIGIN):
                continue
            path = artifact.get("path", "")
            if path.startswith(("com/mojang/", "net/minecraft/")):
                continue
            expected[path] = (artifact.get("url"), artifact.get("sha1"), artifact.get("size"))
    actual = {e["path"]: (e["url"], e["sha1"], e["size"])
              for e in manifest["files"] if e["kind"] == "library"}
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        raise BundleBuildError("Bundle does not match official installer libraries; missing="
                               + repr(missing) + ", extra=" + repr(extra))


def _documents(manifest):
    text = ["BlockHarbor bundled NeoForge preparation files", "",
            "Original upstream artifacts are unmodified. Each object in objects/ is named",
            "by its SHA-256. manifest.json records the original path, URL, size and hashes.",
            "Copy a source object to its original .zip or .jar filename to inspect/extract it.",
            "Source archives are included for redistribution and are never executed by the launcher.",
            "Minecraft game binaries, assets, complete official mappings and Java are not included.", ""]
    for entry in manifest["files"]:
        if entry["kind"] in {"source", "notice"}:
            text.extend([entry.get("name", entry["path"]), "  " + entry["url"],
                         "  objects/" + entry["object"], ""])
    for notice in manifest.get("notices", []):
        text.extend(["=" * 72, notice["name"], notice["source"], "", notice["text"], ""])
    return {"manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            "THIRD-PARTY-NOTICES.txt": "\n".join(text)}


def _source_coverage(manifest, objects):
    for project in REQUIRED_SOURCE_PROJECTS:
        # A Maven -sources.jar without the compilation scripts is insufficient
        # for this bundle's corresponding-source distribution.
        entries = [entry for entry in manifest["files"]
                   if entry["kind"] == "source" and entry.get("sourceProject") == project]
        complete = False
        for entry in entries:
            try:
                if entry["path"].endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tar")):
                    with tarfile.open(objects / entry["object"], "r:*") as archive:
                        names = archive.getnames()
                else:
                    with ZipFile(objects / entry["object"]) as archive:
                        names = archive.namelist()
                basenames = {name.rsplit("/", 1)[-1].lower() for name in names}
                build = basenames & {"build.gradle", "build.gradle.kts", "build.xml",
                                     "pom.xml", "makefile", "cmakelists.txt"}
                # Trove 1.0.2's original upstream source distribution shipped
                # no build file. Its complete source plus documented javac
                # rebuild procedure accompany the byte-identical upstream tar.
                if (project == "Trove" and entry.get("sourceFormat") == "original-source-distribution"
                        and entry.get("rebuildNotice") in {n["name"] for n in manifest["notices"]}):
                    build = True
                license_files = any(name.startswith(("license", "copying")) for name in basenames)
                if build and license_files and any(name.endswith(".java") for name in names):
                    complete = True
            except (OSError, BadZipFile, tarfile.TarError):
                continue
        if not complete:
            raise BundleBuildError("Corresponding source lacks source, license or build scripts: " + project)


def verify_staged(stage, manifest_path=DEFAULT_MANIFEST):
    manifest = load_manifest(manifest_path)
    bundle = Path(stage) / "bundled" / manifest["directory"]
    for entry in manifest["files"]:
        if not verified(bundle / "objects" / entry["object"], entry):
            raise BundleBuildError("Missing or damaged staged NeoForge file: " + entry["path"])
    _installer_coverage(manifest, bundle / "objects")
    _source_coverage(manifest, bundle / "objects")
    for name, expected in _documents(manifest).items():
        path = bundle / name
        if not path.is_file() or path.read_bytes() != expected.encode("utf-8"):
            raise BundleBuildError("Missing or damaged staged NeoForge documentation: " + name)
    return bundle


def prepare(cache, stage, manifest_path=DEFAULT_MANIFEST):
    manifest = load_manifest(manifest_path)
    cached = Path(cache) / "neoforge-bundle" / "objects"
    cached.mkdir(parents=True, exist_ok=True)
    unique = {entry["object"]: entry for entry in manifest["files"]}
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda entry: fetch(entry, cached), unique.values()))
    _installer_coverage(manifest, cached)
    _source_coverage(manifest, cached)
    bundle = Path(stage) / "bundled" / manifest["directory"]
    objects = bundle / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    for entry in unique.values():
        destination = objects / entry["object"]
        with tempfile.NamedTemporaryFile(dir=objects, prefix=".stage-", delete=False) as out:
            partial = Path(out.name)
        try:
            shutil.copyfile(cached / entry["object"], partial)
            if not verified(partial, entry):
                raise BundleBuildError("Cached NeoForge file changed during staging: " + entry["path"])
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    for name, text in _documents(manifest).items():
        (bundle / name).write_text(text, encoding="utf-8", newline="\n")
    return verify_staged(stage, manifest_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    print(prepare(args.cache, args.stage, args.manifest))


if __name__ == "__main__":
    main()

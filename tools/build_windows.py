"""Build a self-extracting BlockHarbor.exe on Linux, macOS, or Windows.

Requires Python 3.10+ and Go (the published binary uses Go 1.27.4).
Example: python tools/build_windows.py --output ../BlockHarbor-Windows-x64
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from windows_resources import write_object

TOOLS = Path(__file__).resolve().parent
PROJECT = TOOLS.parent


def fetch(entry, cache):
    destination = cache / entry["file"]
    if (
        destination.is_file()
        and sha256(destination.read_bytes()).hexdigest() == entry["sha256"]
    ):
        return destination
    with urlopen(entry["url"], timeout=90) as response:
        data = response.read()
    if sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("SHA-256 mismatch: " + entry["file"])
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as out:
        staged = Path(out.name)
    try:
        staged.write_bytes(data)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    return destination


def install_archives(cache, stage):
    lock = json.loads((TOOLS / "windows-lock.json").read_text("utf-8"))
    entries = [lock["python"], *lock["wheels"]]
    with ThreadPoolExecutor(max_workers=5) as pool:
        archives = list(pool.map(lambda entry: fetch(entry, cache), entries))
    runtime = stage / "runtime"
    packages = runtime / "Lib" / "site-packages"
    packages.mkdir(parents=True)
    for idx, archive in enumerate(archives):
        with ZipFile(archive) as zipped:
            if corrupt := zipped.testzip():
                raise ValueError(f"Bad archive member {archive.name}: {corrupt}")
            if idx and any(".data/" in name for name in zipped.namelist()):
                raise ValueError(
                    "Unexpected wheel installation scheme: " + archive.name
                )
            zipped.extractall(runtime if idx == 0 else packages)
    (runtime / "python313._pth").write_text(
        "python313.zip\n.\nLib/site-packages\n..\n", encoding="ascii"
    )


def build_stub(go, stage):
    environment = {
        **os.environ,
        "GOOS": "windows",
        "GOARCH": "amd64",
        "GOAMD64": "v1",
        "CGO_ENABLED": "0",
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOFLAGS": "",
    }
    binary = stage / "BlockHarbor.exe"
    # Building a package directory lets Go include the generated .syso resources.
    # All intermediate resources live outside the distributable payload.
    with tempfile.TemporaryDirectory(dir=stage.parent) as temp:
        source = Path(temp)
        for name in ("bootstrap_windows.go", "bootstrap_payload.go"):
            shutil.copyfile(TOOLS / name, source / name)
        (source / "go.mod").write_text(
            "module blockharbor-bootstrap\n\ngo 1.20\n", encoding="ascii"
        )
        write_object(PROJECT / "ui" / "icon.ico", source / "windows_amd64.syso")
        subprocess.run(
            [
                go,
                "build",
                "-trimpath",
                "-buildvcs=false",
                "-ldflags",
                "-H=windowsgui -s -w -buildid=",
                "-o",
                str(binary),
                ".",
            ],
            check=True,
            env=environment,
            cwd=source,
        )
    goroot = subprocess.check_output(
        [go, "env", "GOROOT"], text=True, env=environment
    ).strip()
    notices = stage / "runtime" / "go-notices"
    notices.mkdir()
    for name in ("LICENSE", "PATENTS"):
        shutil.copy2(Path(goroot) / name, notices / name)
    binary_data = binary.read_bytes()
    (pe,) = struct.unpack_from("<I", binary_data, 0x3C)
    (machine,) = struct.unpack_from("<H", binary_data, pe + 4)
    (subsystem,) = struct.unpack_from("<H", binary_data, pe + 24 + 68)
    if binary_data[pe : pe + 4] != b"PE\0\0" or machine != 0x8664 or subsystem != 2:
        raise ValueError("Expected x64 Windows GUI executable")


def pack_program(stage, target):
    """Append the compressed runtime and application as one verified PE overlay."""
    files = []
    for base in (stage, PROJECT):
        for path in base.rglob("*"):
            rel = path.relative_to(base)
            if not path.is_file() or any(
                part.startswith(".") or part == "__pycache__" for part in rel.parts
            ):
                continue
            if rel.as_posix() == "BlockHarbor.exe" or path.suffix == ".pyc":
                continue
            files.append((rel.as_posix(), path))
    names = [name.casefold() for name, _ in files]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate payload member")
    with tempfile.TemporaryFile() as packed:
        with ZipFile(packed, "w") as archive:
            for name, path in sorted(files):
                info = ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes(), compresslevel=8)
        size = packed.tell()
        packed.seek(0)
        digest = sha256()
        with target.open("xb") as final, (stage / "BlockHarbor.exe").open("rb") as stub:
            shutil.copyfileobj(stub, final)
            while chunk := packed.read(1024 * 1024):
                digest.update(chunk)
                final.write(chunk)
            final.write(
                struct.pack("<Q32s16s", size, digest.digest(), b"BlockHarborPKG18")
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go", help="Path to the Go compiler")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(tempfile.gettempdir()) / "blockharbor-build-cache",
    )
    parser.add_argument(
        "--output", type=Path, default=PROJECT.parent / "BlockHarbor-Windows-x64"
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Emit only the unpacked runtime for development",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output == PROJECT or PROJECT in output.parents:
        raise SystemExit("Use an output directory outside the project.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise SystemExit("Choose a new or empty output directory: " + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        stage = Path(temp) / "distribution"
        stage.mkdir()
        install_archives(cache, stage)
        if not args.runtime_only:
            build_stub(args.go, stage)
            release = Path(temp) / "release"
            release.mkdir()
            pack_program(stage, release / "BlockHarbor.exe")
            stage = release
        if output.exists():
            output.rmdir()
        stage.rename(output)
    print(output)


if __name__ == "__main__":
    main()

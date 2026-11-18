"""Build the per-user BlockHarbor setup executable with NSIS 3.13+.

First build BlockHarbor.exe with build_windows.py, then run:
python tools/build_installer.py --launcher ../BlockHarbor-Windows-x64/BlockHarbor.exe
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

TOOLS = Path(__file__).resolve().parent
PROJECT = TOOLS.parent
MAGIC = b"BlockHarborPKG18"


def verify_launcher(path):
    data = path.read_bytes()
    if len(data) < 128 or data[:2] != b"MZ":
        raise ValueError(
            "Expected the standalone BlockHarbor.exe built by build_windows.py"
        )
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 96 > len(data) or data[pe : pe + 6] != b"PE\0\0\x64\x86":
        raise ValueError("Expected an x64 Windows launcher")
    size, digest, magic = struct.unpack("<Q32s16s", data[-56:])
    if magic != MAGIC or not 0 < size < len(data) - 56:
        raise ValueError("The launcher has no complete embedded package")
    if hashlib.sha256(data[-56 - size : -56]).digest() != digest:
        raise ValueError("The embedded launcher package failed SHA-256 verification")
    return len(data)


def nsis_literal(text, *, runtime=False):
    value = str(text)
    if any(c in value for c in "\r\n\0"):
        raise ValueError("NSIS values must be single-line strings")
    if runtime:
        value = value.replace("$", "$$")
    return value.replace('"', '$\\"')


def build_cleanup(go, output):
    """Compile the standalone uninstaller helper outside the deleted runtime.

    os.Root.RemoveAll requires Go 1.25+. It is deliberately built from its own
    dependency-free module; no downloaded toolchain or build hooks are used.
    """
    environment = dict(os.environ)
    environment.update(
        GOOS="windows",
        GOARCH="amd64",
        GOAMD64="v1",
        CGO_ENABLED="0",
        GOTOOLCHAIN="local",
        GOWORK="off",
        GOFLAGS="",
    )
    subprocess.run(
        [
            str(go),
            "build",
            "-trimpath",
            "-buildvcs=false",
            "-ldflags=-H=windowsgui -s -w -buildid=",
            "-o",
            str(output),
            ".",
        ],
        check=True,
        cwd=TOOLS / "data_cleanup",
        env=environment,
    )
    built = output.read_bytes()
    if len(built) < 128 or built[:2] != b"MZ":
        raise ValueError("The data cleanup helper is not a Windows executable")
    offset = struct.unpack_from("<I", built, 0x3C)[0]
    if built[offset : offset + 6] != b"PE\0\0\x64\x86":
        raise ValueError("The data cleanup helper must be a Windows x64 executable")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version", default="2.4.7")
    parser.add_argument("--makensis", default="makensis", help="NSIS 3.13+ compiler")
    parser.add_argument("--go", default="go", help="Go 1.25+ compiler for the safe data cleanup helper")
    parser.add_argument(
        "--nsisdir", type=Path, help="NSIS data directory for a portable compiler"
    )
    parser.add_argument("--project-url", default="", help="Public HTTPS project URL")
    args = parser.parse_args()
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}", args.version
    ):
        parser.error("--version must have three numeric parts, e.g. 2.4.7")
    if args.project_url:
        url = urlsplit(args.project_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            parser.error("--project-url must be a public HTTPS URL without credentials")
    launcher = args.launcher.resolve()
    size = verify_launcher(launcher)
    output = (
        args.output or PROJECT.parent / f"BlockHarbor-Setup-{args.version}.exe"
    ).resolve()
    if output == launcher or PROJECT == output.parent or PROJECT in output.parents:
        parser.error(
            "Choose an output file outside the project and different from the launcher"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    if args.nsisdir:
        environment["NSISDIR"] = str(args.nsisdir.resolve())
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        staged = Path(temp) / output.name
        cleanup = Path(temp) / "BlockHarborDataCleanup.exe"
        build_cleanup(args.go, cleanup)
        defines = {
            "APP_VERSION": args.version,
            "LAUNCHER_EXE": launcher,
            "CLEANUP_EXE": cleanup,
            "ICON_FILE": PROJECT / "ui/icon.ico",
            "OUT_FILE": staged,
            "PROJECT_URL": args.project_url,
            "APP_SIZE": (size + cleanup.stat().st_size + 1023) // 1024 + 256,
        }
        option = "/" if os.name == "nt" else "-"
        subprocess.run(
            [
                args.makensis,
                option + "V3",
                *(
                    option
                    + "D"
                    + key
                    + "="
                    + nsis_literal(value, runtime=key == "PROJECT_URL")
                    for key, value in defines.items()
                ),
                str(TOOLS / "installer_windows.nsi"),
            ],
            check=True,
            cwd=TOOLS,
            env=environment,
        )
        built = staged.read_bytes()
        offset = struct.unpack_from("<I", built, 0x3C)[0]
        if built[offset : offset + 6] != b"PE\0\0\x64\x86":
            raise ValueError("NSIS did not produce the requested Windows x64 setup")
        os.replace(staged, output)
    print(output)


if __name__ == "__main__":
    main()

"""Compile the small Java 8 e4steam addon without Gradle or Minecraft internals.

Provide the official e4steam-api 1.1.0 JAR. It is a compile-only dependency and
is never copied into the mod. The final ZIP has deterministic member order,
permissions and timestamps. --java also works with a JDK lacking a javac shim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_SHA256 = "33f3216bbc92d5761fc4063f102110f18a9ba8a8de8096adb0fe4284835c91c4"


def build(api, java, output):
    if hashlib.sha256(Path(api).read_bytes()).hexdigest() != API_SHA256:
        raise ValueError("e4steam-api 1.1.0 checksum does not match the pinned official artifact")
    with zipfile.ZipFile(api) as archive:
        for name in ("link/e4steam/api/session/SessionService.class", "link/e4steam/api/addon/E4steamAddonEntrypoint.class"):
            if name not in archive.namelist():
                raise ValueError("Provide the official e4steam-api 1.1.0 compile-only JAR")
    with tempfile.TemporaryDirectory(prefix="blockharbor-bridge-") as temporary:
        classes = Path(temporary) / "classes"
        classes.mkdir()
        source = ROOT / "bridge/src/blockharbor/bridge/RoomStatusAddon.java"
        # Some distro JDK runtimes include jdk.compiler without ct.sym. The addon
        # deliberately uses only Java 8 APIs, audited by its runtime contract test.
        subprocess.run([str(java), "com.sun.tools.javac.Main", "-source", "8", "-target", "8", "-g:none", "-encoding", "UTF-8",
                        "-Xlint:all,-options", "-Werror", "-cp", str(Path(api).resolve()), "-d", str(classes), str(source)], check=True)
        members = {path.relative_to(classes).as_posix(): path.read_bytes() for path in classes.rglob("*.class")}
        members["fabric.mod.json"] = (ROOT / "bridge/fabric.mod.json").read_bytes()
        members["META-INF/MANIFEST.MF"] = b"Manifest-Version: 1.0\r\nImplementation-Title: BlockHarbor Room Status\r\nImplementation-Version: 1.0.0\r\n\r\n"
        if (ROOT / "LICENSE").is_file():
            members["META-INF/LICENSE"] = (ROOT / "LICENSE").read_bytes()
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(members.items()):
                entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data, compresslevel=9)
        data = output.read_bytes()
        return {"name": output.name, "size": len(data), "sha512": hashlib.sha512(data).hexdigest(), "apiSha256": hashlib.sha256(Path(api).read_bytes()).hexdigest()}


def contract_test(api, java, artifact):
    with tempfile.TemporaryDirectory(prefix="blockharbor-bridge-test-") as temporary:
        root = Path(temporary)
        classes, game = root / "classes", root / "game"
        classes.mkdir()
        game.mkdir()
        classpath = os.pathsep.join((str(Path(api).resolve()), str(Path(artifact).resolve())))
        source = ROOT / "bridge/test/blockharbor/bridge/RoomStatusHarness.java"
        subprocess.run([str(java), "com.sun.tools.javac.Main", "-source", "8", "-target", "8", "-encoding", "UTF-8",
                        "-Xlint:all,-options", "-Werror", "-cp", classpath, "-d", str(classes), str(source)], check=True)
        subprocess.run([str(java), "-cp", classpath + os.pathsep + str(classes), "blockharbor.bridge.RoomStatusHarness", str(game)], check=True, timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", type=Path, required=True, help="Official e4steam-api 1.1.0 JAR (compile only)")
    parser.add_argument("--java", default=shutil.which("java"), help="A JDK java executable with the jdk.compiler module")
    parser.add_argument("--output", type=Path, default=ROOT / "bundled/bridge/blockharbor-room-status.jar")
    parser.add_argument("--test", action="store_true", help="Run the JVM contract harness against the official API")
    args = parser.parse_args()
    if not args.java:
        parser.error("Supply a JDK with --java")
    result = build(args.api, args.java, args.output)
    if args.test:
        contract_test(args.api, args.java, args.output)
    print(json.dumps(result, indent=2))

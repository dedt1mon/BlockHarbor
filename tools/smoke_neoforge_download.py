"""Opt-in real NeoForge download checks; never install or launch Minecraft/Java.

Run with the launcher's Python, including the Python inside an installed build:
  python tools/smoke_neoforge_download.py --root C:/AI/BlockHarbor-NeoForge-smoke

The isolated run downloads the exact official 21.1.267 installer, exercises a
real verified fallback with all primary requests blocked on an empty cache, then replays
the persistent cache in a fresh process with networking forbidden. No download
bytes or checksum responses are mocked. The parent enforces a wall-clock limit
even if a worker is stuck in DNS, TLS, or a socket read on Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launcher import preparation


VERSION = "21.1.267"
FILENAME = f"neoforge-{VERSION}-installer.jar"
PRIMARY = "https://maven.neoforged.net/releases/net/neoforged/neoforge/" + VERSION + "/" + FILENAME
FALLBACK_HOST = "neoforged.forgecdn.net"
# Independently verified against PRIMARY and its .sha1/.sha256 sidecars.
# Each smoke report records the UTC time of its live verification.
# A candidate mirror cannot replace these trust anchors.
EXPECTED_SIZE = 6992416
EXPECTED_SHA1 = "4565232f0bf231d0edfe512b949664dc5ff88fd8"
EXPECTED_SHA256 = "25bbe66e82205abeccdfd23bdac61e6a8e9ff1333885f3c9f31d392a5ec97e60"
MARKER = ".blockharbor-neoforge-download-smoke.json"
PURPOSE = {"format": 1, "purpose": "isolated-neoforge-download-smoke"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def owned_root(value):
    root = Path(value).resolve()
    marker = root / MARKER
    if root.exists() and any(root.iterdir()):
        require(marker.is_file() and not marker.is_symlink(), "Refusing a non-smoke data directory")
        require(marker.stat().st_size < 4096 and json.loads(marker.read_text("utf-8")) == PURPOSE,
                "Invalid smoke directory marker")
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(json.dumps(PURPOSE) + "\n", encoding="utf-8")
    return root


def validate_installer(path):
    require(path.is_file() and not path.is_symlink(), "Installer is missing or redirected")
    require(path.stat().st_size == EXPECTED_SIZE, "Installer size differs from the pinned official release")
    raw = path.read_bytes()
    require(hashlib.sha1(raw).hexdigest() == EXPECTED_SHA1, "Installer SHA-1 mismatch")
    require(hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256, "Installer SHA-256 mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.testzip() is None, "Installer ZIP integrity check failed")
        require(archive.getinfo("install_profile.json").file_size <= 1024 * 1024,
                "Installer profile exceeds its bound")
        profile = json.loads(archive.read("install_profile.json"))
        require(profile.get("minecraft") == "1.21.1" and profile.get("version") == "neoforge-" + VERSION,
                "Installer contains an unexpected game/loader profile")
    return {"size": len(raw), "sha1": EXPECTED_SHA1, "sha256": EXPECTED_SHA256}


def worker(phase, root):
    started = time.monotonic()
    events, failures, denied = [], [], []
    root = Path(root).resolve()
    cache = root / ("fallback-cache" if phase == "fallback" else "primary-cache")
    target = cache / ("vendor-replay" if phase == "cache" else "vendor-first") / FILENAME
    progress = preparation.Progress(lambda text: print(text, flush=True))
    downloader = preparation.Download(progress, cache=cache)

    def block_network(*_args, **_kwargs):
        denied.append("network-call")
        raise AssertionError("Persistent cache replay attempted a network operation")

    def traced(original, transport):
        def receive(url, output, *args, **kwargs):
            if phase == "fallback" and urlsplit(url).hostname == "maven.neoforged.net":
                failures.append(transport)
                raise TimeoutError("Smoke-injected primary origin timeout, including checksum requests")
            before = output.tell()
            headers, effective = original(url, output, *args, **kwargs)
            events.append({"transport": transport, "url": url, "effectiveUrl": effective,
                           "bytes": output.tell() - before})
            return headers, effective
        return receive

    try:
        with ExitStack() as stack:
            if phase == "cache":
                # These fail loudly rather than returning synthetic responses.
                # Also forbid lower-level connections to catch future adapters.
                for name in ("_receive", "_receive_requests", "metadata_response"):
                    stack.enter_context(patch.object(preparation, name, block_network))
                stack.enter_context(patch.object(socket, "create_connection", block_network))
                stack.enter_context(patch.object(socket, "getaddrinfo", block_network))
                stack.enter_context(patch.object(socket.socket, "connect", block_network))
            else:
                for name in ("_receive", "_receive_requests"):
                    original = getattr(preparation, name)
                    stack.enter_context(patch.object(preparation, name, traced(original, name)))

            if phase == "primary":
                checksum = preparation.metadata_response(PRIMARY + ".sha1").text.strip().lower()
                require(checksum == EXPECTED_SHA1, "Primary checksum differs from the pinned release")
            # No caller-supplied digest: the reviewed exact-version seed must
            # authorize a cold fallback even when the origin's sidecar is down.
            result = downloader(PRIMARY, target, sha1=None, minecraft_directory=cache)
            require(result is True, "A new vendor destination must be populated")
            verified = validate_installer(target)

            if phase == "primary":
                require(any(row["url"] == PRIMARY and row["bytes"] == EXPECTED_SIZE for row in events),
                        "Primary artifact was not observed; this phase does not certify its availability")
                require(any(row["url"] == PRIMARY + ".sha1" for row in events),
                        "No real primary checksum request was observed")
            elif phase == "fallback":
                require(bool(failures), "Primary transport fault was not exercised")
                require(any(urlsplit(row["url"]).hostname == FALLBACK_HOST
                            and row["bytes"] == EXPECTED_SIZE for row in events),
                        "No real ForgeCDN artifact transfer was observed")
            else:
                require(downloader(PRIMARY, target, minecraft_directory=cache) is False,
                        "Verified existing vendor file should be reused")
                # A valid ZIP is insufficient evidence of the correct installer.
                with zipfile.ZipFile(target, "w") as wrong:
                    wrong.writestr("not-the-installer.txt", "Deliberately damaged smoke destination")
                require(downloader(PRIMARY, target, minecraft_directory=cache) is True,
                        "Corrupt vendor target was not repaired from verified persistent cache")
                validate_installer(target)
                require(not denied, "Network was attempted during cache replay")

        return {"phase": phase, "ok": True, "seconds": round(time.monotonic() - started, 2),
                "verified": verified, "network": events, "injectedPrimaryTimeouts": len(failures),
                "networkDeniedAttempts": len(denied),
                "newVendorPathRestored": phase == "cache", "corruptVendorPathRepaired": phase == "cache"}
    finally:
        downloader.close()


def write_report(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Dedicated smoke directory; existing user data is refused")
    parser.add_argument("--timeout", type=float, default=180, help="Total wall-clock budget in seconds (30–600, default 180)")
    parser.add_argument("--phase", choices=("primary", "fallback", "cache"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 30 <= args.timeout <= 600:
        parser.error("--timeout must be between 30 and 600 seconds")

    if args.phase:
        try:
            result = worker(args.phase, args.root)
        except Exception as error:
            result = {"phase": args.phase, "ok": False, "error": f"{type(error).__name__}: {error}"}
        write_report(args.root / (args.phase + ".json"), result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["ok"] else 1

    root = owned_root(args.root)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    started = time.monotonic()
    deadline = started + args.timeout
    report = {"format": 1, "started": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "python": platform.python_version(),
              "version": VERSION, "primary": PRIMARY, "checksumSource": PRIMARY + ".sha1",
              "runDirectory": str(run), "timeoutSeconds": args.timeout,
              "minecraftLaunched": False, "javaLaunched": False, "phases": [], "ok": False}
    try:
        for phase in ("primary", "fallback", "cache"):
            remaining = deadline - time.monotonic()
            require(remaining > 0, "Total smoke deadline reached")
            command = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
                       "--root", str(run), "--phase", phase]
            print("NeoForge download smoke: " + phase, flush=True)
            try:
                completed = subprocess.run(command, cwd=run, capture_output=True, text=True,
                                           encoding="utf-8", errors="replace",
                                           timeout=min(20 if phase == "cache" else 85, remaining), check=False)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(f"{phase}: worker exceeded its bounded wall-clock budget") from error
            result_path = run / (phase + ".json")
            if not result_path.is_file():
                detail = (completed.stderr or completed.stdout)[-3000:]
                raise RuntimeError(f"{phase}: worker returned no report: {detail}")
            result = json.loads(result_path.read_text("utf-8"))
            report["phases"].append(result)
            require(completed.returncode == 0 and result.get("ok") is True,
                    f"{phase}: {result.get('error', 'worker failed')}")
            print(f"  PASS ({result['seconds']}s)", flush=True)
        report["ok"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        report["seconds"] = round(time.monotonic() - started, 2)
        write_report(root / "report.json", report)
        print("Report: " + str(root / "report.json"), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

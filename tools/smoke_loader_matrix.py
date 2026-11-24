"""Opt-in real Minecraft/loader installation and startup checks in isolated data.

Example (downloads game files; no Microsoft account or user data is used):
  python tools/smoke_loader_matrix.py --root C:/AI/BlockHarbor-runtime-audit --launch

An alive process is reported as startup observation, never as verified gameplay.
Windows packaging/DPI and multiplayer tests remain separate checks.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launcher import compatibility, game, lifecycle
from launcher.store import Store

DEFAULT_CASES = (
    "vanilla:1.20.1", "vanilla:26.3", "fabric:1.20.1", "fabric:26.3",
    "forge:1.20.1", "forge:1.21.11", "neoforge:1.21.1", "neoforge:26.3",
    "quilt:1.20.1", "quilt:26.3",
)
MARKER = ".blockharbor-runtime-audit.json"


def case_value(value):
    loader, separator, version = value.partition(":")
    if not separator:
        raise argparse.ArgumentTypeError("Use loader:minecraft-version")
    try:
        game._check(version, loader)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return loader, version


def prepare_root(root):
    root = root.resolve()
    marker = root / MARKER
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError("Refusing to use an existing non-audit data directory")
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(json.dumps({"format": 1, "purpose": "isolated-runtime-audit"}), encoding="utf-8")
    if marker.is_symlink() or json.loads(marker.read_text(encoding="utf-8")) != {"format": 1, "purpose": "isolated-runtime-audit"}:
        raise ValueError("Invalid audit directory marker")
    return root


def timed_install(version, loader, cache, folder, status, seconds):
    scope = lifecycle.CancellationScope()
    timer = threading.Timer(seconds, scope.cancel)
    timer.daemon = True
    timer.start()
    try:
        with lifecycle.bind(scope):
            return game.install(version, loader, cache, folder, status)
    finally:
        timer.cancel()


def observe_launch(cache, folder, seconds, status, capture=None):
    started = time.monotonic()
    process = None
    try:
        process = game.launch_local(cache, folder, "BHAuditPlayer", 2048, status)
        deadline = time.monotonic() + seconds
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.25)
        if process.poll() is not None:
            return {"state": "exited", "exitCode": process.returncode, "seconds": round(time.monotonic() - started, 2)}
        result = {"state": "running-observed", "seconds": round(time.monotonic() - started, 2), "gameplayVerified": False}
        if capture is not None:
            try:
                from PIL import ImageGrab
                ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).save(capture)
                result["displayCapture"] = capture.name
            except Exception as error:
                result["displayCaptureError"] = type(error).__name__
        return result
    except Exception as error:
        return {"state": "failed", "error": f"{type(error).__name__}: {error}", "seconds": round(time.monotonic() - started, 2)}
    finally:
        # Only the exact process created above belongs to this test.
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def run_case(store, loader, version, args):
    cache = store.root / "game-cache"
    label = loader + "-" + version
    output = store.root / "audit-results" / label
    if output.exists():
        history = store.root / "audit-history" / (label + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        history.parent.mkdir(parents=True, exist_ok=True)
        output.rename(history)
    output.mkdir(parents=True, exist_ok=True)
    result = {"loader": loader, "minecraft": version, "started": datetime.now(timezone.utc).isoformat(), "created": False, "install": "not-run", "repeatInstall": "not-run", "launches": [], "deletion": "not-run"}
    pid = store.create("Проверка " + loader + " " + version, version, loader)
    result["created"] = True
    folder = store.folder(pid)
    began = time.monotonic()
    last = [0.0]
    def status(message):
        with (output / "preparation.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.monotonic() - began:.2f} {message}\n")
        if time.monotonic() - last[0] >= 5:
            print(f"[{loader} {version}] {message}", flush=True)
            last[0] = time.monotonic()
    try:
        if args.interrupt_first:
            try:
                timed_install(version, loader, cache, folder, status, args.interrupt_first)
                result["interruption"] = "finished-before-cancellation"
            except lifecycle.OperationCancelled:
                result["interruption"] = "cancelled-and-will-retry"
        result["id"] = timed_install(version, loader, cache, folder, status, args.timeout)
        result["install"] = "passed"
        context = compatibility.installation_profile(store, pid)
        result["platform"] = {key: context.get(key) for key in ("loader_version", "java_major")}
        started = time.monotonic()
        timed_install(version, loader, cache, folder, status, args.timeout)
        result["repeatInstall"] = "passed"
        result["repeatSeconds"] = round(time.monotonic() - started, 3)
        if args.launch:
            for index in range(2):
                capture = output / f"launch-{index + 1}.png" if args.capture_display else None
                result["launches"].append(observe_launch(cache, folder, args.observe_seconds, status, capture))
        else:
            result["launches"] = [{"state": "not-requested", "gameplayVerified": False}]
        logs = folder / "logs"
        if logs.is_dir():
            shutil.copytree(logs, output / "game-logs", dirs_exist_ok=True)
        crashes = folder / "crash-reports"
        if crashes.is_dir():
            shutil.copytree(crashes, output / "crash-reports", dirs_exist_ok=True)
        store.move_profile(pid)
        store.purge_profile(pid)
        result["deletion"] = "passed" if not folder.exists() else "failed"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        cause, seen = error, set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, subprocess.CalledProcessError):
                chunks = []
                for stream in (cause.stdout, cause.stderr):
                    if isinstance(stream, bytes):
                        stream = stream[-65536:].decode("utf-8", errors="replace")
                    if isinstance(stream, str):
                        chunks.append(stream[-65536:])
                # JVM option echoes may disclose values from the caller's
                # environment. Keep only installer output and strip URL users.
                detail = "\n".join(line for line in "\n".join(chunks).splitlines()
                                   if "Picked up " not in line)
                detail = re.sub(r"(https?://)[^\s/@]+@", r"\1[redacted]@", detail)
                (output / "installer-error.log").write_text(detail, encoding="utf-8")
                result["installerDiagnostic"] = "installer-error.log"
                break
            cause = cause.__cause__ or cause.__context__
        if result["install"] == "not-run":
            result["install"] = "failed"
        elif result["repeatInstall"] == "not-run":
            result["repeatInstall"] = "failed"
    result["seconds"] = round(time.monotonic() - began, 2)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="New or previously marked isolated audit directory")
    parser.add_argument("--case", type=case_value, action="append", help="loader:version; repeat to select a matrix")
    parser.add_argument("--launch", action="store_true", help="Actually start each game twice with a temporary local nickname")
    parser.add_argument("--capture-display", action="store_true", help="Capture the whole isolated Linux test display after startup; never use on a personal desktop")
    parser.add_argument("--observe-seconds", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=1200, help="Bound each installation operation, in seconds")
    parser.add_argument("--interrupt-first", type=float, default=0, help="Cancel the first preparation after this many seconds, then recover")
    args = parser.parse_args()
    if not 1 <= args.observe_seconds <= 300 or not 5 <= args.timeout <= 3600 or not 0 <= args.interrupt_first <= 60:
        parser.error("Observation 1–300s, timeout 5–3600s, interruption 0–60s")
    if args.capture_display and (sys.platform != "linux" or not os.environ.get("DISPLAY") or not args.launch):
        parser.error("--capture-display needs --launch and an isolated Linux X display")
    store = Store(prepare_root(args.root))
    report = {"started": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(), "python": platform.python_version(), "displayAvailable": sys.platform == "win32" or bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")), "externalJavaOptions": bool(os.environ.get("JDK_JAVA_OPTIONS") or os.environ.get("JAVA_TOOL_OPTIONS")), "gameplayVerified": False, "cases": []}
    previous = store.root / "audit-results.json"
    if previous.exists():
        history = store.root / "audit-history"
        history.mkdir(exist_ok=True)
        previous.rename(history / ("matrix-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"))
    for loader, version in args.case or map(case_value, DEFAULT_CASES):
        result = run_case(store, loader, version, args)
        report["cases"].append(result)
        (store.root / "audit-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    failed = any(row["install"] != "passed" or row["repeatInstall"] != "passed" or row["deletion"] != "passed" or any(launch["state"] in {"failed", "exited"} for launch in row["launches"]) for row in report["cases"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

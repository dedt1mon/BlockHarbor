"""Real NeoForge preparation and ZIP transfer, without launching a game session.

Run only against a dedicated smoke directory. The first phase imports an old
ordinary ZIP and prepares Minecraft/managed Java and NeoForge 21.1.267 from an
empty cache while both NeoForge Maven and ForgeCDN are unavailable. Shipped
verified inputs must avoid both origins entirely. The second imports an actual ZIP
into a separate store that has only officially acquired Minecraft/Java inputs,
then installs NeoForge with network access forbidden. A fresh process verifies the resulting completed
installation without running its installer or making a network request.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
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

from launcher import build_archive, bundled_loaders, game, lifecycle, loader_files, neoforge_install, preparation
from launcher.store import Store
from launcher.version import VERSION as LAUNCHER_VERSION

MINECRAFT = "1.21.1"
NEOFORGE = "21.1.267"
INSTALLED = "neoforge-" + NEOFORGE
MARKER = ".blockharbor-neoforge-install-smoke.json"
PURPOSE = {"format": 1, "purpose": "isolated-neoforge-install-smoke"}
BLOCKED_ORIGINS = {"maven.neoforged.net", "neoforged.forgecdn.net"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def owned_root(value):
    root = Path(value).resolve()
    marker = root / MARKER
    if root.exists() and any(root.iterdir()):
        require(marker.is_file() and not marker.is_symlink(), "Refusing a non-smoke directory")
        require(marker.stat().st_size < 4096 and json.loads(marker.read_text("utf-8")) == PURPOSE,
                "Invalid smoke directory marker")
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(json.dumps(PURPOSE) + "\n", encoding="utf-8")
    return root


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clone_vanilla(source, destination):
    """Simulate prepared official Minecraft inputs, never copy NeoForge state."""
    source, destination = Path(source), Path(destination)
    for name in ("assets", "runtime"):
        if (source / name).is_dir():
            shutil.copytree(source / name, destination / name)
    metadata_path = game._manifest_path(source, MINECRAFT)
    metadata = json.loads(metadata_path.read_bytes())
    profile = game._manifest_path(destination, MINECRAFT).parent
    shutil.copytree(metadata_path.parent, profile)
    for path, entry in game._library_files(source, metadata):
        require(game._verified_install_file(path, entry), "Unverified vanilla input: " + path.name)
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Some installer inputs are different versions of Mojang-hosted libraries.
    # They deliberately are not redistributed in the ZIP. Preseed only these
    # official inputs here so that the test can prohibit every network call.
    # A real clean recipient obtains these, Minecraft and Java from Mojang.
    installer, _ = loader_files._installer_paths(source, loader_files.installer_url(NEOFORGE))
    for entry in neoforge_install.installer_artifacts(installer, MINECRAFT, NEOFORGE):
        if urlsplit(entry["url"]).hostname != "libraries.minecraft.net":
            continue
        path = source / "libraries" / entry["path"]
        require(game._verified_install_file(path, entry), "Unverified official library: " + path.name)
        target = destination / "libraries" / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # These mappings came from Mojang, not the transferred ZIP. Copy the exact
    # digest-checked input so the second phase can forbid all network access.
    mapping = source / "installer-inputs" / MINECRAFT / "client_mappings.txt"
    require(game._verified_install_file(mapping, metadata["downloads"]["client_mappings"]),
            "Official mappings input was not preserved")
    target = destination / mapping.relative_to(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mapping, target)
    require(not game._manifest_path(destination, INSTALLED).exists(), "Copied a loader profile")
    require(not (destination / "installer-cache").exists(), "Copied an installer cache")


def worker(phase, root):
    root = Path(root).resolve()
    started = time.monotonic()
    store = Store(root / ("sender" if phase == "cold" else "recipient"))
    cache = store.root / "game-cache"
    trace, forbidden, java_runs, bundled_copies = [], [], [], []
    source_document_count = 0
    status_log = root / (phase + ".log")
    last_status = ["", 0.0]
    log = status_log.open("w", encoding="utf-8", buffering=1)

    def status(message):
        message = str(message)
        log.write(message + "\n")
        # Preserve every real message in the report log without flooding CI.
        now = time.monotonic()
        if now - last_status[1] > 5 or message.startswith(("Резервный", "Настраиваем", "Устанавливаем")):
            print(message, flush=True)
            last_status[:] = [message, now]

    if phase == "cold":
        require(not cache.exists() or not any(cache.iterdir()), "Cold test cache is not empty")
        shipped = bundled_loaders.entries()
        require(len(shipped) == 52, "Expected the complete pinned NeoForge runtime bundle")
        for entry in shipped:
            require(bundled_loaders.verified_source(entry["url"], entry["sha1"]) is not None,
                    "Missing shipped component: " + entry["path"])
        # A recipient must recover an existing ordinary ZIP. Re-exporting the
        # sender's build must not be a prerequisite for fixing this failure.
        legacy_zip = io.BytesIO()
        with zipfile.ZipFile(legacy_zip, "w") as archive:
            archive.writestr(build_archive.MANIFEST, json.dumps({
                "format": "BlockHarbor", "formatVersion": 1,
                "name": "Старый ZIP NeoForge", "minecraft": MINECRAFT,
                "loader": "neoforge", "loaderVersion": NEOFORGE,
            }, ensure_ascii=False))
        legacy_zip.seek(0)
        imported = build_archive.import_build(store, legacy_zip)
        pid = imported["id"]
        require(not (store.folder(pid) / loader_files.STAGED).exists(),
                "Ordinary ZIP unexpectedly contains loader files")
        write_json(root / "sender.json", {"id": pid})
    elif phase == "transfer":
        sender = Store(root / "sender")
        sender_id = json.loads((root / "sender.json").read_bytes())["id"]
        archive_path = root / "prepared-build.zip"
        with archive_path.open("w+b") as archive:
            build_archive.export_build(sender, sender_id, archive, include_loader_files=True)
            with zipfile.ZipFile(archive) as bundle:
                metadata = json.loads(bundle.read(build_archive.MANIFEST))
                require(metadata.get("formatVersion") == 3 and metadata.get("loaderFiles") is True,
                        "ZIP did not include verified loader inputs")
                loader_manifest = json.loads(bundle.read(loader_files.SECTION + "/" + loader_files.MANIFEST))
                documents = loader_manifest.get("documents", [])
                expected_sources = bundled_loaders.distribution_sources()
                require(bool(expected_sources) and
                        {item["sha256"] for item in documents} ==
                        {item["sha256"] for item in expected_sources},
                        "ZIP did not carry all matching source archives and notices")
                source_document_count = len(documents)
            archive.seek(0)
            imported = build_archive.import_build(store, archive)
        pid = imported["id"]
        clone_vanilla(sender.root / "game-cache", cache)
        write_json(root / "recipient.json", {"id": pid})
    else:
        pid = json.loads((root / "recipient.json").read_bytes())["id"]

    def denied(*_args, **_kwargs):
        forbidden.append("network")
        raise AssertionError("Unexpected network operation in " + phase)

    def traced(original, transport):
        def receive(url, output, *args, **kwargs):
            host = urlsplit(url).hostname
            if host in BLOCKED_ORIGINS:
                forbidden.append({"host": host, "transport": transport})
                raise AssertionError("Bundled preparation attempted the blocked NeoForge origin: " + host)
            before = output.tell()
            response = original(url, output, *args, **kwargs)
            trace.append({"host": host, "transport": transport,
                          "file": urlsplit(url).path.rsplit("/", 1)[-1],
                          "bytes": max(0, output.tell() - before)})
            return response
        return receive

    original_run = lifecycle.run
    original_copy = bundled_loaders.copy_file
    original_lookup = socket.getaddrinfo

    def tracked_copy(url, target, sha1):
        result = original_copy(url, target, sha1)
        if result:
            bundled_copies.append(url)
        return result

    def checked_lookup(host, *args, **kwargs):
        normalized = host.decode("ascii") if isinstance(host, bytes) else host
        if normalized in BLOCKED_ORIGINS:
            forbidden.append({"host": normalized, "transport": "direct-dns"})
            raise AssertionError("Direct connection to a blocked NeoForge origin")
        return original_lookup(host, *args, **kwargs)

    # Reserve an unlistened local port. Actual Java HTTP/HTTPS connections must
    # fail too, independently of Python's network interception and --offline.
    java_proxy = socket.socket()
    java_proxy.bind(("127.0.0.1", 0))
    proxy_port = str(java_proxy.getsockname()[1])

    def tracked_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "net.minecraftforge.installer.SimpleInstaller" in command:
            require("--offline" in command, "NeoForge Java installer still has network access enabled")
            require(phase != "replay", "Completed receipt unexpectedly reran the installer")
            command = [argument for argument in command
                       if not str(argument).startswith("-Djava.net.useSystemProxies=")]
            command[1:1] = ["-Djava.net.useSystemProxies=false",
                            "-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=" + proxy_port,
                            "-Dhttps.proxyHost=127.0.0.1", "-Dhttps.proxyPort=" + proxy_port,
                            "-Dhttp.nonProxyHosts=", "-Dhttps.nonProxyHosts="]
            java_runs.append({"offline": True, "closedHttpHttpsProxy": True})
        return original_run(command, *args, **kwargs)

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(lifecycle, "run", side_effect=tracked_run))
            stack.enter_context(patch.object(bundled_loaders, "copy_file", side_effect=tracked_copy))
            if phase == "cold":
                stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=checked_lookup))
                for name in ("_receive", "_receive_requests"):
                    stack.enter_context(patch.object(preparation, name, traced(getattr(preparation, name), name)))
            else:
                for name in ("_receive", "_receive_requests", "metadata_response"):
                    stack.enter_context(patch.object(preparation, name, side_effect=denied))
                for name in ("create_connection", "getaddrinfo"):
                    stack.enter_context(patch.object(socket, name, side_effect=denied))
                stack.enter_context(patch.object(socket.socket, "connect", side_effect=denied))
            installed = game.install(MINECRAFT, "neoforge", cache, store.folder(pid), status)
            require(installed == INSTALLED, "Prepared an unexpected loader version")
            require(game._missing_install_file(cache, MINECRAFT, installed) is None,
                    "Preparation left a missing launch dependency")
            require(game._completed_loader(cache, MINECRAFT, "neoforge", NEOFORGE, installed),
                    "Completed installation receipt was not written")
            if phase == "cold":
                require(not forbidden, "Old ZIP still attempted NeoForge Maven or ForgeCDN")
                require(loader_files.installer_url(NEOFORGE) in bundled_copies,
                        "Cold installer was not copied from the actual shipped component")
                require(len(set(bundled_copies)) > 1,
                        "Cold preparation did not consume shipped loader libraries")
                require(len(java_runs) == 1, "The actual NeoForge installer did not run exactly once")
            elif phase == "transfer":
                require(len(java_runs) == 1, "ZIP import did not prepare the loader on the recipient")
                require(not forbidden, "Transferred inputs still required a network request")
            else:
                require(not java_runs and not forbidden, "Completed cache replay was not local")
        return {"phase": phase, "ok": True, "seconds": round(time.monotonic() - started, 2),
                "installed": installed, "blockedOrigins": sorted(BLOCKED_ORIGINS),
                "networkDeniedAttempts": len(forbidden), "javaInstallers": java_runs,
                "bundledFilesUsed": len(set(bundled_copies)),
                "bundledFilesAvailable": len(shipped) if phase == "cold" else None,
                "sourceDocumentsTransferred": source_document_count,
                "legacyZipWithoutLoaderFiles": phase == "cold",
                "officialMinecraftInputsPreseeded": phase == "transfer",
                "network": trace, "minecraftLaunched": False}
    finally:
        java_proxy.close()
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1500)
    parser.add_argument("--phase", choices=("cold", "transfer", "replay"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 120 <= args.timeout <= 2400:
        parser.error("--timeout must be between 120 and 2400 seconds")
    if args.phase:
        try:
            result = worker(args.phase, args.root)
        except Exception as error:
            result = {"phase": args.phase, "ok": False, "error": f"{type(error).__name__}: {error}"}
            # Full local traceback helps resolve a failed CI gate without ever
            # running against a user's data or including credentials.
            import traceback
            traceback.print_exc()
        write_json(args.root / (args.phase + ".json"), result)
        print(json.dumps({key: value for key, value in result.items() if key != "network"}, ensure_ascii=False), flush=True)
        return 0 if result["ok"] else 1
    root = owned_root(args.root)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    started = time.monotonic()
    deadline = started + args.timeout
    report = {"format": 1, "started": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "python": platform.python_version(),
              "launcher": LAUNCHER_VERSION, "minecraft": MINECRAFT, "neoforge": NEOFORGE,
              "runDirectory": str(run), "timeoutSeconds": args.timeout,
              "minecraftLaunched": False, "phases": [], "ok": False}
    try:
        for phase in ("cold", "transfer", "replay"):
            remaining = deadline - time.monotonic()
            require(remaining > 0, "Full installation smoke deadline reached")
            print("NeoForge full preparation smoke: " + phase, flush=True)
            command = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
                       "--root", str(run), "--phase", phase]
            # Inherit stdout so CI shows continued progress during real asset
            # downloads and Java processing. Each child writes a small report.
            completed = subprocess.run(command, cwd=run, timeout=min(120 if phase == "replay" else 1200, remaining),
                                       check=False)
            result_path = run / (phase + ".json")
            require(result_path.is_file(), phase + " returned no report")
            result = json.loads(result_path.read_bytes())
            report["phases"].append(result)
            require(completed.returncode == 0 and result.get("ok") is True,
                    phase + ": " + result.get("error", "worker failed"))
            print(f"  PASS ({result['seconds']}s)", flush=True)
        report["ok"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        report["seconds"] = round(time.monotonic() - started, 2)
        write_json(root / "report.json", report)
        print("Report: " + str(root / "report.json"), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

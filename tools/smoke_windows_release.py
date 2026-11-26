"""Real installer/packaged application smoke test on an ephemeral Windows CI runner."""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

APP_REG = r"Software\BlockHarbor"
UNINSTALL_REG = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BlockHarbor"


def ensure_ci():
    if (sys.platform != "win32" or os.getenv("GITHUB_ACTIONS") != "true"
            or os.getenv("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.getenv("GITHUB_REPOSITORY") != "dedt1mon/BlockHarbor"):
        raise RuntimeError("Run only on a clean official hosted Windows runner")
    for key in (APP_REG, UNINSTALL_REG):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                raise RuntimeError("Existing install: refuse to change a real installation")
        except FileNotFoundError:
            pass


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install(setup, folder, *, upgrade=False):
    command = subprocess.list2cmdline([str(setup), "/S"] + (["/AUTOUPDATE"] if upgrade else []))
    # NSIS requires /D to be last and its value unquoted even if it has spaces.
    subprocess.run(command + " /D=" + str(folder), check=True, timeout=150)


def check_registry(path, version):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG, 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        assert winreg.QueryValueEx(key, "DisplayVersion")[0] == version
        assert Path(winreg.QueryValueEx(key, "InstallLocation")[0]).resolve() == path.resolve()


def test_runtime(installed, root, version):
    local = root / "local"
    local.mkdir()
    data = root / "data"
    environment = dict(os.environ, LOCALAPPDATA=str(local))
    # Hosted runners have development tools installed. Exercise the packaged
    # launcher with only the Windows system paths so a successful start cannot
    # depend on Python, Go, or Java from the runner's toolchain PATH/environment.
    windows = Path(os.environ["SystemRoot"])
    environment["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    for variable in ("JAVA_HOME", "JDK_HOME", "PYTHONHOME", "PYTHONPATH",
                     "PYTHONSTARTUP", "VIRTUAL_ENV", "CONDA_PREFIX", "GOROOT", "GOPATH"):
        environment.pop(variable, None)
    process = subprocess.Popen([str(installed), "--no-browser", "--data", str(data)],
                               env=environment)
    try:
        log = local / "BlockHarbor" / "logs" / "startup.log"
        deadline = time.monotonic() + 90
        match = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Packaged launcher exited before serving its UI")
            if log.is_file():
                match = re.search(r"(http://127\.0\.0\.1:\d+/)\#([A-Za-z0-9_-]{32,128})",
                                  log.read_text(encoding="utf-8", errors="replace"))
                if match:
                    break
            time.sleep(0.25)
        if not match:
            raise RuntimeError("Packaged launcher did not expose its loopback endpoint")
        base, token = match.groups()
        opener = build_opener(ProxyHandler({}))

        def call(path, data=None, *, auth=True, raw=False):
            headers = {"Authorization": "Bearer " + token} if auth else {}
            if data is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            with opener.open(Request(base + path, data=data, headers=headers), timeout=15) as response:
                result = response.read()
                return result if raw else json.loads(result)

        state = call("api/state")
        assert state["appVersion"] == version, state.get("appVersion")
        assert state["capabilities"]["communitySharing"] is False
        try:
            call("api/state", auth=False)
        except HTTPError as exc:
            assert exc.code == 403
            exc.close()
        else:
            raise AssertionError("Local API permitted an unauthenticated state request")
        call("api/action", {"action": "create", "name": "Тестовая сборка",
                            "version": "1.20.1", "loader": "vanilla",
                            "allowWithoutSteam": True})
        state = call("api/state")
        profile = next(x for x in state["profiles"] if x["name"] == "Тестовая сборка")
        for action in ("careState", "careWorlds", "experienceState", "performanceState",
                       "shareRoomState", "diagnostics", "updateState"):
            result = call("api/feature?action=" + action + "&id=" + profile["id"])
            assert isinstance(result, dict), action
        try:
            call("api/feature?action=communityPublication&id=" + profile["id"])
        except HTTPError as exc:
            assert exc.code == 400
            assert "временно отключена" in exc.read().decode("utf-8")
            exc.close()
        else:
            raise AssertionError("Packaged launcher permitted paused community publication")
        print("PASS: packaged runtime keeps community publication paused")
        for script in ("app.js", "window.js", "pack-hub.js", "updates.js", "build-care.js"):
            assert len(call(script, auth=False, raw=True)) > 100, script
        print("PASS: packaged runtime with isolated system PATH, local API, Russian profile, feature reads, static UI, authentication")
        # Exercise the reported failing download with the interpreter, libraries
        # and production downloader extracted by this exact release EXE. The
        # live smoke owns a separate temporary cache and never starts Minecraft.
        with installed.open("rb") as source:
            source.seek(-56, os.SEEK_END)
            payload_size, payload_hash, magic = struct.unpack("<Q32s16s", source.read(56))
        assert magic == b"BlockHarborPKG18" and 0 < payload_size < installed.stat().st_size - 56
        # provision() scopes the current pointer by payload digest, so another
        # installed release cannot supply the interpreter for this smoke.
        app_cache = local / "BlockHarbor" / "app" / ("2.0-" + payload_hash.hex()[:16])
        generation = (app_cache / "current").read_text(encoding="utf-8")
        assert re.fullmatch(r"files-[A-Za-z0-9_-]+", generation), generation
        packaged = (app_cache / generation).resolve()
        assert packaged.is_relative_to(app_cache.resolve())
        receipt = json.loads((packaged / ".installed.json").read_text(encoding="utf-8"))
        assert receipt["Package"] == payload_hash.hex()
        subprocess.run([
            str(packaged / "runtime" / "python.exe"), "-B", "-X", "utf8",
            str(packaged / "tools" / "smoke_neoforge_download.py"),
            "--root", str(root / "neoforge-download"), "--timeout", "180",
        ], check=True, timeout=210, cwd=packaged, env=environment)
        download_report = json.loads((root / "neoforge-download" / "report.json").read_text(encoding="utf-8"))
        assert download_report["ok"] and len(download_report["phases"]) == 3
        primary, fallback, replay = download_report["phases"]
        assert primary["verified"] == fallback["verified"] == replay["verified"]
        assert fallback["injectedPrimaryTimeouts"] >= 2 and replay["networkDeniedAttempts"] == 0
        print("PASS: NeoForge installer SHA-256 " + primary["verified"]["sha256"])
        print("PASS: actual release payload downloads NeoForge 21.1.267, verifies fallback and reuses its cache offline")
        # The original failure can also occur after installer.jar has arrived.
        # Import an ordinary old ZIP with both NeoForge origins unavailable,
        # then transfer its prepared ZIP to another isolated store.
        smoke_root = root / "neoforge-install"
        reports = Path.cwd() / "artifacts" / "windows-release-smoke"
        reports.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([
                str(packaged / "runtime" / "python.exe"), "-B", "-X", "utf8",
                str(packaged / "tools" / "smoke_neoforge_install.py"),
                "--root", str(smoke_root), "--timeout", "1500",
            ], check=True, timeout=1530, cwd=packaged, env=environment)
        finally:
            if (smoke_root / "report.json").is_file():
                shutil.copy2(smoke_root / "report.json", reports / "report.json")
            # Keep small diagnostics even on failure, never upload the game
            # cache or downloaded third-party binaries as a workflow artifact.
            for run in smoke_root.glob("run-*"):
                target = reports / run.name
                target.mkdir(exist_ok=True)
                for name in ("cold.json", "transfer.json", "replay.json", "cold.log", "transfer.log", "replay.log"):
                    source = run / name
                    if source.is_file() and source.stat().st_size <= 16 * 1024**2:
                        shutil.copy2(source, target / name)
        full_report = json.loads((smoke_root / "report.json").read_bytes())
        assert full_report["ok"] and len(full_report["phases"]) == 3
        assert all(row["ok"] for row in full_report["phases"])
        cold = full_report["phases"][0]
        assert cold["legacyZipWithoutLoaderFiles"] and cold["bundledFilesAvailable"] == 52
        assert cold["bundledFilesUsed"] > 1
        assert cold["blockedOrigins"] == ["maven.neoforged.net", "neoforged.forgecdn.net"]
        assert cold["networkDeniedAttempts"] == 0
        assert full_report["phases"][1]["networkDeniedAttempts"] == 0
        assert full_report["phases"][1]["sourceDocumentsTransferred"] == 34
        assert full_report["phases"][2]["networkDeniedAttempts"] == 0
        print("PASS: actual release imports an old ZIP with both NeoForge origins blocked, verifies 52 shipped files, transfers prepared inputs and replays offline")
    finally:
        if process.poll() is None:
            # This is our isolated CI test process tree only.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           timeout=15, check=False, stdout=subprocess.DEVNULL)
            process.wait(timeout=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--portable", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    ensure_ci()
    setup, portable = args.setup.resolve(), args.portable.resolve()
    with tempfile.TemporaryDirectory(prefix="bh-release-smoke-", dir=os.environ["RUNNER_TEMP"]) as tmp:
        root = Path(tmp)
        folder = root / "Проверка установки"
        install(setup, folder)
        installed = folder / "BlockHarbor.exe"
        uninstaller = folder / "Uninstall.exe"
        assert installed.is_file() and uninstaller.is_file()
        assert digest(installed) == digest(portable)
        check_registry(folder, args.version)
        print("PASS: actual silent NSIS installation to a Unicode path and registry metadata")
        keep = folder / "preserve-test.txt"
        keep.write_text("preserve", encoding="utf-8")
        install(setup, folder, upgrade=True)
        assert digest(installed) == digest(portable)
        assert keep.read_text(encoding="utf-8") == "preserve"
        check_registry(folder, args.version)
        print("PASS: actual silent upgrade preserves unrelated files")
        test_runtime(installed, root, args.version)
        subprocess.run([str(uninstaller), "/S"], check=True, timeout=90)
        deadline = time.monotonic() + 30
        while installed.exists() and time.monotonic() < deadline:
            time.sleep(0.25)
        assert not installed.exists(), "Uninstall did not remove launcher"
        assert keep.is_file(), "Uninstall removed unrelated file"
        print("PASS: actual silent uninstall preserves unrelated files")


if __name__ == "__main__":
    main()

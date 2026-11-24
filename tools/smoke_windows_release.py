"""Real installer/packaged application smoke test on an ephemeral Windows CI runner."""
import argparse
import hashlib
import json
import os
import re
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
                       "shareRoomState", "communityPublication", "diagnostics", "updateState"):
            result = call("api/feature?action=" + action + "&id=" + profile["id"])
            assert isinstance(result, dict), action
        for script in ("app.js", "window.js", "pack-hub.js", "updates.js", "build-care.js"):
            assert len(call(script, auth=False, raw=True)) > 100, script
        print("PASS: packaged runtime with isolated system PATH, local API, Russian profile, feature reads, static UI, authentication")
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

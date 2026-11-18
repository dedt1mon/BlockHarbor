"""Windows update handoff, launched as a separate embedded-Python process.

It opens process handles before announcing readiness, waits for the exact app
and bootstrap processes to exit, rechecks the file, then starts the installer.
No command shell, elevation, global process termination, or forced game exit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _write(path, value):
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate(path):
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("Некорректный запрос установки.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != 1 or not re.fullmatch(r"[a-f0-9]{24}", str(data.get("id", ""))):
        raise ValueError("Некорректный запрос установки.")
    if path.name != "handoff-" + data["id"] + ".json":
        raise ValueError("Неверный идентификатор установки.")
    version = data.get("version", "")
    if not re.fullmatch(r"\d{1,5}\.\d{1,5}\.\d{1,5}(?:-(?:alpha|beta|rc)\.\d{1,5})?", version):
        raise ValueError("Неверная версия установки.")
    root = path.parent.resolve()
    kind = data.get("kind")
    if kind == "installer":
        source = root / "installers" / ("BlockHarbor-Setup-" + version + ".exe")
    elif kind == "binary":
        source = root / "rollback" / ("BlockHarbor-" + version + ".exe")
    else:
        raise ValueError("Неизвестный способ установки.")
    if Path(data.get("file", "")) != source or source.is_symlink() or source.parent.is_symlink():
        raise ValueError("Файл установки перенаправлен.")
    ready = root / ("ready-" + data["id"] + ".json")
    if Path(data.get("ready", "")) != ready or ready.is_symlink():
        raise ValueError("Файл ответа перенаправлен.")
    if type(data.get("automatic", False)) is not bool:
        raise ValueError("Некорректный режим установки.")
    if data.get("automatic") and kind != "installer":
        raise ValueError("Автоматический откат недоступен.")
    if "commit" in data or data.get("automatic"):
        commit = root / ("commit-" + data["id"] + ".json")
        if Path(data.get("commit", "")) != commit or commit.is_symlink():
            raise ValueError("Подтверждение установки перенаправлено.")
    target = Path(data.get("target", ""))
    if not target.is_absolute() or target.name.lower() != "blockharbor.exe" or target.is_symlink() or not target.is_file():
        raise ValueError("Не найден установленный BlockHarbor.exe.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(data.get("sha256", ""))) or type(data.get("size")) is not int or not 0 < data["size"] <= 512 * 1024**2:
        raise ValueError("Некорректная контрольная сумма или размер.")
    pids = data.get("waitPids")
    if not isinstance(pids, list) or not 1 <= len(pids) <= 2 or any(type(pid) is not int or not 0 < pid < 2**32 for pid in pids):
        raise ValueError("Некорректный список процессов лаунчера.")
    if "sessionRoot" in data or "launcherSession" in data:
        session_root = Path(data.get("sessionRoot", ""))
        if not session_root.is_absolute() or session_root.resolve() != session_root or not re.fullmatch(r"[a-f0-9]{24}", str(data.get("launcherSession", ""))):
            raise ValueError("Некорректный сеанс установки.")
        shared = session_root / "updates"
        if root != shared and not (root.parent == shared and re.fullmatch(r"window-[1-9][0-9]{0,2}", root.name)):
            raise ValueError("Запрос установки находится вне данных сеанса.")
        directory = session_root / "launcher-sessions"
        if directory.is_symlink() or directory.resolve().parent != session_root:
            raise ValueError("Каталог сеанса установки перенаправлен.")
    return data, source, target, ready


def _hold_session_gate(data):
    if not data.get("sessionRoot"):
        return None
    # The helper runs by filename in embedded Python, outside the package path.
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from launcher.sessions import atomic_json, process_identity, read_record
    path = Path(data["sessionRoot"]) / "launcher-sessions" / "update.json"
    gate = read_record(path)
    if gate.get("session") != data["launcherSession"]:
        raise ValueError("Разрешение на установку уже отозвано.")
    gate.update(helper=process_identity(os.getpid()), targetVersion=data["version"])
    atomic_json(path, gate)
    return path


def _verify(source, data):
    if source.is_symlink() or source.stat().st_size != data["size"]:
        raise ValueError("Размер установщика изменился.")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != data["sha256"]:
        raise ValueError("Контрольная сумма установщика изменилась.")


def _require_commit(data):
    """A closing/crashed window without its final commit cannot trigger setup."""
    if "commit" not in data:
        return  # Compatibility with already issued manual handoffs.
    path = Path(data["commit"])
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Установка отменена при закрытии BlockHarbor.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("id") != data["id"] or value.get("install") is not True:
        raise ValueError("Установка не была подтверждена работающим BlockHarbor.")


def _installer_command(source, target, automatic):
    if not automatic:
        return [str(source)]
    directory = str(target.parent)
    if any(char in directory for char in '\"\r\n\0'):
        raise ValueError("Некорректная папка установленного приложения.")
    # NSIS requires /D to be last and unquoted, including paths with spaces.
    # This is a CreateProcess command line, never a command-shell invocation.
    return subprocess.list2cmdline([str(source), "/S", "/AUTOUPDATE"]) + " /D=" + directory


class ProcessHandles:
    def __init__(self, pids):
        if os.name != "nt":
            raise ValueError("Помощник обновления предназначен для Windows.")
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
        self.kernel.WaitForMultipleObjects.restype = wintypes.DWORD
        self.handles = []
        try:
            for pid in dict.fromkeys(pids):
                handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                if not handle:
                    if ctypes.get_last_error() == 87:  # Already exited, not access denied.
                        continue
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.append(handle)
        except BaseException:
            self.close()
            raise

    def wait(self):
        if not self.handles:
            return
        handles = (self.wintypes.HANDLE * len(self.handles))(*self.handles)
        result = self.kernel.WaitForMultipleObjects(len(self.handles), handles, True, 600000)
        if result == 0x102:
            raise ValueError("BlockHarbor не завершился за 10 минут. Установка отменена.")
        if result == 0xFFFFFFFF:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        if result != 0:
            raise ValueError("Не удалось дождаться завершения лаунчера.")

    def close(self):
        for handle in self.handles:
            self.kernel.CloseHandle(handle)
        self.handles = []


def _restore(source, target, data):
    if target.is_symlink() or not target.is_file():
        raise ValueError("Расположение установленного приложения изменилось.")
    temporary = target.with_name("BlockHarbor.exe.restore-" + secrets.token_hex(8))
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        _verify(temporary, data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        import winreg
        keyname = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BlockHarbor"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, keyname, 0, winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
            if Path(location).resolve() == target.parent.resolve():
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, data["version"])
    except (ImportError, OSError, TypeError):
        pass


def run(path):
    data, source, target, ready = _validate(path)
    result_path = path.parent / ("result-" + data["id"] + ".json")
    handles = None
    gate = None
    restart = False
    try:
        _verify(source, data)
        handles = ProcessHandles(data["waitPids"])
        gate = _hold_session_gate(data)
        _write(ready, {"ready": True, "id": data["id"]})
        handles.wait()
        _require_commit(data)
        _verify(source, data)
        if data["kind"] == "binary":
            _restore(source, target, data)
            restart = True
            result = {"ok": True, "version": data["version"], "kind": "binary", "time": time.time()}
        else:
            # Manual updates retain the normal wizard. Automatic updates keep
            # existing shortcuts and let this helper perform the single restart.
            automatic = data.get("automatic", False)
            process = subprocess.Popen(_installer_command(source, target, automatic), cwd=source.parent, close_fds=True)
            code = process.wait()
            result = {"ok": code == 0, "exitCode": code, "version": data["version"], "kind": "installer", "time": time.time()}
            if code:
                result["error"] = "Установщик BlockHarbor завершился с кодом " + str(code) + ". Повтори обновление вручную в настройках."
            restart = automatic and code == 0
        _write(result_path, result)
    except BaseException as exc:
        _write(result_path, {"ok": False, "error": str(exc)[:700], "time": time.time()})
        raise
    finally:
        if handles:
            handles.close()
        ready.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        if data.get("commit"):
            Path(data["commit"]).unlink(missing_ok=True)
        if gate:
            try:
                stored = json.loads(gate.read_text(encoding="utf-8"))
                if stored.get("session") == data["launcherSession"]:
                    gate.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    if restart:
        command = [str(target)]
        if data.get("sessionRoot"):
            command.extend(["--data", data["sessionRoot"]])
        try:
            # The gate is released before spawning the one replacement window.
            subprocess.Popen(command, cwd=target.parent, close_fds=True)
        except OSError as exc:
            _write(result_path, {"ok": False, "installed": True, "version": data["version"], "error": "Обновление установлено, но BlockHarbor не открылся: " + str(exc)[:500], "time": time.time()})
            raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Ожидается один файл запроса обновления.")
    try:
        run(Path(sys.argv[1]).resolve())
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)

"""Coordinate independent launcher windows without sharing writable game sessions.

The OS releases locks after crashes. Process creation times distinguish a still
running JVM from a recycled PID; a surviving JVM keeps its build protected even
if its launcher was terminated. No account credentials or command lines are kept.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .store import Store


def process_identity(pid):
    if type(pid) is not int or not 0 < pid < 2**32:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return {"pid": pid, "birth": "unknown"} if ctypes.get_last_error() == 5 else None
        try:
            code = wintypes.DWORD()
            if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
                return None
            created, ended, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(system), ctypes.byref(user)):
                return {"pid": pid, "birth": "unknown"}
            birth = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            return {"pid": pid, "birth": birth}
        finally:
            kernel.CloseHandle(handle)
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] in {"Z", "X"}:
            return None
        return {"pid": pid, "birth": fields[19]}
    except FileNotFoundError:
        return None
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return {"pid": pid, "birth": "unknown"}


def process_alive(identity):
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int or not isinstance(identity.get("birth"), str):
        return False
    current = process_identity(identity["pid"])
    return bool(current and (current["birth"] == "unknown" or current == identity))


def atomic_json(path, value):
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise ValueError("Файл состояния окна перенаправлен.")
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_record(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


class FileMutex:
    """Nonblocking advisory byte lock, compatible with the older launcher lock."""

    def __init__(self, path, *, shared=False):
        self.path, self.shared = Path(path), shared
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.resolve().parent != self.path.parent.resolve():
            raise ValueError("Файл блокировки перенаправлен.")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes
                class Overlapped(ctypes.Structure):
                    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
                kernel.LockFileEx.restype = wintypes.BOOL
                kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
                kernel.UnlockFileEx.restype = wintypes.BOOL
                overlapped = Overlapped()
                native = msvcrt.get_osfhandle(fd)
                if not kernel.LockFileEx(native, 1 | (0 if self.shared else 2), 0, 1, 0, ctypes.byref(overlapped)):
                    error = ctypes.get_last_error()
                    if error in {32, 33, 158}:
                        handle.close()
                        return False
                    raise ctypes.WinError(error)
                self._windows = kernel, native, overlapped
            else:
                import fcntl
                fcntl.flock(handle, (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            if os.name == "nt":
                import ctypes
                kernel, native, overlapped = self._windows
                kernel.UnlockFileEx(native, 0, 1, 0, ctypes.byref(overlapped))
            self.handle.close()
            self.handle = None

    def __del__(self):
        try:
            self.close()
        except (OSError, AttributeError, ImportError):
            pass


class SessionBusy(ValueError):
    """A peer owns a file transaction; read-only window startup may continue."""


class WindowStore(Store):
    """Persist defaults while each existing window keeps its selected player/build."""
    LOCAL = {"active", "nickname", "realName", "mode", "clientId", "skinDigest", "model"}

    def __init__(self, root, *, recover=True):
        self.local = {}
        self.local_lock = threading.RLock()
        super().__init__(root, recover=recover)
        values = super().settings()
        self.local = {key: values.get(key) for key in self.LOCAL}

    def remember(self, values):
        with self.local_lock:
            self.local.update({k: str(v) if v is not None else None for k, v in values.items() if k in self.LOCAL})

    def get(self, key, default=""):
        if key in self.LOCAL:
            with self.local_lock:
                value = self.local.get(key)
                return default if value is None else value
        return super().get(key, default)

    def settings(self):
        values = super().settings()
        with self.local_lock:
            for key, value in self.local.items():
                if value is None:
                    values.pop(key, None)
                else:
                    values[key] = value
        return values

    def set_many(self, values):
        super().set_many(values)
        self.remember(values)

    def _create(self, db, *args, **kwargs):
        pid = super()._create(db, *args, **kwargs)
        self.remember({"active": pid})
        return pid

    def move_profile(self, pid, *, restore=False):
        result = super().move_profile(pid, restore=restore)
        if not restore and self.get("active") == pid:
            self.remember({"active": super().get("active")})
        return result


class SessionCoordinator:
    def __init__(self, root, *, startup_timeout=30):
        self.root = Path(root).resolve()
        self.directory = self.root / "launcher-sessions"
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.resolve().parent != self.root:
            raise ValueError("Каталог сеансов перенаправлен.")
        self.session = secrets.token_hex(12)
        self.owner = process_identity(os.getpid())
        self.claims = {}
        self.record_lock = threading.RLock()
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.operation_lock = FileMutex(self.directory / "operations.lock")
        self.legacy = FileMutex(self.root / "activation-v10" / ".session" / "owner.lock", shared=True)
        self.slot_lock = None
        self.slot = 0
        self.path = None
        self.closed = False
        self.recover = None
        # The installer can start the new application from its Finish page just
        # before the helper exits. Wait without holding any lifetime/session lock,
        # otherwise the installer's running-app check would wait on this process.
        deadline = time.monotonic() + startup_timeout
        while True:
            try:
                self._require_no_update()
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        if not self.legacy.acquire():
            raise ValueError("Закрой предыдущую версию BlockHarbor перед запуском нескольких новых окон.")
        try:
            with self.registry():
                self._require_no_update()
                for slot in range(1, 129):
                    candidate = FileMutex(self.directory / (str(slot) + ".lock"))
                    old = read_record(self.directory / (str(slot) + ".json"))
                    previous_profiles = old.get("profiles", {})
                    if isinstance(previous_profiles, dict) and any(process_alive(row.get("process")) for row in previous_profiles.values() if isinstance(row, dict)):
                        continue
                    if candidate.acquire():
                        self.slot, self.slot_lock = slot, candidate
                        break
                if not self.slot:
                    raise ValueError("Открыто слишком много окон BlockHarbor. Закрой неиспользуемые окна.")
                self.path = self.directory / (str(self.slot) + ".json")
                self._save()
        except BaseException:
            self.close()
            raise

    @property
    def namespace(self):
        return "" if self.slot == 1 else "window-" + str(self.slot)

    @contextmanager
    def registry(self):
        mutex = FileMutex(self.directory / "registry.lock")
        deadline = time.monotonic() + 15
        while not mutex.acquire():
            if time.monotonic() >= deadline:
                raise ValueError("Другое окно запускается. Повтори запуск BlockHarbor.")
            time.sleep(0.05)
        try:
            yield
        finally:
            mutex.close()

    def _save(self):
        with self.record_lock:
            if self.path is not None:
                atomic_json(self.path, {"format": 1, "session": self.session, "owner": self.owner, "profiles": dict(self.claims)})

    def snapshot(self):
        launchers, profiles = 0, set()
        for path in list(self.directory.glob("*.json"))[:256]:
            if not path.stem.isdecimal():
                continue
            row = read_record(path)
            live = process_alive(row.get("owner"))
            if live:
                launchers += 1
            records = row.get("profiles", {})
            if not isinstance(records, dict):
                continue
            for pid, info in records.items():
                if isinstance(pid, str) and re.fullmatch(r"[a-f0-9]{32}", pid) and isinstance(info, dict) and (live or process_alive(info.get("process"))):
                    profiles.add(pid)
        return {"runningProfiles": sorted(profiles), "launcherCount": launchers}

    def _require_no_update(self):
        gate = read_record(self.directory / "update.json")
        if gate.get("session") != self.session and (process_alive(gate.get("owner")) or process_alive(gate.get("helper"))):
            raise ValueError("Устанавливается обновление BlockHarbor. Дождись завершения установщика и открой лаунчер снова.")

    @contextmanager
    def operation(self, *, wait=False, timeout=300, status=None):
        if not self.thread_lock.acquire(blocking=wait):
            raise SessionBusy("В этом окне изменяются файлы. Дождись завершения операции.")
        acquired = False
        try:
            if not self.depth:
                deadline = time.monotonic() + timeout
                while not self.operation_lock.acquire():
                    if not wait or time.monotonic() >= deadline:
                        raise SessionBusy("В другом окне BlockHarbor изменяются файлы. Дождись завершения операции и повтори.")
                    if status:
                        status("Ожидаем завершения подготовки в другом окне BlockHarbor…")
                        status = None
                    time.sleep(0.1)
                acquired = True
            self.depth += 1
            try:
                self._require_no_update()
                if acquired and self.recover is not None:
                    self.recover()
                yield
            finally:
                self.depth -= 1
        finally:
            if acquired:
                self.operation_lock.close()
            self.thread_lock.release()

    def claim(self, pid):
        # Caller holds operation() until startup has published the child identity.
        if pid in self.snapshot()["runningProfiles"]:
            raise ValueError("Эта сборка уже запущена в Minecraft или готовится в другом окне. Для ещё одного запуска сначала сделай отдельную копию сборки.")
        with self.record_lock:
            self.claims[pid] = {"stage": "preparing", "process": None}
            self._save()

    def attach(self, pid, process, *, stage="game"):
        with self.record_lock:
            self.claims[pid] = {"stage": stage, "process": process_identity(getattr(process, "pid", None))}
            self._save()

    def release(self, pid):
        with self.record_lock:
            if self.claims.pop(pid, None) is not None:
                self._save()

    @contextmanager
    def update_guard(self):
        with self.operation(wait=True, timeout=15):
            with self.registry():
                state = self.snapshot()
                if state["launcherCount"] > 1 or state["runningProfiles"]:
                    raise ValueError("Закрой все Minecraft и остальные окна BlockHarbor перед установкой обновления.")
                path = self.directory / "update.json"
                atomic_json(path, {"format": 1, "session": self.session, "owner": self.owner})
            try:
                yield
            finally:
                gate = read_record(path)
                if gate.get("session") == self.session and not process_alive(gate.get("helper")):
                    path.unlink(missing_ok=True)

    def handoff(self):
        return {"sessionRoot": str(self.root), "launcherSession": self.session}

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.path:
                with self.record_lock:
                    # Retain a surviving JVM's identity if the owner closes abnormally.
                    self.claims = {key: row for key, row in self.claims.items() if process_alive(row.get("process"))}
                    if self.claims:
                        atomic_json(self.path, {"format": 1, "session": self.session, "owner": None, "profiles": self.claims})
                    else:
                        self.path.unlink(missing_ok=True)
        except OSError:
            pass  # A temporary/test data directory may already have been removed.
        finally:
            if self.slot_lock:
                self.slot_lock.close()
            self.legacy.close()

    def __del__(self):
        try:
            self.close()
        except (OSError, AttributeError, ImportError):
            pass


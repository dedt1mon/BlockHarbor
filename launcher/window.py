"""Launch the desktop UI and bound only the Windows app window registered by this session."""

from __future__ import annotations

import ctypes
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

from . import lifecycle

WIDTH, HEIGHT = 1360, 840
WINDOW_TITLE = "BlockHarbor"
SW_RESTORE = 9
# Browser windows retain their HICONs. Reuse the two handles for this process
# lifetime; Windows releases them when the launcher exits.
_WINDOW_ICONS = {}


class MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def window_bounds(rect, work, dpi, *, opening=False, maximized=False):
    """Return a work-area constrained *outer* rectangle in physical pixels."""
    left, top, right, bottom = work
    if dpi <= 0 or right <= left or bottom <= top:
        return None
    width = min(right - left, round(WIDTH * dpi / 96))
    height = min(bottom - top, round(HEIGHT * dpi / 96))
    x, y, r, b = rect
    if opening or maximized:
        return (
            left + (right - left - width) // 2,
            top + (bottom - top - height) // 2,
            width,
            height,
        )
    if r - x <= width and b - y <= height:
        return None
    width, height = min(r - x, width), min(b - y, height)
    return (
        max(left, min(x, right - width)),
        max(top, min(y, bottom - height)),
        width,
        height,
    )


def browser_executable():
    directories = [
        os.getenv(key)
        for key in ("ProgramFiles(x86)", "ProgramW6432", "ProgramFiles", "LOCALAPPDATA")
    ]
    for name in (
        "Microsoft/Edge/Application/msedge.exe",
        "Google/Chrome/Application/chrome.exe",
        "BraveSoftware/Brave-Browser/Application/brave.exe",
        "Mozilla Firefox/firefox.exe",
    ):
        for folder in directories:
            if folder:
                exe = Path(folder) / name
                if exe.is_file():
                    return exe
    return None


def _private_profile(path):
    if path.is_symlink() or path.resolve().parent != path.parent:
        raise OSError("Browser profile is redirected")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise OSError("Browser profile is redirected")
    return path


class NativeFrame:
    """Win32 calls with pointer-sized handles. Created only for the Windows app host."""

    def __init__(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = wintypes.HWND
        self.enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, hwnd, wintypes.LPARAM)
        signatures = {
            "EnumWindows": (wintypes.BOOL, [self.enum_callback, wintypes.LPARAM]),
            "GetWindowTextW": (ctypes.c_int, [hwnd, wintypes.LPWSTR, ctypes.c_int]),
            "GetClassNameW": (ctypes.c_int, [hwnd, wintypes.LPWSTR, ctypes.c_int]),
            "GetWindowThreadProcessId": (
                wintypes.DWORD,
                [hwnd, ctypes.POINTER(wintypes.DWORD)],
            ),
            "GetWindowRect": (wintypes.BOOL, [hwnd, ctypes.POINTER(wintypes.RECT)]),
            "MonitorFromWindow": (wintypes.HANDLE, [hwnd, wintypes.DWORD]),
            "GetMonitorInfoW": (
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)],
            ),
            "GetDpiForWindow": (wintypes.UINT, [hwnd]),
            "SetThreadDpiAwarenessContext": (ctypes.c_void_p, [ctypes.c_void_p]),
            "ShowWindow": (wintypes.BOOL, [hwnd, ctypes.c_int]),
            "SetWindowPos": (
                wintypes.BOOL,
                [
                    hwnd,
                    hwnd,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ],
            ),
            "SetPropW": (wintypes.BOOL, [hwnd, wintypes.LPCWSTR, wintypes.HANDLE]),
            "GetPropW": (wintypes.HANDLE, [hwnd, wintypes.LPCWSTR]),
            "IsIconic": (wintypes.BOOL, [hwnd]),
            "IsZoomed": (wintypes.BOOL, [hwnd]),
            "SetForegroundWindow": (wintypes.BOOL, [hwnd]),
            "LoadImageW": (
                wintypes.HANDLE,
                [
                    wintypes.HANDLE,
                    wintypes.LPCWSTR,
                    wintypes.UINT,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ],
            ),
            "SendMessageTimeoutW": (
                ctypes.c_ssize_t,
                [
                    hwnd,
                    wintypes.UINT,
                    ctypes.c_size_t,
                    ctypes.c_ssize_t,
                    wintypes.UINT,
                    wintypes.UINT,
                    ctypes.POINTER(ctypes.c_size_t),
                ],
            ),
        }
        for name, (result, arguments) in signatures.items():
            proc = getattr(user, name)
            proc.restype, proc.argtypes = result, arguments
        self.user = user
        self.hwnd = None
        self.pid = 0
        self.frame_property = ""
        self.registration_marker = ""
        self.owned_host = False

    def _identity(self, handle):
        kind = ctypes.create_unicode_buffer(128)
        title = ctypes.create_unicode_buffer(512)
        pid = wintypes.DWORD()
        self.user.GetClassNameW(handle, kind, len(kind))
        if kind.value not in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
            return kind.value, "", 0
        self.user.GetWindowTextW(handle, title, len(title))
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return kind.value, title.value, pid.value

    @staticmethod
    def _caption(title, caption):
        if title == caption:
            return True
        # Edge can append a localized profile and a zero-width space in its brand
        # (e.g. " — Личный: Microsoft​ Edge"). A tab name must start with our caption.
        if not title.startswith(caption):
            return False
        suffix = title[len(caption) :].replace("\u200b", "").replace("\xa0", " ")
        return suffix.startswith((" - ", " — ", " – ")) and suffix.rstrip().endswith(
            ("Google Chrome", "Microsoft Edge", "Chromium", "Brave", "Mozilla Firefox")
        )

    def register(self, marker):
        caption = f"{WINDOW_TITLE} [{marker}]"
        prop = f"BlockHarbor.frame.{marker}"

        @self.enum_callback
        def match(handle, _):
            _, title, pid = self._identity(handle)
            if (
                pid
                and self._caption(title, caption)
                and self.user.SetPropW(handle, prop, 1)
            ):
                self.hwnd, self.pid = handle, pid
                self.frame_property, self.registration_marker = prop, marker
                self.owned_host = True
                return False
            return True

        self.user.EnumWindows(match, 0)
        if self.hwnd:
            self.set_icons()
            self.set_identity()
        return bool(self.hwnd)

    def register_host(self, pid, marker):
        """Find only our still-running Popen host before HTML can register itself."""
        if type(pid) is not int or pid <= 0:
            return False
        prop = f"BlockHarbor.frame.{marker}"

        @self.enum_callback
        def match(handle, _):
            kind, _, owner = self._identity(handle)
            if (owner == pid and kind in ("Chrome_WidgetWin_1", "MozillaWindowClass")
                    and self.user.SetPropW(handle, prop, 1)):
                self.hwnd, self.pid = handle, owner
                self.frame_property, self.registration_marker = prop, marker
                self.owned_host = True
                return False
            return True

        self.user.EnumWindows(match, 0)
        return bool(self.hwnd)

    def set_identity(self):
        if not self.current():
            return False
        from .windows_identity import mark_default_shortcuts, set_window_identity

        target = os.environ.get("BLOCKHARBOR_LAUNCHER_EXE", "")
        if not target:
            return False
        result = set_window_identity(self.hwnd, target)
        if result:
            threading.Thread(target=mark_default_shortcuts, args=(target,), daemon=True).start()
        return result

    def clear_identity(self):
        if not self.current():
            return False
        from .windows_identity import set_window_identity

        return set_window_identity(self.hwnd, clear=True)

    def close(self):
        if not self.alive():
            return False
        self.clear_identity()
        result = ctypes.c_size_t()
        # WM_CLOSE only to the registered launcher window, with a bounded wait.
        return bool(self.user.SendMessageTimeoutW(self.hwnd, 0x10, 0, 0, 3, 1500, ctypes.byref(result)))

    def set_icons(self):
        if not self.current():
            return False
        icon_file = str(Path(__file__).resolve().parents[1] / "ui" / "icon.ico")
        sent = 0
        for kind, size in ((1, 256), (0, 32)):
            handle = _WINDOW_ICONS.get(size)
            if not handle:
                handle = self.user.LoadImageW(None, icon_file, 1, size, size, 0x10)
                if not handle:
                    continue
                _WINDOW_ICONS[size] = handle
            previous = ctypes.c_size_t()
            # WM_SETICON, ICON_BIG/ICON_SMALL. A hung browser must not block the UI.
            sent += bool(
                self.user.SendMessageTimeoutW(
                    self.hwnd, 0x80, kind, handle, 3, 1000, ctypes.byref(previous)
                )
            )
        return sent == 2

    def alive(self):
        if not self.hwnd:
            return False
        kind, _, pid = self._identity(self.hwnd)
        return (
            kind in ("Chrome_WidgetWin_1", "MozillaWindowClass")
            and pid == self.pid
            and self.frame_property
            and self.user.GetPropW(self.hwnd, self.frame_property) == 1
        )

    def current(self):
        if not self.alive():
            return False
        _, title, _ = self._identity(self.hwnd)
        return bool(getattr(self, "owned_host", False) or (
                self._caption(title, WINDOW_TITLE)
                or self._caption(title, f"{WINDOW_TITLE} [{self.registration_marker}]")
            ))

    def hide(self):
        if not self.current():
            return False
        self.user.ShowWindow(
            self.hwnd, 0
        )  # SW_HIDE removes this window from the taskbar.
        return True

    def focus(self):
        if not self.current():
            return False
        if self.user.IsIconic(self.hwnd):
            self.user.ShowWindow(self.hwnd, SW_RESTORE)
        else:
            self.user.ShowWindow(self.hwnd, 5)  # SW_SHOW also restores a hidden frame.
        self.user.SetForegroundWindow(self.hwnd)
        return True

    def constrain(self, *, opening=False):
        if not self.current() or self.user.IsIconic(self.hwnd):
            return
        # Get physical rectangles regardless of the embedded Python executable's manifest.
        previous = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            work = MonitorInfo()
            work.cbSize = ctypes.sizeof(work)
            monitor = self.user.MonitorFromWindow(self.hwnd, 2)
            rect = wintypes.RECT()
            if (
                not monitor
                or not self.user.GetMonitorInfoW(monitor, ctypes.byref(work))
                or not self.user.GetWindowRect(self.hwnd, ctypes.byref(rect))
            ):
                return

            def coords(r):
                return r.left, r.top, r.right, r.bottom

            zoomed = bool(self.user.IsZoomed(self.hwnd))
            bounds = window_bounds(
                coords(rect),
                coords(work.rcWork),
                self.user.GetDpiForWindow(self.hwnd),
                opening=opening,
                maximized=zoomed,
            )
            if bounds:
                if zoomed:
                    self.user.ShowWindow(self.hwnd, SW_RESTORE)
                self.user.SetWindowPos(
                    self.hwnd, None, *bounds, 0x0014
                )  # NOZORDER | NOACTIVATE
        finally:
            if previous:
                self.user.SetThreadDpiAwarenessContext(previous)


class DesktopWindow:
    def __init__(self, root, *, namespace=""):
        self.profile = Path(root).resolve() / ("window-profile-" + namespace if namespace else "window-profile")
        self.lock = threading.RLock()
        self.url = ""
        self.parked = False
        self.restore_after_game = False
        self.awaiting_registration = False
        self.native = None
        self.marker = ""
        self.host_process = None
        self.release_host = None
        self.opened_at = 0.0
        self.stopped = False

    def _discover(self):
        if not self.native or self.native.hwnd or not self.marker:
            return
        process = self.host_process
        # No title, PID enumeration or OS handle from the default browser is
        # ever adopted here. The Popen is a dedicated app profile we created.
        if process is not None and process.poll() is None:
            self.native.register_host(process.pid, self.marker)
        if self.native.hwnd and self.parked:
            self.native.hide()

    def game_started(self):
        with self.lock:
            if self.stopped:
                return
            if self.native and self.url:
                self.restore_after_game = True
                self.parked = True
                self.native.hide()

    def close(self, timeout=1):
        with self.lock:
            if self.stopped:
                return False
            self.stopped = True
            self.restore_after_game = self.parked = False
            self._discover()
            closed = bool(self.native and self.native.close())
            process, release = self.host_process, self.release_host
            self.host_process = self.release_host = None
        if process is not None:
            try:
                process.wait(timeout=max(0, timeout))
            except subprocess.TimeoutExpired:
                # Popen belongs to the dedicated app profile, not a user's
                # default/shared browser. Its JobObject owns its renderers too.
                try:
                    process.kill()
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
            finally:
                if release:
                    release()
        return closed

    def closed(self):
        """A hidden registered window is alive; an explicitly closed one is not."""
        with self.lock:
            if self.stopped:
                return True
            self._discover()
            if self.native and self.native.hwnd:
                return not self.native.alive()
            if self.host_process is not None:
                code = self.host_process.poll()
                # Chromium can briefly hand off to its existing dedicated host.
                # Allow the marked page to attach; a failed/closed new host must
                # never keep its server alive for the former 180 second timeout.
                return code is not None and (code != 0 or time.monotonic() - self.opened_at > 2)
            return False

    def page_closed(self):
        """A fallback page owns no HWND. A native reload keeps its own frame."""
        with self.lock:
            self._discover()
            return not self.native or not self.native.hwnd or not self.native.alive()

    def game_ended(self):
        with self.lock:
            if self.stopped or not self.restore_after_game:
                return
            self.restore_after_game = False
            if self.closed():
                return  # Manual X never becomes a fresh window after game exit.
            # An unregistered frame could not have been hidden. Its first redirect
            # or title handshake may still be in progress; reuse that same window.
            if self.parked and self.awaiting_registration:
                self.parked = False
                return
            self.show(self.url)

    def show(self, url):
        with self.lock:
            if self.stopped:
                return
            self.url = url
            self.parked = False
            if self.native and self.native.focus():
                return
            if self.native and self.native.hwnd and self.native.alive():
                return  # A title/navigation transition is not a second app host.
            exe = browser_executable() if sys.platform == "win32" else None
            if exe:
                try:
                    native = NativeFrame()
                    marker = secrets.token_hex(12)
                    base, fragment = url.split("#", 1)
                    address = base + "?window=" + marker + "#" + fragment
                    if exe.name.lower() == "firefox.exe":
                        profile = _private_profile(self.profile.with_name("firefox-" + self.profile.name))
                        command = [
                            str(exe),
                            "-no-remote",
                            "-profile",
                            str(profile),
                            "-new-window",
                            address,
                        ]
                    else:
                        _private_profile(self.profile)
                        command = [
                            str(exe),
                            "--user-data-dir=" + str(self.profile),
                            "--app=" + address,
                            f"--window-size={WIDTH},{HEIGHT}",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-background-mode",
                        ]
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except (OSError, AttributeError):
                    # An absent Win32 entry point or unavailable executable must not
                    # give the fallback browser a native-window registration marker.
                    self.awaiting_registration = False
                    self.native, self.marker = None, ""
                else:
                    self.host_process = process
                    self.release_host = lifecycle.own_process(process)
                    self.opened_at = time.monotonic()
                    self.awaiting_registration = True
                    self.native, self.marker = native, marker
                    return
        if not webbrowser.open(url):
            raise RuntimeError(
                "Не удалось открыть окно. Установи Microsoft Edge или выбери браузер по умолчанию."
            )

    def adjust(self, marker):
        # Fallback browsers do not receive a window marker. An unrelated browser's HWND
        # cannot be supplied to this endpoint; register() resolves the one-use caption.
        with self.lock:
            if self.stopped or not self.native or not self.marker or marker != self.marker:
                return False
            first = self.native.hwnd is None or self.awaiting_registration
            if self.native.hwnd is None and not self.native.register(marker):
                return False
            if not self.native.alive():
                return False
            self.awaiting_registration = False
            if first:
                self.native.set_icons()
                self.native.set_identity()
            if self.parked:
                self.native.hide()
            else:
                self.native.constrain(opening=first)
            return True

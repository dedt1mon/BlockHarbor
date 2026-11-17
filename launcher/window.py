"""Launch the desktop UI and bound only the Windows app window registered by this session."""

from __future__ import annotations

import ctypes
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from ctypes import wintypes
from pathlib import Path

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
                return False
            return True

        self.user.EnumWindows(match, 0)
        if self.hwnd:
            self.set_icons()
        return bool(self.hwnd)

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

    def current(self):
        if not self.hwnd:
            return False
        kind, title, pid = self._identity(self.hwnd)
        return (
            kind in ("Chrome_WidgetWin_1", "MozillaWindowClass")
            and pid == self.pid
            and self.frame_property
            and self.user.GetPropW(self.hwnd, self.frame_property) == 1
            and (
                self._caption(title, WINDOW_TITLE)
                or self._caption(title, f"{WINDOW_TITLE} [{self.registration_marker}]")
            )
        )

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
    def __init__(self, root):
        self.profile = Path(root).resolve() / "window-profile"
        self.lock = threading.RLock()
        self.url = ""
        self.parked = False
        self.restore_after_game = False
        self.awaiting_registration = False
        self.native = None
        self.marker = ""

    def game_started(self):
        with self.lock:
            if self.native and self.url:
                self.restore_after_game = True
                self.parked = True
                self.native.hide()

    def game_ended(self):
        with self.lock:
            if not self.restore_after_game:
                return
            self.restore_after_game = False
            # An unregistered frame could not have been hidden. Its first redirect
            # or title handshake may still be in progress; reuse that same window.
            if self.parked and self.awaiting_registration:
                self.parked = False
                return
            self.show(self.url)

    def show(self, url):
        with self.lock:
            self.url = url
            self.parked = False
            if self.native and self.native.focus():
                return
            exe = browser_executable() if sys.platform == "win32" else None
            if exe:
                try:
                    native = NativeFrame()
                    marker = secrets.token_hex(12)
                    base, fragment = url.split("#", 1)
                    address = base + "?window=" + marker + "#" + fragment
                    if exe.name.lower() == "firefox.exe":
                        profile = self.profile.with_name("firefox-window-profile")
                        profile.mkdir(parents=True, exist_ok=True)
                        command = [
                            str(exe),
                            "-no-remote",
                            "-profile",
                            str(profile),
                            "-new-window",
                            address,
                        ]
                    else:
                        command = [
                            str(exe),
                            "--user-data-dir=" + str(self.profile),
                            "--app=" + address,
                            f"--window-size={WIDTH},{HEIGHT}",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-background-mode",
                        ]
                    subprocess.Popen(
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
            if not self.native or not self.marker or marker != self.marker:
                return False
            first = self.native.hwnd is None
            if first and not self.native.register(marker):
                return False
            self.awaiting_registration = False
            if self.parked:
                self.native.hide()
            else:
                self.native.constrain(opening=first)
            return True

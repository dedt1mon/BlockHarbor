"""Open Steam friends, falling back to the installed client or Valve's installer page."""

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

STEAM_FRIENDS = "steam://open/friends"
STEAM_SETUP = "https://store.steampowered.com/about/"


def _windows_steam():
    try:
        import winreg
    except ImportError:
        return True, None
    exe = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            path = Path(winreg.QueryValueEx(key, "SteamExe")[0])
            if (
                path.is_absolute()
                and path.name.lower() == "steam.exe"
                and path.is_file()
            ):
                exe = path
    except (OSError, TypeError, ValueError):
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"steam\shell\open\command"):
            return True, exe
    except OSError:
        return False, exe


def open_steam() -> str:
    if sys.platform == "win32":
        registered, exe = _windows_steam()
        if registered:
            try:
                os.startfile(STEAM_FRIENDS)
                return "friends"
            except OSError:
                pass
        if exe:
            try:
                subprocess.Popen([str(exe), STEAM_FRIENDS], cwd=exe.parent)
                return "friends"
            except OSError:
                pass
        try:
            os.startfile(STEAM_SETUP)
            return "install"
        except OSError:
            pass
    else:
        try:
            if webbrowser.open(STEAM_FRIENDS):
                return "friends"
            if webbrowser.open(STEAM_SETUP):
                return "install"
        except OSError:
            pass
    raise RuntimeError(
        "Не удалось открыть Steam. Используй ссылку «Скачать Steam» или запусти клиент из меню приложений."
    )

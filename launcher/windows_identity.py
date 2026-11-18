"""Give the verified launcher window and its own shortcuts a stable Windows ID.

Only documented Shell property APIs are used. Pinning remains the user's action.
The caller must verify HWND ownership before calling ``set_window_identity``.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import uuid
from pathlib import Path

APP_ID = "BlockHarbor.Launcher"
HRESULT = ctypes.c_int32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]


class _Value(ctypes.Union):
    _fields_ = [("pointer", ctypes.c_void_p), ("space", ctypes.c_uint64 * 2)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("reserved1", ctypes.c_uint16), ("reserved2", ctypes.c_uint16), ("reserved3", ctypes.c_uint16), ("value", _Value)]


IDENTITY_GUID = "9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"
STORE_IID = "886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"
TARGET_KEY = PROPERTYKEY(GUID.parse("b9b4b3fc-2b51-4a42-b5d8-324146afcf25"), 2)


def property_values(executable):
    """Set relaunch metadata before AppUserModel.ID, as required by Shell."""
    path = Path(executable)
    if not path.is_absolute() or path.name.lower() != "blockharbor.exe" or any(c in str(path) for c in '\r\n\x00"'):
        raise ValueError("Неверный путь BlockHarbor.exe.")
    return [
        (2, subprocess.list2cmdline([str(path)])),
        (4, "BlockHarbor"),
        (3, str(path) + ",0"),
        (5, APP_ID),
    ]


class _Shell:
    def __init__(self):
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole.CoInitializeEx.restype = HRESULT
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
        self.ole.PropVariantClear.restype = HRESULT
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        self.shell.SHGetPropertyStoreForWindow.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self.shell.SHGetPropertyStoreForWindow.restype = HRESULT
        self.shell.SHGetPropertyStoreFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self.shell.SHGetPropertyStoreFromParsingName.restype = HRESULT
        self.shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.shell.SHGetKnownFolderPath.restype = HRESULT
        initialized = self.ole.CoInitializeEx(None, 2)
        self.must_uninit = initialized in (0, 1)
        if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE: already initialized.
            raise OSError("COM initialization failed")

    @staticmethod
    def method(store, index, result, *arguments):
        table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[index])

    def close_store(self, store):
        if store.value:
            self.method(store, 2, ctypes.c_uint32)(store)

    def set_values(self, store, values):
        setter = self.method(store, 6, HRESULT, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
        for number, text in values:
            key = PROPERTYKEY(GUID.parse(IDENTITY_GUID), number)
            variant = PROPVARIANT()
            # The Shell copies this string during SetValue. The buffer remains
            # Python-owned and must not be passed to PropVariantClear.
            buffer = ctypes.create_unicode_buffer(text) if text is not None else None
            if buffer is not None:
                variant.vt = 31  # VT_LPWSTR
                variant.value.pointer = ctypes.cast(buffer, ctypes.c_void_p)
            if setter(store, ctypes.byref(key), ctypes.byref(variant)) < 0:
                return False
        return True

    def known_folder(self, value):
        pointer = ctypes.c_void_p()
        key = GUID.parse(value)
        if self.shell.SHGetKnownFolderPath(ctypes.byref(key), 0, None, ctypes.byref(pointer)) < 0:
            return None
        try:
            return Path(ctypes.wstring_at(pointer)) if pointer.value else None
        finally:
            self.ole.CoTaskMemFree(pointer)

    def close(self):
        if self.must_uninit:
            self.ole.CoUninitialize()


def set_window_identity(hwnd, executable=None, *, clear=False):
    if os.name != "nt" or not hwnd:
        return False
    api, store = None, ctypes.c_void_p()
    try:
        values = [(key, None) for key in (5, 2, 4, 3)] if clear else property_values(executable)
        api = _Shell()
        iid = GUID.parse(STORE_IID)
        if api.shell.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(store)) < 0:
            return False
        return api.set_values(store, values)
    except (OSError, AttributeError, TypeError, ValueError):
        return False
    finally:
        if api:
            api.close_store(store)
            api.close()


def mark_shortcut(shortcut, executable):
    """Label an existing .lnk only after its real target matches our launcher."""
    if os.name != "nt":
        return False
    api, store = None, ctypes.c_void_p()
    try:
        shortcut, executable = Path(shortcut), Path(executable)
        if shortcut.suffix.lower() != ".lnk" or not shortcut.is_file() or shortcut.is_symlink():
            return False
        values = property_values(executable)
        api = _Shell()
        iid = GUID.parse(STORE_IID)
        if api.shell.SHGetPropertyStoreFromParsingName(str(shortcut), None, 2, ctypes.byref(iid), ctypes.byref(store)) < 0:
            return False
        value = PROPVARIANT()
        getter = api.method(store, 5, HRESULT, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
        try:
            if getter(store, ctypes.byref(TARGET_KEY), ctypes.byref(value)) < 0 or value.vt != 31 or not value.value.pointer:
                return False
            actual = Path(ctypes.wstring_at(value.value.pointer))
            if os.path.normcase(str(actual.resolve())) != os.path.normcase(str(executable.resolve())):
                return False
        finally:
            api.ole.PropVariantClear(ctypes.byref(value))
        # Shortcut arguments are intentionally preserved (per-build shortcuts).
        # RelaunchCommand on the HWND is for the generic launcher, whereas a
        # shortcut only needs its identity to match that HWND.
        if not api.set_values(store, [(5, APP_ID)]):
            return False
        return api.method(store, 7, HRESULT)(store) >= 0
    except (OSError, AttributeError, TypeError, ValueError):
        return False
    finally:
        if api:
            api.close_store(store)
            api.close()


def mark_default_shortcuts(executable):
    if os.name != "nt":
        return
    api = None
    try:
        api = _Shell()
        desktop = api.known_folder("b4bfcc3a-db2c-424c-b029-7fe99a87c641")
        programs = api.known_folder("a77f5d77-2e2b-44c3-a6a2-aba601054a51")
        for path in [desktop / "BlockHarbor.lnk" if desktop else None, programs / "BlockHarbor" / "BlockHarbor.lnk" if programs else None]:
            if path:
                mark_shortcut(path, executable)
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    finally:
        if api:
            api.close()


def mark_desktop_shortcut(filename, executable):
    if os.name != "nt" or not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith(".lnk"):
        return False
    api = None
    try:
        api = _Shell()
        desktop = api.known_folder("b4bfcc3a-db2c-424c-b029-7fe99a87c641")
        return bool(desktop and mark_shortcut(desktop / filename, executable))
    except (OSError, AttributeError, TypeError, ValueError):
        return False
    finally:
        if api:
            api.close()

"""A small, cancellable native notice while an older launcher is finishing.

The modeless Win32 dialog needs neither Tk nor third-party GUI packages. Its
messages are pumped by the short lock retry loop on the same thread that owns
the dialog. It is created only when waiting is actually necessary.
"""

from __future__ import annotations

import os
import struct


class StartupCancelled(Exception):
    """The user cancelled opening this new launcher; exit without an error box."""


def _dialog_template(message):
    def string(value):
        return value.encode("utf-16-le") + b"\0\0"

    # Standard DLGTEMPLATE: popup, caption, close button, system font, centred.
    style = 0x80000000 | 0x10000000 | 0x00C00000 | 0x00080000 | 0x80 | 0x40 | 0x800
    data = bytearray(struct.pack("<LLHhhhh", style, 0x00040000, 2, 0, 0, 340, 122))
    data.extend(b"\0\0\0\0")  # no menu, default dialog class
    data.extend(string("BlockHarbor — запуск"))
    data.extend(struct.pack("<H", 9) + string("Segoe UI"))

    def control(class_id, control_id, text, x, y, width, height, extra_style=0):
        data.extend(b"\0" * (-len(data) % 4))
        data.extend(struct.pack("<LLhhhhH", 0x50000000 | extra_style, 0, x, y, width, height, control_id))
        data.extend(struct.pack("<HH", 0xFFFF, class_id))
        data.extend(string(text) + b"\0\0")

    control(0x82, 100, message, 12, 12, 316, 76)  # STATIC
    control(0x80, 2, "Отмена", 258, 96, 70, 18, 0x00010001)  # BUTTON, tab stop/default
    return bytes(data)


class StartupWait:
    def __init__(self):
        self.hwnd = None
        self.cancelled = False
        self.message = None
        self.user = None

    def _open(self, message):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        class Message(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                        ("time", wintypes.DWORD), ("pt", wintypes.POINT), ("lPrivate", wintypes.DWORD)]
        self.Message = Message
        user = self.user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        dialog_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        user.CreateDialogIndirectParamW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p, wintypes.HWND, dialog_proc, ctypes.c_ssize_t]
        user.CreateDialogIndirectParamW.restype = wintypes.HWND
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.DestroyWindow.restype = wintypes.BOOL
        user.PeekMessageW.argtypes = [ctypes.POINTER(Message), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user.PeekMessageW.restype = wintypes.BOOL
        user.IsDialogMessageW.argtypes = [wintypes.HWND, ctypes.POINTER(Message)]
        user.IsDialogMessageW.restype = wintypes.BOOL
        user.TranslateMessage.argtypes = [ctypes.POINTER(Message)]
        user.TranslateMessage.restype = wintypes.BOOL
        user.DispatchMessageW.argtypes = [ctypes.POINTER(Message)]
        user.DispatchMessageW.restype = ctypes.c_ssize_t
        user.SetDlgItemTextW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LPCWSTR]
        user.SetDlgItemTextW.restype = wintypes.BOOL
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE

        def procedure(hwnd, message_id, wparam, lparam):
            if message_id == 0x0110:  # WM_INITDIALOG: let Windows focus Cancel.
                return 1
            if message_id == 0x0010 or (message_id == 0x0111 and wparam & 0xFFFF == 2):
                self.cancelled = True  # WM_CLOSE or IDCANCEL; owner loop closes it.
                return 1
            return 0

        self.procedure = dialog_proc(procedure)  # Retain callback for the dialog lifetime.
        self.template = ctypes.create_string_buffer(_dialog_template(message))
        instance = kernel.GetModuleHandleW(None)
        if not instance:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = user.CreateDialogIndirectParamW(instance, self.template, None, self.procedure, 0)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.message = message

    def update(self, message):
        if self.cancelled:
            return False
        if os.name != "nt":
            return True
        if self.hwnd is None:
            self._open(message)
        elif message != self.message:
            self.user.SetDlgItemTextW(self.hwnd, 100, message)
            self.message = message
        item = self.Message()
        # Keep acquisition responsive even if the window receives many messages.
        for _ in range(100):
            if not self.user.PeekMessageW(self.ctypes.byref(item), self.hwnd, 0, 0, 1):
                break
            if item.message == 0x0012:  # WM_QUIT
                self.cancelled = True
                break
            if not self.user.IsDialogMessageW(self.hwnd, self.ctypes.byref(item)):
                self.user.TranslateMessage(self.ctypes.byref(item))
                self.user.DispatchMessageW(self.ctypes.byref(item))
        return not self.cancelled

    def close(self):
        if self.hwnd is not None:
            hwnd, self.hwnd = self.hwnd, None
            self.user.DestroyWindow(hwnd)


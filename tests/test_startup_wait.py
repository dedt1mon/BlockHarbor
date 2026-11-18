import ctypes
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from launcher.startup_wait import StartupWait, _dialog_template


class NativeStartupWaitTests(unittest.TestCase):
    def setUp(self):
        self.hwnd = 0x123456789
        self.user = SimpleNamespace(**{name: Mock(return_value=1) for name in (
            "CreateDialogIndirectParamW", "DestroyWindow", "PeekMessageW", "IsDialogMessageW",
            "TranslateMessage", "DispatchMessageW", "SetDlgItemTextW",
        )})
        self.user.CreateDialogIndirectParamW.return_value = self.hwnd
        self.user.PeekMessageW.return_value = 0
        self.kernel = SimpleNamespace(GetModuleHandleW=Mock(return_value=0x987654321))
        self.enterContext(patch("ctypes.WinDLL", side_effect=lambda name, **_: self.user if name == "user32" else self.kernel, create=True))
        self.enterContext(patch("ctypes.WINFUNCTYPE", getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE), create=True))
        self.enterContext(patch("launcher.startup_wait.os", SimpleNamespace(name="nt")))

    def test_own_notice_closes_on_cancel_with_pointer_sized_handles(self):
        from ctypes import wintypes
        notice = StartupWait()
        self.assertTrue(notice.update("Ожидаем завершения…"))
        self.assertEqual(notice.hwnd, self.hwnd)
        call = self.user.CreateDialogIndirectParamW.call_args.args
        self.assertEqual(call[0], 0x987654321)
        self.assertIs(self.user.CreateDialogIndirectParamW.restype, wintypes.HWND)
        self.assertIs(self.user.CreateDialogIndirectParamW.argtypes[0], wintypes.HINSTANCE)
        self.assertEqual(call[3](self.hwnd, 0x0110, 0, 0), 1)
        self.assertEqual(call[3](self.hwnd, 0x0111, 2, 0), 1)  # IDCANCEL
        self.assertFalse(notice.update("Ожидаем завершения…"))
        notice.close()
        notice.close()
        self.user.DestroyWindow.assert_called_once_with(self.hwnd)

    def test_window_close_button_and_keyboard_messages_remain_cancellable(self):
        notice = StartupWait()
        self.assertTrue(notice.update("Сеанс завершается"))
        def peek(pointer, hwnd, *_):
            self.assertEqual(hwnd, self.hwnd)
            pointer._obj.message = 0x0010  # WM_CLOSE
            self.user.PeekMessageW.side_effect = None
            self.user.PeekMessageW.return_value = 0
            return 1
        self.user.PeekMessageW.side_effect = peek
        self.user.IsDialogMessageW.return_value = 0
        self.user.DispatchMessageW.side_effect = lambda item: notice.procedure(self.hwnd, item._obj.message, 0, 0)
        self.assertFalse(notice.update("Сеанс завершается"))
        self.user.IsDialogMessageW.assert_called_once()
        notice.close()

    def test_template_has_two_aligned_controls_and_unicode_cancel_button(self):
        raw = _dialog_template("Ожидаем…")
        style, extended, count, *_ = struct.unpack_from("<LLHhhhh", raw)
        self.assertTrue(style & 0x10000000)  # The modeless notice is visible.
        self.assertEqual(count, 2)
        cursor = 18 + 4  # Empty menu and predefined dialog class.
        def string():
            nonlocal cursor
            start = cursor
            while raw[cursor:cursor + 2] != b"\0\0":
                cursor += 2
            text = raw[start:cursor].decode("utf-16-le")
            cursor += 2
            return text
        self.assertEqual(string(), "BlockHarbor — запуск")
        cursor += 2  # Point size precedes the typeface.
        self.assertEqual(string(), "Segoe UI")
        controls = []
        for _ in range(count):
            cursor = (cursor + 3) & ~3
            _, _, _, _, _, _, identifier = struct.unpack_from("<LLhhhhH", raw, cursor)
            cursor += 18
            ordinal, class_id = struct.unpack_from("<HH", raw, cursor)
            cursor += 4
            self.assertEqual(ordinal, 0xFFFF)
            controls.append((identifier, class_id, string()))
            self.assertEqual(struct.unpack_from("<H", raw, cursor)[0], 0)
            cursor += 2
        self.assertEqual(controls, [(100, 0x82, "Ожидаем…"), (2, 0x80, "Отмена")])
        self.assertEqual(cursor, len(raw))

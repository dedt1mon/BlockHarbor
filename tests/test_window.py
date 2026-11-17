import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import window


class DesktopWindowTests(unittest.TestCase):
    def test_initial_outer_frame_at_scale_and_negative_monitor_origin(self):
        self.assertEqual(
            window.window_bounds(
                (0, 0, 800, 600), (-2560, 0, 0, 1440), 144, opening=True
            ),
            (-2300, 90, 2040, 1260),
        )
        self.assertEqual(
            window.window_bounds((0, 0, 800, 600), (0, 0, 1366, 728), 96, opening=True),
            (3, 0, 1360, 728),
        )

    def test_resize_allows_smaller_and_limits_maximize_and_one_axis(self):
        work = (0, 0, 1920, 1040)
        self.assertIsNone(window.window_bounds((50, 40, 1000, 600), work, 96))
        self.assertEqual(
            window.window_bounds((50, 40, 1500, 600), work, 96), (50, 40, 1360, 560)
        )
        self.assertEqual(
            window.window_bounds(work, work, 96, maximized=True), (280, 100, 1360, 840)
        )

    def test_standalone_scopes_profile_and_registers_only_its_issued_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            host = window.DesktopWindow(temp)
            api = Mock()
            api.hwnd = None
            api.register.side_effect = lambda marker: setattr(api, "hwnd", 20) or True
            with (
                patch.object(window.sys, "platform", "win32"),
                patch.object(
                    window, "browser_executable", return_value=Path(temp) / "edge.exe"
                ),
                patch.object(window, "NativeFrame", return_value=api),
                patch.object(window.subprocess, "Popen") as spawn,
            ):
                host.show("http://127.0.0.1:7501/#test-token")
                args = spawn.call_args.args[0]
                self.assertIn(
                    "--user-data-dir=" + str(Path(temp).resolve() / "window-profile"),
                    args,
                )
                self.assertIn(
                    "--app=http://127.0.0.1:7501/?window="
                    + host.marker
                    + "#test-token",
                    args,
                )
                self.assertIn("--window-size=1360,840", args)
                self.assertFalse(host.adjust("another-marker"))
                api.register.assert_not_called()
                self.assertTrue(host.adjust(host.marker))
                api.constrain.assert_called_with(opening=True)
                self.assertTrue(host.adjust(host.marker))
                api.constrain.assert_called_with(opening=False)
                api.focus.return_value = True
                host.show("http://127.0.0.1:7501/#test-token")
                self.assertEqual(spawn.call_count, 1)

    def test_default_browser_fallback_never_receives_native_bounds_request(self):
        with tempfile.TemporaryDirectory() as temp:
            host = window.DesktopWindow(temp)
            with (
                patch.object(window.sys, "platform", "win32"),
                patch.object(window, "browser_executable", return_value=None),
                patch.object(window.webbrowser, "open", return_value=True) as browser,
            ):
                host.show("http://127.0.0.1:90/#token")
            browser.assert_called_once_with("http://127.0.0.1:90/#token")
            self.assertFalse(host.adjust(""))

    def test_failed_native_host_falls_back_without_a_marker(self):
        url = "http://127.0.0.1:9500/#session"
        for unavailable in ("api", "process"):
            with (
                self.subTest(unavailable=unavailable),
                tempfile.TemporaryDirectory() as temp,
            ):
                host = window.DesktopWindow(temp)
                with (
                    patch.object(window.sys, "platform", "win32"),
                    patch.object(
                        window,
                        "browser_executable",
                        return_value=Path(temp) / "edge.exe",
                    ),
                    patch.object(
                        window,
                        "NativeFrame",
                        side_effect=AttributeError("missing entry point")
                        if unavailable == "api"
                        else None,
                    ),
                    patch.object(
                        window.subprocess,
                        "Popen",
                        side_effect=OSError("missing executable"),
                    ),
                    patch.object(
                        window.webbrowser, "open", return_value=True
                    ) as browser,
                ):
                    host.show(url)
                browser.assert_called_once_with(url)
                self.assertIsNone(host.native)
                self.assertFalse(host.adjust(host.marker))


class WindowIconTests(unittest.TestCase):
    def create_frame(self, api):
        with (
            patch.object(window.ctypes, "WinDLL", return_value=api, create=True),
            patch.object(window.ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True),
        ):
            frame = window.NativeFrame()
        frame.hwnd, frame.pid = 17, 320
        frame.frame_property = "BlockHarbor.test-frame"
        frame.registration_marker = "marker"
        frame._identity = Mock(return_value=("Chrome_WidgetWin_1", "BlockHarbor", 320))
        api.GetPropW.return_value = 1
        return frame

    def test_native_icon_keeps_64_bit_handles_and_scopes_the_registered_window(self):
        api = Mock()
        handles = [0x112233445566, 0x223344556677]
        api.LoadImageW.side_effect = handles
        api.SendMessageTimeoutW.return_value = 1
        with patch.dict(window._WINDOW_ICONS, {}, clear=True):
            frame = self.create_frame(api)
            self.assertTrue(frame.set_icons())
            self.assertEqual(api.LoadImageW.restype, window.wintypes.HANDLE)
            self.assertEqual(
                api.SendMessageTimeoutW.argtypes[2:4],
                [ctypes.c_size_t, ctypes.c_ssize_t],
            )
            calls = api.SendMessageTimeoutW.call_args_list
            self.assertEqual([c.args[3] for c in calls], handles)
            self.assertEqual([c.args[2] for c in calls], [1, 0])
            self.assertTrue(
                all(
                    c.args[0:2] == (17, 0x80) and c.args[4:6] == (3, 1000)
                    for c in calls
                )
            )
            self.assertTrue(frame.set_icons())
            self.assertEqual(api.LoadImageW.call_count, 2)
            frame._identity.return_value = ("Chrome_WidgetWin_1", "BlockHarbor", 999)
            self.assertFalse(frame.set_icons())
            self.assertEqual(api.SendMessageTimeoutW.call_count, 4)

    def test_missing_icon_does_not_send_invalid_handles(self):
        api = Mock()
        api.LoadImageW.return_value = 0
        with patch.dict(window._WINDOW_ICONS, {}, clear=True):
            frame = self.create_frame(api)
            self.assertFalse(frame.set_icons())
            api.SendMessageTimeoutW.assert_not_called()
            self.assertEqual(window._WINDOW_ICONS, {})

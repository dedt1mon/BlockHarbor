import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from launcher import game, prism, prism_watch, window
from main import App


def native_frame():
    """Keep the real registration/hide/focus logic, replacing only Win32 calls."""
    frame = window.NativeFrame.__new__(window.NativeFrame)
    frame.user = Mock()
    frame.user.IsIconic.return_value = False
    frame.user.LoadImageW.return_value = 0
    frame.hwnd, frame.pid = None, 0
    frame.frame_property = ""
    frame.registration_marker = ""
    properties = {}

    def set_property(hwnd, key, value):
        properties[(hwnd, key)] = value
        return True

    frame.user.SetPropW.side_effect = set_property
    frame.user.GetPropW.side_effect = lambda hwnd, key: properties.get((hwnd, key), 0)
    frame.enum_callback = lambda callback: callback
    return frame


class RegisteredFrameLifecycleTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.host = window.DesktopWindow(root)
        self.frame = native_frame()
        self.host.native = self.frame
        self.host.url = "http://127.0.0.1:7001/#test"
        self.host.marker = "a" * 24

    def registered(self, hwnd, pid):
        self.frame.hwnd, self.frame.pid = hwnd, pid
        self.frame.frame_property = "BlockHarbor.frame." + self.host.marker
        self.frame.registration_marker = self.host.marker
        self.frame.user.SetPropW(hwnd, self.frame.frame_property, 1)

    def test_profile_and_localized_brand_suffixes_preserve_registration(self):
        caption = "BlockHarbor [" + self.host.marker + "]"
        edge = " — Личный: Microsoft\u200b Edge"
        self.assertTrue(self.frame._caption(caption + edge, caption))
        self.assertTrue(self.frame._caption("BlockHarbor" + edge, "BlockHarbor"))
        self.assertFalse(self.frame._caption("Журнал " + caption + edge, caption))

    def test_late_registration_hides_only_the_marked_frame_and_restores_it_once(self):
        marked = "BlockHarbor [" + self.host.marker + "]"
        identities = {
            21: ("Chrome_WidgetWin_1", "BlockHarbor", 110),
            22: ("Chrome_WidgetWin_1", "Documentation: " + marked, 120),
            23: ("Chrome_WidgetWin_1", marked, 130),
        }
        self.frame._identity = identities.__getitem__

        def enumerate_windows(callback, _):
            for hwnd in identities:
                if not callback(hwnd, 0):
                    break

        self.frame.user.EnumWindows.side_effect = enumerate_windows
        self.host.game_started()
        self.frame.user.ShowWindow.assert_not_called()
        self.assertFalse(self.host.adjust("old-or-unrelated-window"))
        self.frame.user.EnumWindows.assert_not_called()
        self.assertTrue(self.host.adjust(self.host.marker))
        self.frame.user.ShowWindow.assert_called_once_with(23, 0)
        identities[23] = ("Chrome_WidgetWin_1", "BlockHarbor", 130)
        self.host.game_ended()
        self.host.game_ended()
        self.assertEqual(
            self.frame.user.ShowWindow.call_args_list, [call(23, 0), call(23, 5)]
        )
        self.frame.user.SetForegroundWindow.assert_called_once_with(23)

    def test_reused_hwnd_never_hides_or_focuses_an_unrelated_window(self):
        self.registered(42, 123)
        for identity in (
            ("Chrome_WidgetWin_1", "BlockHarbor", 456),
            ("Notepad", "BlockHarbor", 123),
            ("Chrome_WidgetWin_1", "BlockHarbor setup notes - Google Chrome", 123),
        ):
            with self.subTest(identity=identity):
                self.frame._identity = Mock(return_value=identity)
                self.assertFalse(self.frame.hide())
                self.assertFalse(self.frame.focus())
        # Windows destroys HWND properties when it recycles a handle, even in the same PID.
        self.frame._identity.return_value = ("Chrome_WidgetWin_1", "BlockHarbor", 123)
        self.frame.user.SetPropW(42, self.frame.frame_property, 0)
        self.assertFalse(self.frame.hide())
        self.assertFalse(self.frame.focus())
        self.frame.user.ShowWindow.assert_not_called()
        self.frame.user.SetForegroundWindow.assert_not_called()

    def test_explicit_show_during_game_reactivates_and_does_not_hide_on_next_adjust(
        self,
    ):
        self.registered(77, 1200)
        self.frame._identity = Mock(
            return_value=("Chrome_WidgetWin_1", "BlockHarbor", 1200)
        )
        self.host.game_started()
        self.frame.user.IsIconic.return_value = True
        self.host.show(self.host.url)
        with patch.object(self.frame, "constrain") as constrain:
            self.assertTrue(self.host.adjust(self.host.marker))
            constrain.assert_called_once_with(opening=False)
        self.frame.user.IsIconic.return_value = False
        self.host.game_ended()
        self.host.game_ended()
        self.assertEqual(
            self.frame.user.ShowWindow.call_args_list,
            [call(77, 0), call(77, window.SW_RESTORE), call(77, 5)],
        )
        self.assertEqual(
            self.frame.user.SetForegroundWindow.call_args_list, [call(77), call(77)]
        )

    def test_firefox_registration_recognizes_its_scoped_title_in_both_phases(self):
        self.frame._identity = Mock(
            return_value=(
                "MozillaWindowClass",
                "BlockHarbor [" + self.host.marker + "] — Mozilla Firefox",
                901,
            )
        )
        self.frame.user.EnumWindows.side_effect = lambda callback, _: callback(84, 0)
        self.assertTrue(self.frame.register(self.host.marker))
        self.assertTrue(self.frame.hide())
        self.frame._identity.return_value = (
            "MozillaWindowClass",
            "BlockHarbor — Mozilla Firefox",
            901,
        )
        self.assertTrue(self.frame.focus())
        self.assertEqual(
            self.frame.user.ShowWindow.call_args_list, [call(84, 0), call(84, 5)]
        )

    def test_default_browser_window_stays_visible_and_no_new_tab_opens_on_exit(self):
        self.host.native = None
        with (
            patch.object(window, "browser_executable", return_value=None),
            patch.object(window.webbrowser, "open", return_value=True) as open_tab,
        ):
            self.host.show(self.host.url)
            self.host.game_started()
            self.assertFalse(self.host.adjust("default-browser"))
            self.host.game_ended()
            self.host.game_ended()
        open_tab.assert_called_once_with(self.host.url)


class GameProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.app = App(Path(root))
        self.pid = self.app.store.create("Остров друзей", "1.20.1", "fabric")
        self.frame = Mock(spec=window.NativeFrame)
        self.frame.focus.return_value = True
        self.app.window.native = self.frame
        self.app.window.url = "http://127.0.0.1:7001/#test"
        self.enterContext(patch("launcher.e4steam.ensure"))
        self.enterContext(patch("launcher.skin_sync.prepare"))
        self.prepare = self.enterContext(patch.object(game, "install"))

    def play(self):
        self.app.action("play", {"id": self.pid, "nickname": "HarborTest"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.app.job_snapshot()
            if not job["busy"]:
                return job
            time.sleep(0.005)
        self.fail("Play job did not finish")

    def test_failed_preparation_spawn_and_immediate_jvm_exit_never_hide_frame(self):
        with patch.object(game, "launch_local") as launch:
            self.prepare.side_effect = OSError("network unavailable")
            self.assertFalse(self.play()["ok"])
            launch.assert_not_called()
            self.prepare.side_effect = None
            launch.side_effect = OSError("Java unavailable")
            self.assertFalse(self.play()["ok"])
            launch.side_effect = None
            launch.return_value.poll.return_value = 5
            self.assertEqual(self.play()["gameExit"]["code"], 5)
        self.frame.hide.assert_not_called()
        self.frame.focus.assert_not_called()

    def test_direct_java_crash_restores_once_and_report_survives_later_polls(self):
        child = Mock()
        child.poll.return_value = None
        with patch.object(game, "launch_local", return_value=child):
            self.assertTrue(self.play()["gameRunning"])
        self.frame.hide.assert_called_once_with()
        self.frame.focus.assert_not_called()
        child.poll.return_value = 7
        event = self.app.process_state()["gameExit"]
        self.assertEqual(event["code"], 7)
        self.assertIn(self.app.store.profile(self.pid)["name"], event["message"])
        self.assertEqual(self.app.job_snapshot()["gameExit"]["id"], event["id"])
        self.assertFalse(self.app.game_running())
        self.frame.focus.assert_called_once_with()

    def test_signed_in_direct_launch_restores_after_clean_exit(self):
        self.app.store.set("mode", "direct")
        self.app.account = {"name": "HarborOnline", "access_token": "test-session"}
        child = Mock()
        child.poll.return_value = None
        with patch.object(game, "launch", return_value=child) as authenticated_launch:
            self.assertTrue(self.play()["gameRunning"])
        self.assertIs(authenticated_launch.call_args.args[2], self.app.account)
        self.frame.hide.assert_called_once_with()
        child.poll.return_value = 0
        self.assertEqual(
            self.app.process_state(), {"gameRunning": False, "gameExit": None}
        )
        self.assertFalse(self.app.job_snapshot()["gameRunning"])
        self.frame.focus.assert_called_once_with()

    def test_forwarded_prism_cli_is_not_a_game_and_restore_waits_for_both_jvms(self):
        self.app.store.set("mode", "prism")
        engine = self.app.store.root / "prismlauncher.exe"
        cli = Mock()
        cli.wait.return_value = cli.poll.return_value = 0  # Forwarded via Prism IPC.
        with (
            patch.object(prism, "sys", SimpleNamespace(platform="win32")),
            patch.object(prism, "_engine", return_value=engine),
            patch.object(prism, "_instance", return_value="selected-wrapper"),
            patch.object(prism.subprocess, "Popen", return_value=cli),
            patch.object(prism_watch, "observe") as observe,
        ):
            job = self.play()
        self.assertTrue(job["ok"], job)
        self.assertFalse(job["gameRunning"])
        wrapper, observed_engine, game_arrived = observe.call_args.args
        self.assertEqual(wrapper.name, "selected-wrapper")
        self.assertEqual(observed_engine, engine)
        self.frame.hide.assert_not_called()
        self.frame.focus.assert_not_called()

        # Another build can start while Prism signs in. The reserved build itself
        # stays protected so two engines never write the same game directory.
        direct, prism_jvm = Mock(), Mock()
        direct.poll.return_value = prism_jvm.poll.return_value = None
        self.app.store.set("mode", "local")
        with self.assertRaisesRegex(ValueError, "уже запущена"):
            self.app.action("play", {"id": self.pid})
        self.pid = self.app.store.create("Second", "1.20.1", "fabric")
        with patch.object(game, "launch_local", return_value=direct):
            self.assertTrue(self.play()["gameRunning"])
        game_arrived(prism_jvm)
        self.frame.hide.assert_called_once_with()
        direct.poll.return_value = 0
        self.assertTrue(self.app.job_snapshot()["gameRunning"])
        self.frame.focus.assert_not_called()
        prism_jvm.poll.return_value = 9
        ended = self.app.job_snapshot()
        self.assertFalse(ended["gameRunning"])
        self.assertEqual(ended["gameExit"]["code"], 9)
        self.assertFalse(self.app.game_running())
        self.frame.focus.assert_called_once_with()

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import game
from launcher.store import Store
from main import App


class PlayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        networking = patch("launcher.e4steam.ensure")
        networking.start()
        self.addCleanup(networking.stop)
        self.root = Path(directory.name)
        self.app = App(self.root)
        self.addCleanup(self.app.close)
        self.pid = self.app.store.create("Остров друзей", "1.20.1", "fabric")

    def wait_for_job(self):
        for _ in range(300):
            state = self.app.job_snapshot()
            if not state["busy"]:
                return state
            time.sleep(0.01)
        self.fail("Launcher job did not finish")

    def test_existing_official_installation_upgrades_in_place(self):
        existing = Store(self.root)
        existing.set("mode", "official")
        existing.set("nickname", "WorldKeeper")
        world = existing.folder(self.pid) / "saves" / "My World" / "level.dat"
        world.parent.mkdir(parents=True)
        world.write_bytes(b"save fixture")
        updated = App(self.root)
        self.addCleanup(updated.close)
        self.assertEqual(updated.state()["active"], self.pid)
        self.assertEqual(updated.state()["mode"], "local")
        self.assertEqual(updated.state()["nickname"], "WorldKeeper")
        self.assertEqual(world.read_bytes(), b"save fixture")

    def test_legacy_alias_preserves_offline_name(self):
        self.app.store.set("mode", "alias")
        self.app.store.set("nickname", "AliasAfterUpdate")
        self.app.store.set("realName", "OfficialName")
        reopened = App(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.player_name(), "AliasAfterUpdate")

    def test_one_click_launches_selected_native_java_and_reports_exit(self):
        native = Mock()
        native.poll.return_value = None
        with (
            patch.object(game, "install") as install,
            patch.object(game, "launch_local", return_value=native) as launch,
            patch.object(
                game, "open_official", side_effect=AssertionError("unexpected handoff")
            ),
        ):
            self.app.action("play", {"id": self.pid, "nickname": "DedTimon"})
            result = self.wait_for_job()
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["gameRunning"])
            cache = self.root / "game-cache"
            folder = self.app.store.folder(self.pid)
            self.assertEqual(
                install.call_args.args[:4], ("1.20.1", "fabric", cache, folder)
            )
            self.assertEqual(launch.call_args.args, (cache, folder, "DedTimon"))
            self.assertEqual(self.app.store.get("nickname"), "DedTimon")
            with self.assertRaises(ValueError):
                self.app.action("play", {"id": self.pid})
            native.poll.return_value = 0
            self.assertFalse(self.app.state()["gameRunning"])
            self.assertFalse(self.app.job_snapshot()["gameRunning"])
            self.app.action(
                "create", {"name": "Чистая", "version": "1.21.1", "loader": "vanilla"}
            )
            second = self.app.store.get("active")
            self.app.action("play", {"id": second})
            self.assertTrue(self.wait_for_job()["ok"])
            self.assertEqual(
                install.call_args.args[:4],
                ("1.21.1", "vanilla", cache, self.app.store.folder(second)),
            )
            self.assertEqual(launch.call_args.args[2], "DedTimon")

    def test_failed_download_leaves_play_available(self):
        with (
            patch.object(
                game, "install", side_effect=ValueError("Скачивание прервано")
            ),
            patch.object(game, "launch_local") as launch,
        ):
            self.app.action("play", {"id": self.pid, "nickname": "SavedBeforeLoad"})
            state = self.wait_for_job()
            self.assertFalse(state["ok"])
            self.assertFalse(state["gameRunning"])
            self.assertEqual(state["message"], "Скачивание прервано")
            launch.assert_not_called()
        self.assertEqual(self.app.store.get("nickname"), "SavedBeforeLoad")

    def test_invalid_explicit_launch_name_is_not_replaced_with_saved_default(self):
        self.app.action("saveNickname", {"nickname": "SavedPlayer"})
        for invalid in ("", " ", "!", None):
            with self.subTest(nickname=invalid), self.assertRaises(ValueError):
                self.app.action("play", {"id": self.pid, "nickname": invalid})
        self.assertEqual(self.app.store.get("nickname"), "SavedPlayer")
        self.assertFalse(self.app.job_snapshot()["busy"])

    def test_nickname_is_saved_before_skin_preparation_can_fail(self):
        image = self.root / "test-skin.png"
        image.write_bytes(b"placeholder, skin load must not be reached")
        with (
            patch.object(self.app, "skin_path", return_value=image),
            patch("launcher.aliases.ensure_skin_support", side_effect=ValueError("Не удалось загрузить скин")),
            patch.object(game, "install") as install,
        ):
            self.app.action("play", {"id": self.pid, "nickname": "RememberMe"})
            result = self.wait_for_job()
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Не удалось загрузить скин")
        self.assertEqual(self.app.store.get("nickname"), "RememberMe")
        install.assert_not_called()

    def test_crash_after_startup_is_reported_once_per_process(self):
        child = Mock()
        child.poll.return_value = None
        with (
            patch.object(game, "install"),
            patch.object(game, "launch_local", return_value=child),
        ):
            self.app.action("play", {"id": self.pid})
            self.assertTrue(self.wait_for_job()["gameRunning"])
        child.poll.return_value = 7
        event = self.app.job_snapshot()["gameExit"]
        self.assertEqual(event["code"], 7)
        self.assertIn("Остров друзей", event["message"])
        self.assertEqual(self.app.state()["gameExit"]["id"], event["id"])
        self.assertFalse(self.app.game_running())

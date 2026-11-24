import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import e4steam, friends, game
from main import App


class FriendsTests(unittest.TestCase):
    def setUp(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.app = App(Path(data.name))
        self.addCleanup(self.app.close)
        self.store = self.app.store
        self.pid = self.store.create("Остров друзей", "1.20.1", "fabric")

    def job(self, action, **params):
        self.app.action(action, params)
        for _ in range(300):
            state = self.app.job_snapshot()
            if not state["busy"]:
                return state
            time.sleep(0.01)
        self.fail("Background task timed out")

    def test_launches_friend_list_in_installed_steam(self):
        with (
            patch.object(friends.sys, "platform", "win32"),
            patch.object(friends, "_windows_steam", return_value=(True, None)),
            patch.object(os, "startfile", create=True) as shell,
        ):
            self.app.action("openSteam", {})
            shell.assert_called_once_with("steam://open/friends")
            shell.side_effect = OSError()
            with self.assertRaisesRegex(RuntimeError, "Steam"):
                self.app.action("openSteam", {})

    def test_steam_compatibility_and_existing_selection_survive_reopening(self):
        first = self.app.state()
        self.assertTrue(first["steam"]["supported"])
        self.assertFalse(first["steam"]["installed"])
        forge = self.store.create("Forge", "1.20.1", "forge")
        self.assertTrue(self.app.state()["steam"]["supported"])
        self.assertEqual(self.app.state()["active"], forge)
        old = self.store.create("Мой мир", "1.18.2", "vanilla")
        with self.store.connection() as db:
            db.execute("DELETE FROM settings WHERE key='steamStarterAdded'")
        reopened = App(self.store.root)
        try:
            updated = reopened.state()
        finally:
            reopened.close()
        self.assertEqual(updated["active"], old)
        self.assertEqual([p["id"] for p in updated["profiles"]], [self.pid, forge, old])

    def test_steam_on_vanilla_keeps_world_and_profile_id(self):
        pid = self.store.create("Мой мир", "1.20.1", "vanilla")
        folder = self.store.folder(pid)
        save = folder / "saves" / "My World" / "level.dat"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"unchanged world bytes")
        self.assertTrue(self.app.state()["steam"]["canEnable"])
        with patch.object(
            e4steam, "ensure", side_effect=ValueError("download interrupted")
        ):
            self.assertFalse(self.job("enableSteam", id=pid)["ok"])
            self.assertEqual(self.store.profile(pid)["loader"], "vanilla")
        with patch.object(e4steam, "ensure") as prepare:
            self.assertTrue(self.job("enableSteam", id=pid)["ok"])
            self.assertEqual(prepare.call_args.args[:3], (folder, "1.20.1", "fabric"))
        self.assertEqual(self.store.profile(pid)["loader"], "fabric")
        self.assertEqual(self.store.get("active"), pid)
        self.assertEqual(save.read_bytes(), b"unchanged world bytes")

    def test_prepares_networking_before_java_or_prism(self):
        for mode in ["local", "direct", "prism"]:
            self.store.set("mode", mode)
            self.app.account = {"name": "Player"}
            steps = []
            child = Mock()
            child.poll.return_value = 0
            with (
                patch.object(
                    e4steam,
                    "ensure",
                    side_effect=lambda *a, **kw: steps.append("steam"),
                ),
                patch.object(
                    game, "install", side_effect=lambda *a, **kw: steps.append("game")
                ),
                patch.object(game, "launch_local", return_value=child),
                patch.object(game, "launch", return_value=child),
                patch(
                    "launcher.prism.launch",
                    side_effect=lambda *a, **kw: steps.append("prism"),
                ),
            ):
                state = self.job("play", id=self.pid)
                self.assertTrue(state["ok"], state)
                self.assertEqual(
                    steps, ["steam", "prism" if mode == "prism" else "game"]
                )

    def test_vanilla_play_preserves_unmodified_vanilla(self):
        pid = self.store.create("Чистая", "1.20.1", "vanilla")
        with (
            patch.object(e4steam, "ensure") as prepare,
            patch.object(game, "install"),
            patch.object(game, "launch_local"),
        ):
            self.assertTrue(self.job("play", id=pid)["ok"])
            prepare.assert_not_called()
        self.assertEqual(self.store.profile(pid)["loader"], "vanilla")

    def test_core_file_cannot_be_deleted_if_complete_and_managed(self):
        name = next(iter(e4steam.protected_names("1.20.1", "fabric")))
        f = self.store.folder(self.pid) / "mods" / name
        f.write_bytes(b"fixture")
        with patch.object(e4steam, "installed", return_value=True):
            row = self.app.state()["contents"]["mods"][0]
            self.assertTrue(row["managed"])
            with self.assertRaisesRegex(ValueError, "Steam"):
                self.app.action(
                    "remove", {"id": self.pid, "kind": "mods", "name": name}
                )
        with patch.object(e4steam, "installed", return_value=False):
            self.app.action("remove", {"id": self.pid, "kind": "mods", "name": name})
        self.assertFalse(f.exists())

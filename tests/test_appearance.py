import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import aliases, game, skins
from main import App
from PIL import Image


class AppearanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = App(Path(temp.name))
        self.store = self.app.store
        self.pid = self.store.create("Остров друзей", "1.20.1", "fabric")
        image = io.BytesIO()
        Image.new("RGBA", (64, 64), (130, 20, 255, 255)).save(image, format="PNG")
        self.png = image.getvalue()

    def completed(self, job):
        for _ in range(300):
            snapshot = self.app.job_snapshot()
            if snapshot["id"] == job["job"] and not snapshot["busy"]:
                return snapshot
            time.sleep(0.01)
        self.fail("Background operation did not finish")

    def save(self, pid, nickname="DedTimon", model="slim", png=None, **options):
        return self.completed(
            self.app.save_appearance(
                {"id": pid, "nickname": nickname, "model": model, **options},
                self.png if png is None else png,
            )
        )

    def test_forge_first_save_and_switch_to_fabric_uses_actual_launch_name(self):
        forge = self.store.create("Forge", "1.20.1", "forge")
        with patch.object(aliases, "ensure_skin_support") as install:
            self.assertTrue(self.save(forge)["ok"])
            install.assert_called_once_with(self.store.folder(forge), "1.20.1", "forge")
            self.assertTrue(self.app.skin_path().is_file())
            self.assertEqual(self.store.get("nickname"), "DedTimon")
            folder = self.store.folder(self.pid)
            with (
                patch.object(game, "install"),
                patch.object(game, "launch_local"),
                patch("launcher.e4steam.ensure"),
            ):
                result = self.completed(
                    self.app.action("play", {"id": self.pid, "nickname": "NewPlayer"})
                )
            self.assertTrue(result["ok"], result)
            install.assert_called_with(folder, "1.20.1", "fabric")
            config = json.loads(
                (folder / "CustomSkinLoader/CustomSkinLoader.json").read_text()
            )
            source = config["loadlist"][0]
            self.assertEqual(source["model"], "slim")
            self.assertTrue(
                (
                    folder
                    / "CustomSkinLoader"
                    / source["skin"].replace("{USERNAME}", "NewPlayer")
                ).is_file()
            )

    def test_vanilla_opt_in_preserves_save_and_does_not_force_nickname_only(self):
        vanilla = self.store.create("Мир", "1.20.1", "vanilla")
        world = self.store.folder(vanilla) / "saves" / "world" / "level.dat"
        world.parent.mkdir(parents=True)
        world.write_bytes(b"world")
        with patch.object(aliases, "ensure_skin_support"):
            with self.assertRaisesRegex(ValueError, "загрузчиком"):
                self.save(vanilla)
            self.assertTrue(self.save(self.pid)["ok"])
            self.assertTrue(self.save(vanilla, "NewNick", png=b"")["ok"])
            self.assertEqual(self.store.profile(vanilla)["loader"], "vanilla")
            self.assertTrue(self.save(vanilla, fabric="1")["ok"])
            self.assertEqual(self.store.profile(vanilla)["loader"], "fabric")
        self.assertEqual(world.read_bytes(), b"world")

    def test_failed_install_keeps_committed_settings_and_skin(self):
        with patch.object(aliases, "ensure_skin_support"):
            self.assertTrue(self.save(self.pid)["ok"])
        previous = self.app.skin_path()
        with patch.object(
            aliases, "ensure_skin_support", side_effect=ValueError("ошибка загрузки")
        ):
            result = self.save(self.pid, "AnotherName", "classic")
        self.assertFalse(result["ok"])
        self.assertEqual(self.app.skin_path(), previous)
        self.assertEqual(self.store.get("nickname"), "DedTimon")
        self.assertEqual(self.store.get("model"), "slim")

    def test_explicit_java_png_save_requires_neither_build_changes_nor_login(self):
        self.store.set("mode", "direct")
        self.store.set("nickname", "OldNick")
        cases = [
            self.store.create("Vanilla", "1.20.1", "vanilla"),
            self.store.create("Old Forge", "1.7.1", "forge"),
            self.pid,
        ]
        with (
            patch.object(self.app, "game_running", return_value=True),
            patch.object(aliases, "ensure_skin_support") as install,
            patch.object(aliases, "apply_alias") as alias,
        ):
            for pid in cases:
                before = self.store.profile(pid)
                folder = self.store.folder(pid)
                files = set(folder.rglob("*"))
                args = {
                    "id": pid,
                    "nickname": "!",
                    "realName": "",
                    "model": "slim",
                    "skinDestination": "java",
                }
                result = self.completed(self.app.save_appearance(args, self.png))
                self.assertTrue(result["ok"], result["message"])
                self.assertEqual(self.store.profile(pid), before)
                self.assertEqual(set(folder.rglob("*")), files)
                self.assertEqual(self.store.get("model"), "slim")
                self.assertEqual(self.store.get("nickname"), "OldNick")
                self.assertEqual(
                    self.app.skin_path().read_bytes(),
                    skins.appearance_skin(self.png, "slim"),
                )
            install.assert_not_called()
            alias.assert_not_called()
        # The account upload flow also works when all builds have been archived.
        for profile in self.store.all():
            self.store.move_profile(profile["id"])
        result = self.completed(
            self.app.save_appearance(
                {"skinDestination": "java", "model": "classic"}, None
            )
        )
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(self.store.get("model"), "classic")
        digest = self.store.get("skinDigest")
        with self.assertRaisesRegex(ValueError, "PNG"):
            self.app.save_appearance(
                {"skinDestination": "java", "model": "slim"}, b"broken"
            )
        self.assertEqual(self.store.get("skinDigest"), digest)
        self.assertEqual(self.store.get("model"), "classic")

    def test_two_players_have_distinct_explicit_models_and_local_priority(self):
        folder = self.store.folder(self.pid)
        root = folder / "CustomSkinLoader"
        root.mkdir()
        config = root / "CustomSkinLoader.json"
        custom = {"name": "Existing site", "type": "MojangAPI"}
        config.write_text(json.dumps({"loadlist": [custom], "threadPoolSize": 6}))
        for nick, model in (
            ("Friend", "slim"),
            ("DedTimon", "slim"),
            ("DedTimon", "classic"),
        ):
            skins.install_local_skin(self.png, folder, nick, model)
        cfg = json.loads(config.read_text())
        first, second, last = cfg["loadlist"]
        self.assertEqual(
            (first["model"], second["model"], last), ("default", "slim", custom)
        )
        self.assertEqual(cfg["threadPoolSize"], 6)
        for source, right, wrong in (
            (first, "DedTimon", "Friend"),
            (second, "Friend", "DedTimon"),
        ):
            self.assertTrue(
                (root / source["skin"].replace("{USERNAME}", right)).is_file()
            )
            self.assertFalse(
                (root / source["skin"].replace("{USERNAME}", wrong)).is_file()
            )

    def test_release_matches_loader_and_version(self):
        self.assertIn("_ForgeV2-", aliases.skin_release("1.20.1", "forge").filename)
        self.assertIn("_Fabric-", aliases.skin_release("1.20.1", "fabric").filename)
        self.assertIn("_Universal-", aliases.skin_release("26.3", "neoforge").filename)
        self.assertIsNone(aliases.skin_release("1.8", "fabric"))
        self.assertIsNone(aliases.skin_release("1.20.1", "vanilla"))

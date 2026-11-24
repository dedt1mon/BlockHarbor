import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import e4steam, game
from main import App


class CreateBuildTests(unittest.TestCase):
    def setUp(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.app = App(Path(data.name))
        self.addCleanup(self.app.close)

    def test_new_data_stays_empty_through_restart_without_seeding_instances(self):
        for app in (self.app, App(self.app.store.root)):
            self.addCleanup(app.close)
            state = app.state()
            self.assertEqual(state["profiles"], [])
            self.assertEqual(state["active"], "")
            self.assertEqual(state["deletedProfiles"], [])
            self.assertEqual(
                state["contents"], {"mods": [], "shaderpacks": [], "resourcepacks": []}
            )
            self.assertFalse(state["steam"]["supported"])
            self.assertEqual(list(app.store.instances.iterdir()), [])

    def test_upgrade_preserves_both_original_profile_names_and_custom_data(self):
        store = self.app.store
        fabric = store.create("Остров друзей", "1.20.1", "fabric")
        forge = store.create("Остров друзей · Forge", "1.20.1", "forge")
        level = store.folder(fabric) / "saves" / "Первый мир" / "level.dat"
        level.parent.mkdir(parents=True)
        level.write_bytes(b"existing world")
        archived = store.create("Перенесено", "1.19.2", "vanilla")
        store.move_profile(archived)
        store.set_many({"active": forge, "nickname": "OldPlayer"})
        with store.connection() as db:
            db.execute("DELETE FROM settings WHERE key='steamStarterAdded'")
        reopened = App(store.root)
        self.addCleanup(reopened.close)
        state = reopened.state()
        self.assertEqual([p["id"] for p in state["profiles"]], [fabric, forge])
        self.assertEqual([p["id"] for p in state["deletedProfiles"]], [archived])
        self.assertEqual(state["active"], forge)
        self.assertEqual(state["nickname"], "OldPlayer")
        self.assertEqual(level.read_bytes(), b"existing world")

    def result(self, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.app.job_snapshot()
            if state["id"] == job["job"] and not state["busy"]:
                return state
            time.sleep(0.01)
        self.fail("Creation job timed out")

    def test_new_coop_build_prepares_before_activation_and_can_be_retried(self):
        store = self.app.store
        old = store.get("active")
        prior = set(store.instances.iterdir())
        wanted = dict(name="Остров", loader="forge", version="1.20.1")
        with patch.object(e4steam, "ensure", side_effect=ValueError("Сбой загрузки")):
            result = self.result(self.app.action("create", wanted))
        self.assertFalse(result["ok"])
        self.assertIn("Сбой загрузки", result["message"])
        self.assertEqual(set(store.instances.iterdir()), prior)
        self.assertEqual(store.get("active"), old)

        entered, finish = threading.Event(), threading.Event()

        def prepare(folder, version, loader, status, *, cache_dir):
            self.assertEqual((version, loader), ("1.20.1", "forge"))
            self.assertEqual(cache_dir, store.root / "steam-mod-cache")
            entered.set()
            self.assertTrue(finish.wait(5))
            (folder / "mods").mkdir()
            (folder / "mods" / "sample.jar").write_bytes(b"ready")

        with patch.object(e4steam, "ensure", side_effect=prepare):
            job = self.app.action("create", wanted)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(store.get("active"), old)
                with self.assertRaisesRegex(ValueError, "операции"):
                    self.app.action("create", {**wanted, "loader": "vanilla"})
            finally:
                finish.set()
            self.assertTrue(self.result(job)["ok"])
        self.assertEqual(len(store.all()), len(prior) + 1)
        self.assertEqual(store.profile(store.get("active"))["name"], "Остров")
        self.assertEqual(
            (store.folder(store.get("active")) / "mods" / "sample.jar").read_bytes(),
            b"ready",
        )

    def test_vanilla_and_old_releases_can_be_created_offline(self):
        with patch.object(e4steam, "ensure") as ensure:
            for loader, version in (("vanilla", "1.20.1"), ("forge", "1.7.10")):
                self.app.action(
                    "create", dict(name=version, loader=loader, version=version)
                )
                self.assertEqual(
                    self.app.store.profile(self.app.store.get("active"))["loader"],
                    loader,
                )
            ensure.assert_not_called()

    def test_release_catalog_initial_offline_refresh_and_legacy_cache(self):
        self.app.store.set("versions", '["1.20.1"]')
        bundled = self.app.state()["versions"]
        self.assertGreaterEqual(len(bundled), 103)
        self.assertEqual(bundled[-1], "1.0")
        self.assertIn("26.3", bundled)
        with patch.object(game, "version_catalog", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                self.app.refresh_releases()
            self.assertEqual(self.app.state()["versions"], bundled)
        updated = ["27.1", *bundled]
        full = [{"id": version, "type": "release"} for version in updated]
        full.insert(1, {"id": "27.1-snapshot-1", "type": "snapshot"})
        with patch.object(game, "version_catalog", return_value=full) as get:
            self.assertEqual(self.app.refresh_releases(), updated)
            self.assertEqual(self.app.refresh_releases(), updated)
            get.assert_called_once()
        self.assertEqual(self.app.version_catalog()[:len(full)], full)
        reopened = App(self.app.store.root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.state()["versions"], updated)
        self.assertEqual(reopened.version_catalog()[:len(full)], full)
        with patch.object(game, "version_catalog", return_value=full) as get:
            self.assertEqual(reopened.refresh_releases(force=True), updated)
            get.assert_called_once()

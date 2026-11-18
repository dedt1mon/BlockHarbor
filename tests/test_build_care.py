import hashlib
import json
import os
import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from PIL import Image

from launcher import compatibility, modrinth
from launcher.build_care import BuildCare, _atomic_json
from launcher.sessions import process_identity
from launcher.store import Store


def jar(path, key="example", version="1.0", depends=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1, "id": key, "version": version, "name": key.title(), "description": "A local test mod", "depends": depends or {}}))
    return path


class BuildCareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "data")
        self.pid = self.store.create("Test", "1.20.2", "fabric")
        self.folder = self.store.folder(self.pid)
        self.care = BuildCare(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def world(self, name="World", data=b"world one"):
        folder = self.folder / "saves" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "level.dat").write_bytes(data)
        (folder / "session.lock").write_bytes(b"lock")
        return folder

    def test_fresh_install_remains_empty(self):
        other = Store(Path(self.temp.name) / "fresh")
        BuildCare(other)
        self.assertEqual([], other.all())

    def test_snapshot_restores_mod_config_pins_and_performance_without_world(self):
        file = jar(self.folder / "mods" / "example.jar")
        config = self.folder / "config" / "nested" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"initial":true}')
        (self.folder / "options.txt").write_text("renderDistance:8\n")
        world = self.world()
        self.care.pin(self.pid, "mods", [file.name], True)
        self.store.set("performanceMemory:" + self.pid, "2048")
        point = self.care.snapshot(self.pid)
        jar(file, version="2.0")
        config.write_text('{"initial":false}')
        self.care.pin(self.pid, "mods", [file.name], False)
        self.store.set("performanceMemory:" + self.pid, "4096")
        (world / "level.dat").write_bytes(b"new world state")
        self.care.restore(self.pid, point)
        self.assertEqual("1.0", self.care.inventory(self.pid)["mods"][0]["version"])
        self.assertTrue(self.care.inventory(self.pid)["mods"][0]["pinned"])
        self.assertEqual('{"initial":true}', config.read_text())
        self.assertEqual("2048", self.store.get("performanceMemory:" + self.pid))
        self.assertEqual(b"new world state", (world / "level.dat").read_bytes())
        self.assertEqual(2, len(self.care.state(self.pid)["snapshots"]))

    def test_world_backup_separate_and_session_lock_excluded(self):
        world = self.world()
        file = jar(self.folder / "mods" / "example.jar")
        point = self.care.snapshot(self.pid, scope="world", world="World")
        archive, _ = self.care._snapshot(self.pid, point)
        with zipfile.ZipFile(archive) as z:
            self.assertNotIn("payload/saves/World/session.lock", z.namelist())
            self.assertNotIn("payload/mods/example.jar", z.namelist())
        (world / "level.dat").write_bytes(b"later world")
        jar(file, version="2.0")
        self.care.restore(self.pid, point)
        self.assertEqual(b"world one", (world / "level.dat").read_bytes())
        self.assertEqual("2.0", self.care.inventory(self.pid)["mods"][0]["version"])

    def test_restore_deleted_world(self):
        import shutil
        world = self.world()
        point = self.care.snapshot(self.pid, scope="world", world="World")
        shutil.rmtree(world)
        self.care.restore(self.pid, point)
        self.assertEqual(b"world one", (world / "level.dat").read_bytes())

    def test_snapshot_rejects_symlink_without_following(self):
        external = Path(self.temp.name) / "secret"
        external.write_text("private")
        (self.folder / "config").mkdir()
        (self.folder / "config" / "secret.json").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.care.snapshot(self.pid)
        self.assertFalse(self.care.state(self.pid)["snapshots"])
        self.assertEqual("private", external.read_text())

    def test_checksum_damage_does_not_change_build(self):
        file = jar(self.folder / "mods" / "example.jar")
        point = self.care.snapshot(self.pid)
        archive, _ = self.care._snapshot(self.pid, point)
        with zipfile.ZipFile(archive) as original:
            payloads = {i.filename: original.read(i) for i in original.infolist()}
        payloads["payload/mods/example.jar"] = b"X" * len(payloads["payload/mods/example.jar"])
        with zipfile.ZipFile(archive, "w") as modified:
            for name, data in payloads.items():
                modified.writestr(name, data)
        original = file.read_bytes()
        with self.assertRaisesRegex(ValueError, "сумма"):
            self.care.restore(self.pid, point)
        self.assertEqual(original, file.read_bytes())

    def test_manifest_traversal_is_rejected(self):
        point = self.care.snapshot(self.pid)
        archive, _ = self.care._snapshot(self.pid, point)
        with zipfile.ZipFile(archive) as original:
            meta = json.loads(original.read("recovery.json"))
        meta["files"] = [{"path": "../outside", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}]
        with zipfile.ZipFile(archive, "w") as modified:
            modified.writestr("recovery.json", json.dumps(meta))
            modified.writestr("payload/../outside", b"x")
        with self.assertRaises(ValueError):
            self.care.restore(self.pid, point)
        self.assertFalse((self.store.root / "outside").exists())

    def test_batch_disable_requires_dependency_review_then_retains_files(self):
        jar(self.folder / "mods" / "library.jar", "library")
        jar(self.folder / "mods" / "client.jar", "client", depends={"library": ">=1.0"})
        result = self.care.mutate(self.pid, "mods", ["library.jar"], "disable")
        self.assertTrue(result["review"])
        self.assertIn("Client", result["dependents"])
        self.assertTrue((self.folder / "mods" / "library.jar").is_file())
        result = self.care.mutate(self.pid, "mods", ["library.jar"], "disable", True)
        self.assertTrue(result["ok"])
        self.assertTrue((self.folder / "mods" / "library.jar.disabled").is_file())
        row = next(r for r in self.care.inventory(self.pid)["mods"] if r["filename"] == "library.jar")
        self.assertFalse(row["enabled"])
        self.assertEqual("Library", row["title"])
        self.care.mutate(self.pid, "mods", ["library.jar.disabled"], "enable")
        self.assertTrue((self.folder / "mods" / "library.jar").is_file())

    def test_enabling_duplicate_mod_is_blocked(self):
        jar(self.folder / "mods" / "one.jar", "same")
        jar(self.folder / "mods" / "two.jar.disabled", "same", "2.0")
        with self.assertRaisesRegex(ValueError, "один мод"):
            self.care.mutate(self.pid, "mods", ["two.jar.disabled"], "enable", True)
        self.assertTrue((self.folder / "mods" / "two.jar.disabled").is_file())

    def test_transaction_rolls_back_on_failed_second_section(self):
        jar(self.folder / "mods" / "original.jar")
        (self.folder / "config").mkdir()
        (self.folder / "config" / "test.txt").write_text("old")
        stage = self.care.root / "prepared"
        jar(stage / "mods" / "new.jar", version="2.0")
        (stage / "config").mkdir()
        (stage / "config" / "test.txt").write_text("new")
        replace = os.replace
        failed = False

        def failing(source, dest):
            nonlocal failed
            if not failed and str(source).endswith("/new/config"):
                failed = True
                raise OSError("simulated full disk")
            return replace(source, dest)

        with patch("launcher.build_care.os.replace", side_effect=failing):
            with self.assertRaisesRegex(OSError, "full disk"):
                self.care._transaction(self.pid, stage, ["mods", "config"])
        self.assertTrue((self.folder / "mods" / "original.jar").is_file())
        self.assertFalse((self.folder / "mods" / "new.jar").exists())
        self.assertEqual("old", (self.folder / "config" / "test.txt").read_text())

    def test_restart_recovers_interrupted_replace_and_database(self):
        jar(self.folder / "mods" / "original.jar")
        work = self.care.root / ("transaction-" + "a" * 32)
        (work / "old").mkdir(parents=True)
        (work / "new").mkdir()
        old_profile = self.store.profile(self.pid)
        _atomic_json(work / "journal.json", {"phase": "prepared", "pid": self.pid, "oldProfile": old_profile, "targets": [{"name": "mods", "had": True}]})
        os.replace(self.folder / "mods", work / "old" / "mods")
        jar(self.folder / "mods" / "new.jar", version="2.0")
        with self.store.connection() as db:
            db.execute("UPDATE profiles SET version='1.21' WHERE id=?", (self.pid,))
        BuildCare(self.store)
        self.assertTrue((self.folder / "mods" / "original.jar").is_file())
        self.assertFalse((self.folder / "mods" / "new.jar").exists())
        self.assertEqual("1.20.2", self.store.profile(self.pid)["version"])
        self.assertFalse(work.exists())

    def test_existing_window_recovers_peer_transaction_and_clears_resolved_error(self):
        jar(self.folder / "mods" / "original.jar")
        work = self.care.root / ("transaction-" + "b" * 32)
        (work / "old").mkdir(parents=True)
        (work / "new").mkdir()
        _atomic_json(work / "journal.json", {"phase": "prepared", "pid": self.pid, "oldProfile": self.store.profile(self.pid), "targets": [{"name": "mods", "had": True}]})
        os.replace(self.folder / "mods", work / "old" / "mods")
        jar(self.folder / "mods" / "incomplete.jar", version="2.0")
        with patch.object(self.care, "_rollback_transaction", side_effect=OSError("sharing violation")):
            self.assertIn(self.pid, self.care.recover_pending())
        self.assertTrue(work.exists())
        with self.assertRaisesRegex(ValueError, "sharing violation"):
            self.care._writable_root(self.pid)
        self.assertEqual({}, self.care.recover_pending())
        self.assertNotIn(self.pid, self.care.recovery_errors)
        self.assertEqual(self.folder, self.care._writable_root(self.pid))
        self.assertTrue((self.folder / "mods" / "original.jar").is_file())
        self.assertFalse((self.folder / "mods" / "incomplete.jar").exists())
        self.assertFalse(work.exists())

    def test_live_process_keeps_reviewed_plan_until_it_exits(self):
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        work = self.care.root / ("plan-" + "c" * 32)
        work.mkdir()
        (work / "downloaded.jar").write_bytes(b"reviewed payload")
        try:
            _atomic_json(work / "owner.json", {"format": 1, "owner": process_identity(owner.pid)})
            other = BuildCare(self.store)
            self.assertEqual(b"reviewed payload", (work / "downloaded.jar").read_bytes())
            other.recover_pending()
            self.assertTrue(work.exists())
        finally:
            owner.terminate()
            owner.wait(timeout=10)
        BuildCare(self.store)
        self.assertFalse(work.exists())

    def test_malformed_journal_blocks_mutations_until_resolved(self):
        work = self.care.root / ("transaction-" + "d" * 32)
        work.mkdir()
        _atomic_json(work / "journal.json", ["invalid journal"])
        self.assertIn(self.pid, self.care.recover_pending())
        with self.assertRaisesRegex(ValueError, "журнал восстановления"):
            self.care._writable_root(self.pid)
        # The peer successfully repaired/finished the transaction.
        _atomic_json(work / "journal.json", {"phase": "committed", "pid": self.pid})
        self.assertEqual({}, self.care.recover_pending())
        self.assertFalse(work.exists())

    def test_clone_is_independent_and_copies_settings_and_pins(self):
        file = jar(self.folder / "mods" / "example.jar")
        self.world()
        self.care.pin(self.pid, "mods", [file.name], True)
        self.store.set("performancePreset:" + self.pid, "economy")
        calls = []
        self.care.on_clone = lambda src, dst: calls.append((src, dst))
        self.care.clone(self.pid, "Copy", False)
        clone = self.store.get("active")
        self.assertNotEqual(self.pid, clone)
        self.assertFalse((self.store.folder(clone) / "saves").exists())
        self.assertTrue(self.care.inventory(clone)["mods"][0]["pinned"])
        self.assertEqual("economy", self.store.get("performancePreset:" + clone))
        self.assertEqual([(self.pid, clone)], calls)
        jar(self.store.folder(clone) / "mods" / "example.jar", version="3.0")
        self.assertEqual("1.0", self.care.inventory(self.pid)["mods"][0]["version"])

    def test_retention_count_is_applied_per_world_and_build(self):
        self.store.set("careRetentionCount", "2")
        self.world()
        for n in range(4):
            self.care.snapshot(self.pid, label=str(n))
        world = self.care.snapshot(self.pid, scope="world", world="World")
        rows = self.care.state(self.pid)["snapshots"]
        self.assertEqual(3, len(rows))
        self.assertEqual({"2", "3"}, {r["label"] for r in rows if r["scope"] == "build"})
        self.assertIn(world, {r["id"] for r in rows})

    def test_require_idle_gate_applies_before_snapshot(self):
        with patch("launcher.build_care.require_idle_files", side_effect=ValueError("game running")):
            with self.assertRaisesRegex(ValueError, "game running"):
                self.care.snapshot(self.pid)

    def test_verified_room_bridge_is_managed(self):
        from launcher import room_bridge
        import shutil
        pid = self.store.create("Rooms", "1.20.1", "fabric")
        shutil.copy2(room_bridge._BUNDLED, self.store.folder(pid) / "mods" / room_bridge.FILENAME)
        rows = self.care.inventory(pid)["mods"]
        self.assertTrue(next(r for r in rows if r["name"] == room_bridge.FILENAME)["managed"])
        with self.assertRaisesRegex(ValueError, "Встроенные"):
            self.care.mutate(pid, "mods", [room_bridge.FILENAME], "disable", True)

    def test_world_parent_symlink_rejected(self):
        external = Path(self.temp.name) / "external"
        (external / "World").mkdir(parents=True)
        (external / "World" / "level.dat").write_bytes(b"external")
        (self.folder / "saves").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.care.snapshot(self.pid, scope="world", world="World")

    def test_absent_performance_setting_remains_absent_after_restore_and_clone(self):
        point = self.care.snapshot(self.pid)
        self.store.set("performanceMemory:" + self.pid, "4096")
        self.care.restore(self.pid, point)
        self.assertIsNone(self.store.get("performanceMemory:" + self.pid, None))
        self.care.clone(self.pid, "Copy")
        self.assertIsNone(self.store.get("performanceMemory:" + self.store.get("active"), None))

    def test_auto_world_backup_is_opt_in_and_skips_unchanged_world(self):
        world = self.world()
        self.assertEqual("", self.care.backup_after_game(self.pid))
        self.assertEqual([], self.care.state(self.pid)["snapshots"])
        self.care.handle("careAutoBackup", {"id": self.pid, "enabled": True})
        self.care.backup_after_game(self.pid)
        self.care.backup_after_game(self.pid)
        self.assertEqual(1, len(self.care.state(self.pid)["snapshots"]))
        (world / "level.dat").write_bytes(b"updated world")
        self.care.backup_after_game(self.pid)
        self.assertEqual(2, len(self.care.state(self.pid)["snapshots"]))

    def test_purge_recovery_requires_permanent_delete_and_cleans_data(self):
        self.care.snapshot(self.pid)
        self.store.set("performancePreset:" + self.pid, "economy")
        with self.assertRaisesRegex(ValueError, "безвозвратное"):
            self.care.purge(self.pid)
        self.store.move_profile(self.pid)
        self.assertTrue((self.care.root / self.pid).exists())
        self.store.purge_profile(self.pid)
        self.care.purge(self.pid)
        self.assertFalse((self.care.root / self.pid).exists())
        self.assertIsNone(self.store.get("performancePreset:" + self.pid, None))
        with self.store.connection() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM care_snapshots WHERE pid=?", (self.pid,)).fetchone()[0])

    def test_project_title_and_icon_survive_disable_clone_and_restore(self):
        file = jar(self.folder / "mods" / "example.jar")
        release = SimpleNamespace(kind="mods", filename=file.name, name="Example 1", number="1.0", project="project", version="v1")
        self.care.remember_project(self.pid, release, {"title": "Project title", "description": "Catalog description"})
        data = io.BytesIO()
        Image.new("RGBA", (96, 96), (120, 220, 80, 255)).save(data, "PNG")
        from launcher.modfiles import file_stamp
        self.store.icons.remember_project(file, file_stamp(file), data.getvalue(), "project", "v1")
        icon = self.care.inventory(self.pid)["mods"][0]["icon"]
        self.assertTrue(icon)
        point = self.care.snapshot(self.pid)
        self.care.mutate(self.pid, "mods", ["example.jar"], "disable")
        disabled = self.care.inventory(self.pid)["mods"][0]
        self.assertEqual("Project title", disabled["title"])
        self.assertEqual(icon, disabled["icon"])
        self.care.clone(self.pid, "Copy")
        clone_row = self.care.inventory(self.store.get("active"))["mods"][0]
        self.assertEqual("Project title", clone_row["title"])
        self.assertEqual(icon, clone_row["icon"])
        self.care.restore(self.pid, point)
        restored = self.care.inventory(self.pid)["mods"][0]
        self.assertEqual("example.jar", restored["name"])
        self.assertEqual("Project title", restored["title"])
        self.assertEqual(icon, restored["icon"])

    def updates_fixture(self):
        original = jar(self.folder / "mods" / "example.jar")
        newer = jar(Path(self.temp.name) / "new.jar", version="2.0")
        old_hash = hashlib.sha512(original.read_bytes()).hexdigest()
        new_hash = hashlib.sha512(newer.read_bytes()).hexdigest()
        old = {"id": "oldversion", "project_id": "project", "version_number": "1.0", "date_published": "2024-01-01", "files": [{"hashes": {"sha512": old_hash}}]}
        release = modrinth.Package("newversion", "project", "mods", "2.0", "Example 2", "release", "2025-01-01", "example-2.jar", newer.stat().st_size, new_hash, "https://cdn.modrinth.com/data/project/versions/newversion/example-2.jar", ())
        return newer, old_hash, old, release

    def test_updates_plan_keeps_old_files_until_review_then_atomic_apply(self):
        newer, old_hash, old, release = self.updates_fixture()
        def download(_release, target):
            target.write_bytes(newer.read_bytes())
        with patch.object(modrinth, "_versions_by_hash", return_value={old_hash: old}), patch.object(modrinth, "_releases", return_value=[release]), patch.object(modrinth, "_download", side_effect=download), patch.object(modrinth, "_catalog_report", return_value=compatibility.report()), patch.object(modrinth, "_get", return_value={"title": "Example", "changelog": "Better"}):
            self.care.check_updates(self.pid, "mods")
        plan = self.care.state(self.pid)["updatePlan"]
        self.assertEqual("2.0", plan["entries"][0]["to"])
        self.assertFalse(plan["blocked"])
        self.assertTrue((self.folder / "mods" / "example.jar").is_file())
        self.assertFalse((self.folder / "mods" / "example-2.jar").exists())
        # Opening a second window must not discard the first window's review.
        BuildCare(self.store)
        self.assertTrue((self.care.root / ("plan-" + plan["token"]) / "owner.json").is_file())
        self.care.apply_updates(self.pid, plan["token"])
        self.assertFalse((self.folder / "mods" / "example.jar").exists())
        self.assertTrue((self.folder / "mods" / "example-2.jar").is_file())
        self.assertEqual(1, len(self.care.state(self.pid)["snapshots"]))

    def test_updates_reject_stale_plan_after_manual_change(self):
        newer, old_hash, old, release = self.updates_fixture()
        with patch.object(modrinth, "_versions_by_hash", return_value={old_hash: old}), patch.object(modrinth, "_releases", return_value=[release]), patch.object(modrinth, "_download", side_effect=lambda r, p: p.write_bytes(newer.read_bytes())), patch.object(modrinth, "_catalog_report", return_value=compatibility.report()), patch.object(modrinth, "_get", return_value={"title": "Example"}):
            self.care.check_updates(self.pid, "mods")
        token = self.care.state(self.pid)["updatePlan"]["token"]
        jar(self.folder / "mods" / "new-local.jar", "local")
        with self.assertRaisesRegex(ValueError, "изменились"):
            self.care.apply_updates(self.pid, token)
        self.assertTrue((self.folder / "mods" / "example.jar").is_file())

    def test_pinned_release_is_not_updated(self):
        newer, old_hash, old, release = self.updates_fixture()
        self.care.pin(self.pid, "mods", ["example.jar"], True)
        with patch.object(modrinth, "_versions_by_hash", return_value={old_hash: old}), patch.object(modrinth, "_releases") as releases:
            self.care.check_updates(self.pid, "mods")
        self.assertFalse(self.care.state(self.pid)["updatePlan"]["entries"])
        releases.assert_not_called()


if __name__ == "__main__":
    unittest.main()

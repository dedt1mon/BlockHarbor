import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import lifecycle
from launcher.build_care import BuildCare
from launcher.store import Store


class StorageLifecycleTests(unittest.TestCase):
    def test_cancel_after_prepare_does_not_publish_profile_or_change_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            existing = store.create("Existing", "1.20.1", "vanilla")
            scope = lifecycle.CancellationScope()

            def prepare(folder):
                (folder / "downloaded.txt").write_text("complete")
                scope.cancel()

            with lifecycle.bind(scope), self.assertRaises(lifecycle.OperationCancelled):
                store.create("Cancelled", "1.20.1", "vanilla", prepare=prepare)
            self.assertEqual([p["id"] for p in store.all()], [existing])
            self.assertEqual(store.get("active"), existing)
            self.assertEqual([p.name for p in store.instances.iterdir()], [existing])

    def test_close_during_backup_removes_partial_and_keeps_original_world(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            pid = store.create("Existing", "1.20.1", "vanilla")
            world = store.folder(pid) / "saves" / "World"
            world.mkdir(parents=True)
            original = b"world-data" * 300000
            (world / "level.dat").write_bytes(original)
            care = BuildCare(store)
            scope = lifecycle.CancellationScope()
            write = zipfile._ZipWriteFile.write

            def cancel_during_write(handle, content):
                result = write(handle, content)
                scope.cancel()
                return result

            with lifecycle.bind(scope), patch.object(zipfile._ZipWriteFile, "write", cancel_during_write):
                with self.assertRaises(lifecycle.OperationCancelled):
                    care.snapshot(pid, scope="world", world="World")
            self.assertEqual((world / "level.dat").read_bytes(), original)
            self.assertEqual(list(care.root.rglob("*.part")), [])
            with store.connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM care_snapshots").fetchone()[0], 0)

    def test_close_before_restore_commit_keeps_installed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            pid = store.create("Existing", "1.20.1", "vanilla")
            original = store.folder(pid) / "mods" / "original.jar"
            original.write_bytes(b"original")
            prepared = Path(directory) / "prepared"
            (prepared / "mods").mkdir(parents=True)
            (prepared / "mods" / "new.jar").write_bytes(b"new")
            care = BuildCare(store)
            scope = lifecycle.CancellationScope()
            with lifecycle.bind(scope):
                scope.cancel()
                with self.assertRaises(lifecycle.OperationCancelled):
                    care._transaction(pid, prepared, ["mods"])
            self.assertEqual(original.read_bytes(), b"original")
            self.assertFalse((original.parent / "new.jar").exists())
            self.assertTrue((prepared / "mods" / "new.jar").exists())


if __name__ == "__main__":
    unittest.main()

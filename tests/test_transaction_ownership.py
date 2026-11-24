"""Recovery never erases files edited or replaced after a real process crash."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from launcher import build_care, lifecycle
from launcher.build_care import BuildCare, _atomic_json
from launcher.store import Store


class TransactionOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.pid = self.store.create("Recovery", "1.20.1", "fabric")
        self.mods = self.store.content_folder(self.pid, "mods")
        self.care = BuildCare(self.store)

    def pending(self):
        source = self.root / "incoming.jar"
        source.write_bytes(b"catalog")
        return [(SimpleNamespace(kind="mods", filename="one.jar"), source, self.mods / "one.jar")]

    def crash(self, mode):
        script = '''
import os,sys
from pathlib import Path
from types import SimpleNamespace
from launcher import build_care
from launcher.store import Store
from launcher.build_care import BuildCare
store=Store(Path(sys.argv[1]));pid=sys.argv[2];mode=sys.argv[3];care=BuildCare(store)
mods=store.content_folder(pid,'mods')
if mode=='new':
 pending=[]
 for name in ('one.jar','two.jar'):
  source=store.root/name;source.write_bytes(b'aaaa')
  pending.append((SimpleNamespace(kind='mods',filename=name),source,mods/name))
 original=build_care.publish_new;count=[0]
 def publish(source,target):
  count[0]+=1
  if count[0]==2:os._exit(87)
  original(source,target)
 build_care.publish_new=publish
 care.publish_addons(pid,pending)
else:
 stage=store.root/'prepared';(stage/'mods'/'nested').mkdir(parents=True)
 (stage/'mods'/'nested'/'changed.jar').write_bytes(b'aaaa')
 original=build_care.os.replace
 def replace(source,target):
  result=original(source,target)
  if Path(target)==mods and Path(source).parent.name=='new':os._exit(87)
  return result
 build_care.os.replace=replace
 care._transaction(pid,stage,['mods'])
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.root), self.pid, mode],
                                cwd=Path(__file__).parents[1], timeout=15)
        self.assertEqual(result.returncode, 87)

    def test_file_created_between_no_replace_preflight_and_journal_is_preserved(self):
        pending = self.pending()
        original = build_care.secrets.token_hex
        def token(length):
            if length == 16:
                (self.mods / "one.jar").write_bytes(b"manual")
            return original(length)
        with patch.object(build_care.secrets, "token_hex", side_effect=token), self.assertRaises(OSError):
            self.care.publish_addons(self.pid, pending)
        self.assertEqual((self.mods / "one.jar").read_bytes(), b"manual")
        self.assertFalse(list(self.care.root.glob("transaction-*")))

    def test_committed_cleanup_failure_does_not_fail_or_block_the_install(self):
        pending = self.pending()
        original = build_care.shutil.rmtree
        def remove(path, *args, **kwargs):
            work = Path(path).parent
            journal = work / "journal.json"
            if (work.name.startswith("transaction-") and journal.is_file()
                    and json.loads(journal.read_text())["phase"] == "committed"):
                raise PermissionError("temporary antivirus lock")
            return original(path, *args, **kwargs)
        with patch.object(build_care.shutil, "rmtree", side_effect=remove):
            self.assertEqual(self.care.publish_addons(self.pid, pending), 1)
            self.assertEqual(self.care.recover_pending(), {})
        self.assertEqual((self.mods / "one.jar").read_bytes(), b"catalog")
        self.assertEqual(self.care.recover_pending(), {})
        self.assertFalse(list(self.care.root.glob("transaction-*")))

    def test_partial_committed_cleanup_keeps_marker_and_retries_remaining_payload(self):
        (self.mods / "old-one.jar").write_bytes(b"old one")
        (self.mods / "old-two.jar").write_bytes(b"old two")
        stage = self.root / "prepared"
        (stage / "mods").mkdir(parents=True)
        (stage / "mods/new.jar").write_bytes(b"new verified content")
        original = build_care.shutil.rmtree
        failures = []
        def remove(path, *args, **kwargs):
            path = Path(path)
            if path.name == "old" and path.parent.name.startswith("transaction-"):
                # Reproduce a real partial rmtree: one entry is already gone
                # before a locked second entry raises. The commit marker must
                # not be among the deleted entries.
                (path / "mods/old-one.jar").unlink(missing_ok=True)
                failures.append(path)
                raise PermissionError("second old file is locked")
            return original(path, *args, **kwargs)
        with patch.object(build_care.shutil, "rmtree", side_effect=remove):
            self.care._transaction(self.pid, stage, ["mods"])
            work, = self.care.root.glob("transaction-*")
            self.assertTrue(failures)
            self.assertEqual(json.loads((work / "journal.json").read_text())["phase"], "committed")
            self.assertFalse((work / "old/mods/old-one.jar").exists())
            self.assertEqual((work / "old/mods/old-two.jar").read_bytes(), b"old two")
            self.assertEqual((self.mods / "new.jar").read_bytes(), b"new verified content")
            self.assertEqual(self.care.recover_pending(), {})
            self.assertTrue((work / "journal.json").exists())
            self.assertEqual(len(failures), 2)
        self.assertEqual(self.care.recover_pending(), {})
        self.assertFalse(work.exists())
        self.assertEqual([p.name for p in self.mods.iterdir()], ["new.jar"])

    def test_final_empty_directory_cleanup_retries_without_removing_unmarked_payloads(self):
        pending = self.pending()
        original = Path.rmdir
        def remove(path):
            if path.parent == self.care.root and path.name.startswith("transaction-"):
                raise PermissionError("final directory briefly locked")
            return original(path)
        with patch.object(Path, "rmdir", autospec=True, side_effect=remove):
            self.assertEqual(self.care.publish_addons(self.pid, pending), 1)
        work, = self.care.root.glob("transaction-*")
        self.assertEqual(list(work.iterdir()), [])
        unmarked = self.care.root / ("transaction-" + "b" * 32)
        unmarked.mkdir()
        (unmarked / "unknown.txt").write_bytes(b"keep")
        unknown_name = self.care.root / "transaction-user-folder"
        unknown_name.mkdir()
        self.assertEqual(self.care.recover_pending(), {})
        self.assertFalse(work.exists())
        self.assertEqual((unmarked / "unknown.txt").read_bytes(), b"keep")
        self.assertTrue(unknown_name.exists())
        self.assertEqual((self.mods / "one.jar").read_bytes(), b"catalog")

    def test_same_size_in_place_edit_after_crash_survives(self):
        self.crash("new")
        file = self.mods / "one.jar"
        file.write_bytes(b"bbbb")
        recovered = BuildCare(self.store)
        self.assertEqual(file.read_bytes(), b"bbbb")
        self.assertEqual(recovered.recovery_errors, {})
        self.assertFalse(list(recovered.root.glob("transaction-*")))

    def test_replaced_inode_with_identical_bytes_after_crash_survives(self):
        self.crash("new")
        file = self.mods / "one.jar"
        original_inode = file.stat().st_ino
        replacement = self.root / "manual.jar"
        replacement.write_bytes(b"aaaa")
        os.replace(replacement, file)
        self.assertNotEqual(file.stat().st_ino, original_inode)
        recovered = BuildCare(self.store)
        self.assertEqual(file.read_bytes(), b"aaaa")
        self.assertEqual(recovered.recovery_errors, {})

    def test_nested_edit_retains_new_tree_old_backup_and_journal(self):
        (self.mods / "original.jar").write_bytes(b"original")
        self.crash("directory")
        file = self.mods / "nested" / "changed.jar"
        # Neither directory inode nor its own mtime reflects this nested edit.
        folder_stamp = self.mods.stat().st_mtime_ns
        file.write_bytes(b"bbbb")
        self.assertEqual(self.mods.stat().st_mtime_ns, folder_stamp)
        recovered = BuildCare(self.store)
        self.assertIn(self.pid, recovered.recovery_errors)
        self.assertEqual(file.read_bytes(), b"bbbb")
        journals = list(recovered.root.glob("transaction-*"))
        self.assertEqual(len(journals), 1)
        self.assertEqual((journals[0] / "old/mods/original.jar").read_bytes(), b"original")
        self.assertTrue((journals[0] / "journal.json").exists())

    def test_unchanged_published_directory_rolls_back_automatically(self):
        (self.mods / "original.jar").write_bytes(b"original")
        self.crash("directory")
        recovered = BuildCare(self.store)
        self.assertEqual(recovered.recovery_errors, {})
        self.assertEqual([p.name for p in self.mods.iterdir()], ["original.jar"])
        self.assertEqual((self.mods / "original.jar").read_bytes(), b"original")

    def test_legacy_journal_preserves_unknown_tree_separately_before_restore(self):
        (self.mods / "original.jar").write_bytes(b"original")
        work = self.care.root / ("transaction-" + "a" * 32)
        (work / "old").mkdir(parents=True)
        os.replace(self.mods, work / "old/mods")
        self.mods.mkdir()
        (self.mods / "manual.jar").write_bytes(b"manual")
        _atomic_json(work / "journal.json", {"phase": "prepared", "pid": self.pid,
                     "oldProfile": self.store.profile(self.pid), "targets": [{"name": "mods", "had": True}]})
        recovered = BuildCare(self.store)
        self.assertEqual(recovered.recovery_errors, {})
        self.assertEqual((self.mods / "original.jar").read_bytes(), b"original")
        saved = list(recovered.root.glob("preserved-*/files/mods/manual.jar"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"manual")
        BuildCare(self.store)
        self.assertTrue(saved[0].exists())
        with self.store.connection() as db:
            self.assertTrue(db.execute("SELECT 1 FROM care_history WHERE action='Сохранены файлы после прерывания'").fetchone())

    def test_cancelled_scope_does_not_interrupt_owned_tree_rollback(self):
        (self.mods / "original.jar").write_bytes(b"original")
        stage = self.root / "prepared"
        (stage / "mods").mkdir(parents=True)
        (stage / "mods/new.jar").write_bytes(b"new")
        (stage / "config").mkdir()
        (stage / "config/new.json").write_text("{}")
        scope = lifecycle.CancellationScope()
        original = build_care.os.replace
        def replace(source, target):
            if Path(source).name == "config" and Path(source).parent.name == "new":
                scope.cancel()
                raise OSError("failed second publication")
            return original(source, target)
        with lifecycle.bind(scope), patch.object(build_care.os, "replace", side_effect=replace), self.assertRaises(OSError):
            self.care._transaction(self.pid, stage, ["mods", "config"])
        self.assertEqual((self.mods / "original.jar").read_bytes(), b"original")
        self.assertFalse((self.mods / "new.jar").exists())
        self.assertFalse(list(self.care.root.glob("transaction-*")))


if __name__ == "__main__":
    unittest.main()

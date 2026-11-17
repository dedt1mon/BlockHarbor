import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from main import App


class BuildRemovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = App(self.root)
        self.store = self.app.store
        self.first = self.store.create("Остров друзей", "1.20.1", "fabric")
        self.second = self.store.create("Вторая сборка", "1.20.1", "forge")
        self.store.set("active", self.first)
        self.relative = Path("saves/Остров/level.dat")
        world = self.store.folder(self.first) / self.relative
        world.parent.mkdir(parents=True)
        world.write_bytes(b"complete-world")

    def move(self, pid, restore=False):
        return self.app.action(
            "restoreBuild" if restore else "deleteBuild", {"id": pid}
        )

    def test_deleting_active_preserves_game_files_and_selects_another_profile(self):
        self.move(self.first)
        self.move(self.first)
        s = self.app.state()
        self.assertEqual(s["active"], self.second)
        self.assertEqual([p["id"] for p in s["profiles"]], [self.second])
        self.assertEqual([p["id"] for p in s["deletedProfiles"]], [self.first])
        self.assertFalse((self.store.instances / self.first).exists())
        archived = self.store.deleted_instances / self.first / self.relative
        self.assertEqual(archived.read_bytes(), b"complete-world")
        self.move(self.first, True)
        self.move(self.first, True)
        self.assertEqual(self.app.state()["active"], self.second)
        self.assertEqual(self.app.state()["deletedProfiles"], [])
        self.assertEqual(
            (self.store.folder(self.first) / self.relative).read_bytes(),
            b"complete-world",
        )

    def test_removing_inactive_and_last_persists_across_restarts(self):
        self.move(self.second)
        self.assertEqual(self.app.state()["active"], self.first)
        self.move(self.first)
        self.app = App(self.root)
        s = self.app.state()
        self.assertEqual(s["active"], "")
        self.assertEqual(s["profiles"], [])
        self.assertEqual(
            s["contents"], {"mods": [], "shaderpacks": [], "resourcepacks": []}
        )
        self.assertFalse(s["steam"]["supported"])
        self.move(self.first, True)
        self.assertEqual(self.app.state()["active"], self.first)
        self.assertEqual(self.app.state()["profiles"][0]["name"], "Остров друзей")

    def test_create_after_deleting_last_profile(self):
        for pid in (self.first, self.second):
            self.move(pid)
        self.app.action(
            "create", {"name": "Чистая", "version": "1.20.1", "loader": "vanilla"}
        )
        s = self.app.state()
        self.assertEqual(len(s["profiles"]), 1)
        self.assertEqual(s["profiles"][0]["id"], s["active"])

    def test_failed_move_and_external_link_leave_game_files_intact(self):
        with patch.object(Path, "rename", side_effect=PermissionError("in use")):
            with self.assertRaisesRegex(ValueError, "переместить"):
                self.move(self.first)
        self.assertEqual(self.app.state()["active"], self.first)
        self.assertEqual(self.store.deleted(), [])
        target = self.store.deleted_instances / self.first
        target.mkdir(parents=True)
        protected = target / "do-not-overwrite.txt"
        protected.write_text("previous contents")
        with self.assertRaisesRegex(ValueError, "назначения"):
            self.move(self.first)
        self.assertTrue(protected.is_file())
        self.assertTrue((self.store.folder(self.first) / self.relative).is_file())

    def test_restart_recovers_interrupted_filesystem_move_in_either_direction(self):
        archived = self.store.deleted_instances / self.first
        archived.parent.mkdir()
        (self.store.instances / self.first).rename(archived)
        self.app = App(self.root)
        self.assertEqual(
            (self.app.store.folder(self.first) / self.relative).read_bytes(),
            b"complete-world",
        )
        self.move(self.first)
        archived.rename(self.store.instances / self.first)
        self.app = App(self.root)
        self.assertTrue((archived / self.relative).is_file())
        self.assertEqual(self.app.state()["deletedProfiles"][0]["id"], self.first)

    def test_bg_work_and_current_game_protected_but_unrelated_build_removable(self):
        self.app.job["busy"] = True
        with self.assertRaisesRegex(ValueError, "операции"):
            self.move(self.second)
        self.app.job["busy"] = False
        child = Mock()
        child.poll.return_value = None
        self.app.child = child
        self.app.playing_id = self.first
        with self.assertRaisesRegex(ValueError, "Minecraft"):
            self.move(self.first)
        self.move(self.second)
        child.poll.return_value = 0
        self.app.prism_launches[self.first] = Mock()
        self.app.prism_launches[self.first].poll.return_value = None
        with self.assertRaisesRegex(ValueError, "Prism"):
            self.move(self.first)

    def test_running_world_owned_by_an_external_process(self):
        session = self.store.folder(self.first) / self.relative.parent / "session.lock"
        session.write_bytes(b"lock")
        code = """
import os, sys
with open(sys.argv[1], "r+b") as file:
    if os.name != "nt":
        import fcntl
        fcntl.lockf(file, fcntl.LOCK_EX)
    print("locked", flush=True)
    sys.stdin.readline()
"""
        with subprocess.Popen(
            [sys.executable, "-c", code, str(session)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(ValueError, "заняты"):
                    self.move(self.first)
            finally:
                child.communicate("\n", timeout=5)
        self.move(self.first)
        self.assertFalse((self.store.instances / self.first).exists())

    def test_cannot_archive_an_instance_symlink(self):
        original = self.store.instances / self.first
        elsewhere = self.store.instances / "another-directory"
        original.rename(elsewhere)
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(elsewhere), str(original))
        else:
            original.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "перенаправлена"):
            self.move(self.first)
        self.assertEqual((elsewhere / self.relative).read_bytes(), b"complete-world")

    def purge_job(self, pid):
        result = self.app.action("purgeBuild", {"id": pid})
        for _ in range(300):
            state = self.app.job_snapshot()
            if not state["busy"]:
                self.assertEqual(state["id"], result["job"])
                return state
            time.sleep(0.01)
        self.fail("Permanent removal did not finish")

    def test_purge_only_archived_instance_and_idempotent_retry(self):
        world = self.store.folder(self.first) / self.relative
        self.assertFalse(self.purge_job(self.first)["ok"])
        self.assertFalse(self.purge_job("f" * 32)["ok"])
        self.assertTrue(world.is_file())
        self.move(self.first)
        self.assertFalse(self.app.state()["deletedProfiles"][0]["purging"])
        self.assertTrue(self.purge_job(self.first)["ok"])
        self.assertFalse((self.store.deleted_instances / self.first).exists())
        self.assertEqual(self.app.state()["deletedProfiles"], [])
        self.assertEqual(self.app.state()["active"], self.second)
        self.assertTrue(self.purge_job(self.first)["ok"])

    def test_unfinished_purge_never_restores_an_incomplete_world(self):
        self.move(self.first)
        archived = self.store.deleted_instances / self.first
        remaining = archived / "mods" / "untouched.jar"
        remaining.write_bytes(b"still here")

        def partial_remove(folder, **kwargs):
            (folder / self.relative).unlink()
            raise PermissionError("sharing violation")

        with patch("launcher.store.shutil.rmtree", side_effect=partial_remove):
            failed = self.purge_job(self.first)
        self.assertFalse(failed["ok"])
        self.assertIn("повтори", failed["message"])
        self.assertTrue(remaining.is_file())
        self.app = App(self.root)
        self.assertTrue(self.app.state()["deletedProfiles"][0]["purging"])
        with self.assertRaisesRegex(ValueError, "Безвозвратное"):
            self.move(self.first, True)
        self.assertTrue(self.purge_job(self.first)["ok"])
        self.assertFalse(archived.exists())
        self.assertTrue(self.store.folder(self.second).is_dir())
        self.assertEqual(App(self.root).state()["deletedProfiles"], [])

    def test_purge_rejects_redirected_root_and_does_not_follow_nested_links(self):
        self.move(self.first)
        archived = self.store.deleted_instances / self.first
        real = archived.with_name("backup")
        elsewhere = self.root / "external"
        elsewhere.mkdir()
        marker = elsewhere / "keep-world"
        marker.write_bytes(b"must survive")

        def link(source, dest):
            if os.name == "nt":
                import _winapi

                _winapi.CreateJunction(str(source), str(dest))
            else:
                dest.symlink_to(source, target_is_directory=True)

        archived.rename(real)
        link(elsewhere, archived)
        self.assertFalse(self.purge_job(self.first)["ok"])
        if os.name == "nt":
            archived.rmdir()
        else:
            archived.unlink()
        real.rename(archived)
        link(elsewhere, archived / "external-world")
        self.assertTrue(self.purge_job(self.first)["ok"])
        self.assertEqual(marker.read_bytes(), b"must survive")
        self.assertFalse(archived.exists())

    def test_purge_is_a_background_job_while_file_removal_runs(self):
        self.move(self.first)
        running, finish = threading.Event(), threading.Event()
        actual_rmtree = shutil.rmtree

        def slow_remove(*args, **kwargs):
            running.set()
            self.assertTrue(finish.wait(3))
            return actual_rmtree(*args, **kwargs)

        with patch("launcher.store.shutil.rmtree", side_effect=slow_remove):
            self.app.action("purgeBuild", {"id": self.first})
            try:
                self.assertTrue(running.wait(2))
                state = self.app.state()
                self.assertTrue(state["job"]["busy"])
                self.assertTrue(state["deletedProfiles"][0]["purging"])
                self.assertTrue(self.store.folder(self.second).is_dir())
            finally:
                finish.set()
            for _ in range(250):
                if not self.app.job_snapshot()["busy"]:
                    break
                time.sleep(0.01)
        self.assertTrue(self.app.job_snapshot()["ok"])
        self.assertEqual(self.app.state()["deletedProfiles"], [])

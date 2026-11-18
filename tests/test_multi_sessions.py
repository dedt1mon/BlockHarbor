import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import game
from launcher.sessions import FileMutex, SessionCoordinator, WindowStore, atomic_json, process_alive, process_identity
from launcher.singleton import SingleInstance
from main import App


PROJECT = Path(__file__).resolve().parents[1]


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def coordinator(self, **kwargs):
        value = SessionCoordinator(self.root, **kwargs)
        self.addCleanup(value.close)
        return value

    def child(self, script, *args):
        child = subprocess.Popen([sys.executable, "-c", script, *map(str, args)], cwd=PROJECT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def clean():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(clean)
        return child

    def test_two_slots_share_legacy_exclusion_and_independent_defaults(self):
        first, second = self.coordinator(), self.coordinator()
        self.assertEqual((first.slot, second.slot), (1, 2))
        self.assertEqual(first.snapshot()["launcherCount"], 2)
        # The old 2.0 exclusive lock cannot run over shared 2.1 data.
        legacy = SingleInstance(self.root / "activation-v10", timeout=0.05)
        with self.assertRaisesRegex(RuntimeError, "не ответил"):
            with legacy:
                pass
        first.close()
        self.assertEqual(second.snapshot()["launcherCount"], 1)
        self.assertEqual(self.coordinator().slot, 1)

    def test_old_launcher_blocks_new_windows_until_closed(self):
        with SingleInstance(self.root / "activation-v10"):
            with self.assertRaisesRegex(ValueError, "предыдущую версию"):
                self.coordinator()
        self.assertEqual(self.coordinator().slot, 1)

    def test_malformed_stale_profiles_do_not_break_startup(self):
        directory = self.root / "launcher-sessions"
        directory.mkdir()
        (directory / "1.json").write_text('{"profiles":null}', encoding="utf-8")
        self.assertEqual(self.coordinator().slot, 1)

    def test_actual_other_process_holds_operation_lock_and_crash_releases_it(self):
        parent = self.coordinator()
        child = self.child("import sys,time; from launcher.sessions import SessionCoordinator; s=SessionCoordinator(sys.argv[1]); "
                           "guard=s.operation(); guard.__enter__(); print('locked',flush=True); time.sleep(60)", self.root)
        self.assertEqual(child.stdout.readline().strip(), "locked")
        with self.assertRaisesRegex(ValueError, "другом окне"):
            with parent.operation():
                self.fail("Must not mutate across the owner's lock")
        child.kill(); child.wait(timeout=5)
        with parent.operation():
            self.assertEqual(parent.snapshot()["launcherCount"], 1)

    def test_surviving_game_keeps_build_reserved_after_owner_exit(self):
        # The JVM substitute belongs to the test parent; only its launcher exits.
        game_process = self.child("import time;time.sleep(60)")
        identity = process_identity(game_process.pid)
        worker = self.child("import json,sys; from launcher.sessions import SessionCoordinator; "
                            "s=SessionCoordinator(sys.argv[1]); s.claim('a'*32); "
                            "s.claims['a'*32]['process']=json.loads(sys.argv[2]);s._save();print('claimed',flush=True)",
                            self.root, json.dumps(identity))
        self.assertEqual(worker.stdout.readline().strip(), "claimed")
        worker.wait(timeout=5)
        current = self.coordinator()
        self.assertIn("a" * 32, current.snapshot()["runningProfiles"])
        with current.operation():
            with self.assertRaisesRegex(ValueError, "уже запущена"):
                current.claim("a" * 32)
        game_process.terminate(); game_process.wait(timeout=5)
        self.assertNotIn("a" * 32, current.snapshot()["runningProfiles"])
        with current.operation():
            current.claim("a" * 32)
        current.release("a" * 32)

    def test_recycled_pid_is_not_a_surviving_game(self):
        identity = process_identity(os.getpid())
        self.assertTrue(process_alive(identity))
        self.assertFalse(process_alive({**identity, "birth": "different-creation-time"}))

    def test_update_excludes_other_windows_and_blocks_new_claims(self):
        first, second = self.coordinator(), self.coordinator()
        with self.assertRaisesRegex(ValueError, "остальные окна"):
            with first.update_guard():
                pass
        second.close()
        with first.update_guard():
            with self.assertRaisesRegex(ValueError, "Устанавливается обновление"):
                self.coordinator(startup_timeout=0.05)
        third = self.coordinator()
        self.assertEqual(third.snapshot()["launcherCount"], 2)

    def test_finish_autolaunch_waits_for_handoff_without_owning_legacy_lock(self):
        directory = self.root / "launcher-sessions"
        directory.mkdir()
        path = directory / "update.json"
        atomic_json(path, {"session": "a" * 24, "helper": process_identity(os.getpid()), "targetVersion": "2.1.0"})
        acquired = []
        def finish():
            lock = FileMutex(self.root / "activation-v10" / ".session" / "owner.lock")
            acquired.append(lock.acquire())
            lock.close()
            path.unlink()
        timer = threading.Timer(0.2, finish)
        timer.start()
        try:
            current = self.coordinator(startup_timeout=2)
        finally:
            timer.join()
        self.assertEqual(acquired, [True])
        self.assertEqual(current.snapshot()["launcherCount"], 1)


class MultiGameTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.first = App(self.root)
        self.addCleanup(self.first.close)
        self.one = self.first.store.create("First", "1.21.1", "vanilla")
        self.two = self.first.store.create("Second", "1.21.1", "vanilla")
        self.second = App(self.root)
        self.addCleanup(self.second.close)
        for app in (self.first, self.second):
            app.window = Mock()

    def wait_job(self, app):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = app.job_snapshot()
            if not state["busy"]:
                self.assertTrue(state["ok"], state)
                return state
            time.sleep(0.005)
        self.fail("Game preparation did not finish")

    def test_existing_windows_keep_their_build_nickname_mode_and_skin(self):
        self.first.store.set_many({"active": self.one, "nickname": "FirstPlayer", "mode": "local", "skinDigest": "a" * 64, "model": "classic"})
        self.second.store.set_many({"active": self.two, "nickname": "SecondPlayer", "mode": "prism", "skinDigest": "b" * 64, "model": "slim"})
        self.assertEqual(self.first.store.get("active"), self.one)
        self.assertEqual(self.first.player_name(), "FirstPlayer")
        self.assertEqual(self.first.store.get("mode"), "local")
        self.assertEqual(self.first.store.get("skinDigest"), "a" * 64)
        self.assertEqual(self.first.store.settings()["model"], "classic")
        next_defaults = WindowStore(self.root)
        self.assertEqual(next_defaults.get("nickname"), "SecondPlayer")
        self.assertEqual(next_defaults.get("active"), self.two)
        third = self.first.store.create("Third", "1.21.1", "vanilla")
        self.assertEqual(self.first.store.get("active"), third)
        self.assertEqual(self.second.store.get("active"), self.two)

    def test_simultaneous_games_and_exit_restore_only_their_own_window(self):
        left, right = Mock(), Mock()
        left.poll.return_value = right.poll.return_value = None
        with patch.object(game, "install"), patch.object(game, "launch_local", side_effect=[left, right]):
            self.first.action("play", {"id": self.one, "nickname": "LeftPlayer"})
            self.wait_job(self.first)
            self.second.action("play", {"id": self.two, "nickname": "RightPlayer"})
            self.wait_job(self.second)
        self.assertEqual(set(self.first.job_snapshot()["runningProfiles"]), {self.one, self.two})
        with self.assertRaisesRegex(ValueError, "уже запущена"):
            self.second.action("play", {"id": self.one})
        with self.assertRaisesRegex(ValueError, "Закрой"):
            self.second.action("deleteBuild", {"id": self.one})
        left.poll.return_value = 7
        state = self.first.job_snapshot()
        self.assertFalse(state["localGameRunning"])
        self.assertEqual(state["gameExit"]["profile"], self.one)
        self.first.window.game_ended.assert_called_once()
        self.second.window.game_ended.assert_not_called()
        self.assertEqual(state["runningProfiles"], [self.two])
        right.poll.return_value = 0
        self.second.process_state()
        self.second.window.game_ended.assert_called_once()

    def test_two_games_in_one_window_both_tracked_and_failure_visible_while_other_runs(self):
        left, right = Mock(), Mock()
        left.poll.return_value = right.poll.return_value = None
        with patch.object(game, "install"), patch.object(game, "launch_local", side_effect=[left, right]):
            self.first.action("play", {"id": self.one})
            self.wait_job(self.first)
            self.first.action("play", {"id": self.two})
            self.wait_job(self.first)
        left.poll.return_value = 8
        state = self.first.job_snapshot()
        self.assertTrue(state["gameRunning"])
        self.assertEqual(state["gameExit"]["profile"], self.one)
        self.first.window.game_ended.assert_not_called()
        right.poll.return_value = 0
        self.first.process_state()
        self.first.window.game_ended.assert_called_once()

    def test_late_profile_claim_is_rechecked_before_queued_worker_changes_files(self):
        entered, release = threading.Event(), threading.Event()
        changed = Mock()
        def occupy():
            with self.second.coordinator.operation():
                entered.set(); release.wait(3)
                self.second.coordinator.claim(self.one)
        thread = threading.Thread(target=occupy)
        thread.start(); self.assertTrue(entered.wait(2))
        try:
            self.first.start_job(lambda _: changed(), profile=self.one)
            release.set(); thread.join(3)
            deadline = time.monotonic() + 3
            while self.first.job_snapshot()["busy"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(self.first.job_snapshot()["ok"])
            changed.assert_not_called()
        finally:
            release.set(); thread.join(3)
            self.second.coordinator.release(self.one)

    def test_namespaces_do_not_overwrite_download_or_update_state(self):
        self.assertNotEqual(self.first.transfers.index, self.second.transfers.index)
        self.assertNotEqual(self.first.updates.root, self.second.updates.root)
        self.assertNotEqual(self.first.window, self.second.window)
        self.assertFalse(self.first._can_update())
        self.second.close()
        self.assertTrue(self.first._can_update())

    def test_new_window_opens_during_peer_file_operation_and_defers_recovery(self):
        with self.first.coordinator.operation():
            # The second metadata-only window must not wait for a long download
            # or touch the first worker's transaction journal.
            with patch("launcher.build_care.BuildCare.recover_pending") as recover:
                third = App(self.root)
                self.addCleanup(third.close)
                self.assertFalse(third.initial_recovery)
                self.assertEqual(third.state()["launcherCount"], 3)
                recover.assert_not_called()
                with self.assertRaisesRegex(ValueError, "другом окне"):
                    third.action("create", {"name": "Later", "version": "1.21.1", "loader": "vanilla"})
        third.action("create", {"name": "Later", "version": "1.21.1", "loader": "vanilla"})
        self.assertTrue(third.initial_recovery)


class WindowClosureTests(unittest.TestCase):
    def test_closed_native_frame_differs_from_hidden_or_unregistered_frame(self):
        from launcher.window import DesktopWindow
        window = DesktopWindow(Path(tempfile.gettempdir()))
        self.assertFalse(window.closed())
        window.native = Mock()
        window.native.hwnd = 84
        window.native.current.return_value = False
        window.awaiting_registration = True
        self.assertFalse(window.closed())
        window.awaiting_registration = False
        self.assertTrue(window.closed())
        window.native.current.return_value = True
        window.parked = True
        self.assertFalse(window.closed())


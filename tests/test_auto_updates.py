"""Automatic updates must stop with their window and never interrupt a game."""

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from launcher import update_helper, updates
from launcher.sessions import atomic_json, process_identity
from launcher.store import Store
from launcher.transfers import TransferManager


PAYLOAD = b"verified windows installer"


def windows_policy():
    # Select the updater's Windows policy without changing pathlib/FileMutex's
    # actual host OS. Those still exercise the real cross-process cache lock.
    return patch.object(updates, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))


def publication(version="2.2.0", schema=1):
    filename = "BlockHarbor-Setup-" + version + ".exe"
    return {
        "version": version, "tag": "v" + version, "filename": filename, "name": "BlockHarbor " + version,
        "download": updates.WEB + "/releases/download/v" + version + "/" + filename,
        "url": updates.WEB + "/releases/tag/v" + version, "notes": "Changes", "published": "",
        "size": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "compatible": schema == 1,
        "prerelease": "-" in version,
        "compatibility": {"format": 1, "version": version, "dataSchema": schema, "minDataSchema": schema,
                          "maxDataSchema": schema, "rollbackSafe": True},
    }


class FakeHelper:
    def __init__(self):
        self.returncode = None
        self.terminated = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


class AutomaticUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root)
        self.transfers = TransferManager(self.root)
        self.shutdown = Mock()
        self.cancel_handoff = Mock()
        self.service = updates.UpdateService(self.store, self.transfers, "2.1.0", can_install=lambda: False,
                                             request_shutdown=self.shutdown, cancel_handoff=self.cancel_handoff)
        self.target = self.root / "installed" / "BlockHarbor.exe"
        self.target.parent.mkdir()
        self.target.write_bytes(b"previous launcher")

    def tearDown(self):
        self.service.close(timeout=0.2)
        close = getattr(self.transfers, "close", None)
        if close:
            close(timeout=0.2)
        self.directory.cleanup()

    def ready(self, info=None):
        info = info or publication()
        path = self.service.cache / info["filename"]
        path.write_bytes(PAYLOAD)
        self.service._remember(info, path)
        self.service.release = info
        return info

    def drain(self):
        worker = self.service._auto_worker
        if worker:
            worker.join(2)
            self.assertFalse(worker.is_alive(), "Automatic check did not finish")

    def remote(self, info=None):
        info = info or publication()
        rows = [{"tag_name": "v" + info["version"], "prerelease": info["prerelease"]}]
        self.enterContext(patch.object(updates, "_read_json", return_value=rows))
        return self.enterContext(patch.object(updates, "release_info", return_value=info))

    def test_default_enabled_preserves_previous_explicit_opt_out(self):
        self.assertTrue(self.service.automatic_enabled())
        self.store.set("checkUpdatesOnStart", "0")
        self.assertFalse(self.service.automatic_enabled())
        self.service.set_automatic(True)
        self.assertTrue(self.service.automatic_enabled())
        self.assertEqual(self.store.get("checkUpdatesOnStart"), "0")

    def test_startup_ignores_recent_check_and_periodic_checks_are_bounded(self):
        self.service.checked = time.time()
        lookup = self.remote(publication("2.1.0"))
        with patch.object(updates.time, "monotonic", return_value=1000), patch.object(updates.time, "time", return_value=1700000000):
            self.service.start_auto()
            self.drain()
            self.service.tick_auto()
            self.drain()
            self.assertEqual(lookup.call_count, 1)
        with patch.object(updates.time, "monotonic", return_value=1000 + updates.AUTO_CHECK_INTERVAL + 1), patch.object(updates.time, "time", return_value=1700000000 + updates.AUTO_CHECK_INTERVAL + 1):
            self.service.tick_auto()
            self.drain()
        self.assertEqual(lookup.call_count, 2)

    def test_disabled_does_not_check_or_download(self):
        self.service.set_automatic(False)
        with patch.object(updates, "_read_json") as fetch, patch.object(self.transfers, "submit") as submit:
            self.service.start_auto()
            self.service.tick_auto()
            self.drain()
        fetch.assert_not_called()
        submit.assert_not_called()

    def test_automatic_download_keeps_hash_size_and_source_checks(self):
        info = publication()
        self.remote(info)
        with windows_policy(), patch.object(self.transfers, "submit", return_value="owned-task") as submit:
            self.service.start_auto()
            self.drain()
        self.assertEqual(submit.call_args.args[0], info["download"])
        self.assertEqual(submit.call_args.kwargs["hashes"], {"sha256": info["sha256"]})
        self.assertEqual(submit.call_args.kwargs["size"], info["size"])
        self.assertEqual(submit.call_args.kwargs["hosts"], updates.ASSET_HOSTS)
        self.assertEqual(self.service._automatic_tasks, {"owned-task"})
        self.assertIsNone(self.service.pending)

    def test_incompatible_release_is_not_downloaded_or_scheduled(self):
        self.remote(publication(schema=2))
        with windows_policy(), patch.object(self.transfers, "submit") as submit:
            self.service.start_auto()
            self.drain()
        submit.assert_not_called()
        self.assertIsNone(self.service.pending)

    def test_download_completion_queues_automatic_install_but_waits_for_idle(self):
        info = self.ready()
        self.remote(info)
        with windows_policy(), patch.object(self.service, "_perform_install") as perform:
            self.service.start_auto()
            self.drain()
            self.assertEqual(self.service.pending["stage"], "queued")
            self.assertTrue(self.service.pending["automatic"])
            self.service.monitor_pending()
            perform.assert_not_called()
            self.service.can_install = lambda: True
            self.service.monitor_pending()
            for worker in list(self.service._workers):
                worker.join(2)
            self.service.monitor_pending()
            perform.assert_called_once()

    def test_opt_out_cancels_automatic_pending_but_preserves_manual_pending(self):
        self.ready()
        with windows_policy():
            self.service.schedule_install(when="idle", automatic=True)
            self.service.set_automatic(False)
            self.assertIsNone(self.service.pending)
            self.service.schedule_install(when="idle")
            manual = self.service.pending
            self.service.set_automatic(False)
            self.assertIs(self.service.pending, manual)

    def test_cancel_automatic_install_snoozes_this_version(self):
        info = self.ready()
        self.remote(info)
        with windows_policy():
            self.service.start_auto()
            self.drain()
            self.service.handle("updateCancelInstall", {})
            self.service._next_auto_check = 0
            self.service.tick_auto()
            self.drain()
        self.assertEqual(self.service._auto_snoozed, info["version"])
        self.assertIsNone(self.service.pending)

    def test_close_interrupts_owned_network_read_and_cannot_publish_late_release(self):
        entered, aborted = threading.Event(), threading.Event()
        class Response:
            def __init__(self):
                sock = SimpleNamespace(shutdown=lambda *_: aborted.set())
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))
            def read(self, _limit):
                entered.set()
                aborted.wait(2)
                return b'[{"tag_name":"v2.2.0"}]'
            def close(self):
                aborted.set()
        with patch.object(updates, "open_https", return_value=Response()), patch.object(updates, "release_info") as metadata:
            self.service.start_auto()
            self.assertTrue(entered.wait(2))
            self.service.close(reason="user", timeout=1)
            self.assertTrue(aborted.is_set())
            self.drain()
        metadata.assert_not_called()
        self.assertIsNone(self.service.release)
        self.assertIsNone(self.service.pending)

    def test_channel_change_during_check_cannot_publish_stale_release(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(_url, **_kwargs):
            entered.set()
            release.wait(2)
            return [{"tag_name": "v2.2.0-rc.1", "prerelease": True}]
        self.store.set("updateChannel", "test")
        with patch.object(updates, "_read_json", side_effect=fetch), patch.object(updates, "release_info", return_value=publication("2.2.0-rc.1")):
            self.service.start_auto()
            self.assertTrue(entered.wait(2))
            self.store.set("updateChannel", "stable")
            release.set()
            self.drain()
        self.assertIsNone(self.service.release)
        self.assertIsNone(self.service.pending)

    def prepare_handoff(self, *, close_during_spawn=False):
        self.ready()
        with windows_policy():
            self.service.schedule_install(when="idle", automatic=True)
        self.service.can_install = lambda: True
        helper = FakeHelper()
        def spawn(command, **_kwargs):
            request = Path(command[-1])
            data = json.loads(request.read_text())
            atomic_json(Path(data["ready"]), {"id": data["id"], "ready": True})
            self.assertFalse(Path(data["commit"]).exists())
            if close_during_spawn:
                self.service.close(reason="user", timeout=0)
            return helper
        with patch.object(self.service, "_launcher", return_value=self.target), patch.object(updates.subprocess, "Popen", side_effect=spawn):
            self.service._perform_install()
        return helper

    def test_user_close_revokes_even_already_acknowledged_handoff(self):
        helper = self.prepare_handoff()
        self.assertTrue(self.service.handoff_ready)
        commit = self.service._handoff_commit
        self.assertTrue(commit.is_file())
        self.service.close(reason="user", timeout=0.2)
        self.assertFalse(self.service.handoff_ready)
        self.assertFalse(commit.exists())
        self.assertGreater(helper.terminated, 0)
        self.cancel_handoff.assert_called()

    def test_update_close_preserves_acknowledged_helper_and_commit(self):
        helper = self.prepare_handoff()
        self.service.close(reason="update", timeout=0.2)
        # A later default finalizer must preserve the already chosen update exit.
        self.service.close(reason="user", timeout=0.2)
        self.assertTrue(self.service.handoff_ready)
        self.assertTrue(self.service._handoff_commit.exists())
        self.assertEqual(helper.terminated, 0)
        self.shutdown.assert_called_once()

    def test_opt_out_revokes_committed_auto_handoff_without_closing_window(self):
        helper = self.prepare_handoff()
        self.service.set_automatic(False)
        self.assertFalse(self.service.closed.is_set())
        self.assertFalse(self.service.handoff_ready)
        self.assertFalse(self.service._handoff_commit.exists())
        self.assertGreater(helper.terminated, 0)
        self.cancel_handoff.assert_called()

    def test_network_error_is_visible_after_reopening(self):
        with patch.object(updates, "_read_json", side_effect=ValueError("Network unavailable")):
            self.service.start_auto()
            self.drain()
        reopened = updates.UpdateService(self.store, self.transfers, "2.1.0")
        self.addCleanup(reopened.close, timeout=0.2)
        self.assertEqual(reopened.state()["error"], "Network unavailable")

    def test_failed_latest_check_blocks_local_automatic_install_of_stale_candidate(self):
        info = self.ready()
        self.service._auto_candidate = dict(info)
        with patch.object(updates, "_read_json", return_value=[{"tag_name": "v2.3.0", "assets": []}]), self.assertRaises(ValueError):
            self.service.check()
        self.assertIsNone(self.service._auto_candidate)
        with windows_policy(), patch.object(self.service, "schedule_install") as install:
            self.service._auto_cycle(self.service._auto_generation, updates.CancellationScope(), remote=False)
        install.assert_not_called()
        with windows_policy(), self.assertRaises(updates.OperationCancelled):
            self.service.schedule_install(when="idle", automatic=True)
        self.assertEqual(self.service.release["version"], info["version"])
        self.assertTrue(self.service.check_status["stale"])

    def test_failed_latest_check_also_revokes_already_queued_automatic_install(self):
        self.ready()
        with windows_policy():
            self.service.schedule_install(when="idle", automatic=True)
        self.assertIsNotNone(self.service.pending)
        with patch.object(updates, "_read_json", return_value=[{"tag_name": "v2.3.0", "assets": []}]), self.assertRaises(ValueError):
            self.service.check()
        self.assertIsNone(self.service.pending)
        self.assertTrue(self.service.cancel_install.is_set())
        self.cancel_handoff.assert_called()

    def test_close_at_helper_spawn_cannot_commit_or_request_restart(self):
        helper = self.prepare_handoff(close_during_spawn=True)
        self.assertGreater(helper.terminated, 0)
        self.assertFalse(self.service.handoff_ready)
        self.assertFalse(self.service._handoff_commit.exists())
        self.shutdown.assert_not_called()

    def test_failed_silent_install_does_not_loop_after_reopening(self):
        info = self.ready()
        request_id = "f" * 24
        self.service.pending = {"version": info["version"], "stage": "launching", "automatic": True, "handoff": request_id}
        self.service._save()
        atomic_json(self.service.root / ("result-" + request_id + ".json"), {"ok": False, "error": "Setup failed", "exitCode": 2})
        reopened = updates.UpdateService(self.store, self.transfers, "2.1.0", can_install=lambda: True, request_shutdown=Mock())
        self.addCleanup(reopened.close, timeout=0.2)
        self.assertEqual(reopened.state()["autoFailure"]["version"], info["version"])
        with windows_policy(), patch.object(updates, "_read_json", return_value=[{"tag_name": "v2.2.0"}]), patch.object(updates, "release_info", return_value=info), patch.object(reopened, "schedule_install") as install:
            reopened.start_auto()
            reopened._auto_worker.join(2)
        install.assert_not_called()
        self.assertEqual(reopened.state()["error"], "Setup failed")

    def test_tampered_ready_installer_never_starts_helper(self):
        info = self.ready()
        (self.service.cache / info["filename"]).write_bytes(b"x" * len(PAYLOAD))
        with windows_policy():
            self.service.schedule_install(when="idle", automatic=True)
        self.service.can_install = lambda: True
        with patch.object(updates.subprocess, "Popen") as spawn:
            self.service._perform_install()
        spawn.assert_not_called()
        self.assertEqual(self.service._auto_failure["version"], info["version"])


class SilentHelperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.updates = self.root / "updates"
        self.source = self.updates / "installers" / "BlockHarbor-Setup-2.2.0.exe"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(PAYLOAD)
        self.target = self.root / "Existing App" / "BlockHarbor.exe"
        self.target.parent.mkdir()
        self.target.write_bytes(b"old launcher")
        self.request_id = "a" * 24
        self.request = self.updates / ("handoff-" + self.request_id + ".json")
        self.commit = self.updates / ("commit-" + self.request_id + ".json")
        self.ready = self.updates / ("ready-" + self.request_id + ".json")
        self.gate = self.root / "launcher-sessions" / "update.json"
        self.gate.parent.mkdir()
        self.data = {"format": 1, "id": self.request_id, "version": "2.2.0", "kind": "installer",
                     "file": str(self.source), "target": str(self.target), "size": len(PAYLOAD),
                     "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "ready": str(self.ready), "commit": str(self.commit),
                     "automatic": True, "waitPids": [123], "sessionRoot": str(self.root), "launcherSession": self.request_id}
        atomic_json(self.request, self.data)
        atomic_json(self.commit, {"id": self.request_id, "install": True})
        atomic_json(self.gate, {"session": self.request_id, "owner": process_identity(os.getpid())})

    def test_successful_silent_install_preserves_path_and_restarts_exactly_once_after_gate_release(self):
        calls = []
        process = Mock()
        process.wait.return_value = 0
        def spawn(command, **_kwargs):
            calls.append(command)
            if len(calls) == 1:
                self.assertIsInstance(command, str)
                self.assertIn(" /S /AUTOUPDATE /D=", command)
                self.assertTrue(command.endswith(" /D=" + str(self.target.parent)))
                self.assertNotIn('"/D=', command)
                self.assertTrue(self.gate.exists())
                return process
            self.assertFalse(self.gate.exists())
            self.assertFalse(self.commit.exists())
            return Mock()
        with patch.object(update_helper, "ProcessHandles"), patch.object(update_helper.subprocess, "Popen", side_effect=spawn):
            update_helper.run(self.request)
        self.assertEqual(calls[1:], [[str(self.target), "--data", str(self.root)]])

    def test_failed_silent_install_does_not_relaunch(self):
        process = Mock()
        process.wait.return_value = 2
        with patch.object(update_helper, "ProcessHandles"), patch.object(update_helper.subprocess, "Popen", return_value=process) as spawn:
            update_helper.run(self.request)
        spawn.assert_called_once()
        result = json.loads((self.updates / ("result-" + self.request_id + ".json")).read_text())
        self.assertFalse(result["ok"])
        self.assertIn("кодом 2", result["error"])

    def test_missing_commit_after_parent_exit_prevents_all_execution(self):
        self.commit.unlink()
        with patch.object(update_helper, "ProcessHandles"), patch.object(update_helper.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "отменена"):
                update_helper.run(self.request)
        spawn.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"old launcher")

    def test_automatic_handoff_requires_explicit_local_commit_path(self):
        self.data.pop("commit")
        atomic_json(self.request, self.data)
        with self.assertRaisesRegex(ValueError, "Подтверждение"):
            update_helper._validate(self.request)


if __name__ == "__main__":
    unittest.main()

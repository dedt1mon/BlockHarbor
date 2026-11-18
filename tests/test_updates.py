import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import updates, update_helper
from launcher.store import Store
from launcher.transfers import TransferManager


def release(version="2.1.0", *, prerelease=False, payload=b"windows executable"):
    tag = "v" + version
    filename = "BlockHarbor-Setup-" + version + ".exe"
    return {
        "tag_name": tag, "name": "BlockHarbor " + version, "body": "Changes", "draft": False,
        "prerelease": prerelease,
        "assets": [
            {"name": filename, "state": "uploaded", "size": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "browser_download_url": updates.WEB + "/releases/download/" + tag + "/" + filename},
            {"name": "BlockHarbor-update.json", "state": "uploaded", "size": 150, "browser_download_url": updates.WEB + "/releases/download/" + tag + "/BlockHarbor-update.json"},
        ],
    }


def manifest(version="2.1.0", schema=1):
    return {"format": 1, "version": version, "dataSchema": schema, "minDataSchema": schema, "maxDataSchema": schema, "rollbackSafe": True}


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root)
        self.transfers = TransferManager(self.root)
        self.service = updates.UpdateService(self.store, self.transfers, "2.0.0", can_install=lambda: True, request_shutdown=Mock())

    def tearDown(self):
        self.directory.cleanup()

    def info(self, version="2.1.0", schema=1):
        with patch.object(updates, "_read_json", return_value=manifest(version, schema)):
            return updates.release_info(release(version))

    def ready(self, version="2.1.0"):
        info = self.info(version)
        path = self.service.cache / info["filename"]
        path.write_bytes(b"windows executable")
        self.service._remember(info, path)
        self.service.release = info
        return info

    def test_semantic_order_and_prereleases(self):
        ordered = ["1.10.0", "2.0.0-alpha.2", "2.0.0-beta.1", "2.0.0-rc.1", "2.0.0", "2.1.0", "10.0.0"]
        self.assertEqual(sorted(reversed(ordered), key=updates.version_key), ordered)
        for value in ["2.0", "v../../file", "02.0.0", "2.0.0-rc.shell", "2.0.0+metadata"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                updates.version_key(value)

    def test_release_asset_must_be_exact_repository_path(self):
        data = release()
        data["assets"][0]["browser_download_url"] = "https://github.com/other/launcher/releases/download/v2.1.0/BlockHarbor-Setup-2.1.0.exe"
        with self.assertRaisesRegex(ValueError, "адрес"):
            updates.release_info(data)

    def test_new_release_requires_explicit_data_compatibility(self):
        data = release()
        data["assets"] = data["assets"][:1]
        with self.assertRaisesRegex(ValueError, "BlockHarbor-update.json"):
            updates.release_info(data)
        incompatible = self.info(schema=2)
        self.assertFalse(incompatible["compatible"])
        self.service.release = incompatible
        with self.assertRaisesRegex(ValueError, "формат"):
            self.service.download()

    def test_manifest_cannot_name_another_version_or_coerce_types(self):
        bad = manifest("9.0.0")
        with patch.object(updates, "_read_json", return_value=bad), self.assertRaisesRegex(ValueError, "Манифест"):
            updates.release_info(release())
        bad = manifest()
        bad["minDataSchema"] = True
        with patch.object(updates, "_read_json", return_value=bad), self.assertRaises(ValueError):
            updates.release_info(release())

    def test_check_filters_stable_and_selects_highest_semver(self):
        rows = [release("2.1.0-rc.1", prerelease=True), release("2.0.1"), release("1.10.0")]
        def fetch(url, **kwargs):
            if url.endswith("per_page=30"):
                return rows
            version = url.split("/releases/download/v")[1].split("/")[0]
            return manifest(version)
        with patch.object(updates, "_read_json", side_effect=fetch):
            self.service.check()
            self.assertEqual(self.service.release["version"], "2.0.1")
            self.service.handle("updateChannel", {"channel": "test"})
            self.service.check()
            self.assertEqual(self.service.release["version"], "2.1.0-rc.1")

    def test_invalid_latest_does_not_silently_offer_older_release(self):
        bad = release("2.1.0")
        bad["assets"] = []
        with patch.object(updates, "_read_json", return_value=[bad, release("2.0.1")]), self.assertRaises(ValueError):
            self.service.check()
        self.assertIsNone(self.service.release)

    def test_cache_reuse_verifies_bytes(self):
        info = self.info()
        path = self.service.cache / info["filename"]
        path.write_bytes(b"windows executable")
        self.assertIsNone(self.service._enqueue(info))
        self.assertTrue(self.service._ready(info["version"]))
        path.write_bytes(b"x" * len(b"windows executable"))
        with patch.object(self.transfers, "submit", return_value="new-transfer") as submit:
            self.assertEqual(self.service._enqueue(info), "new-transfer")
            submit.assert_called_once()

    def test_install_now_refuses_running_game_but_idle_can_queue(self):
        self.ready()
        self.service.can_install = lambda: False
        with patch.object(updates.os, "name", "nt"), patch.object(self.service, "monitor_pending"):
            with self.assertRaisesRegex(ValueError, "Закрой Minecraft"):
                self.service.schedule_install(when="now")
            result = self.service.schedule_install(when="idle")
            self.assertTrue(result["scheduled"])
        self.assertEqual(self.service.pending["stage"], "queued")
        self.service.handle("updateCancelInstall", {})
        self.assertIsNone(self.service.pending)

    def test_rollback_is_only_offered_for_same_data_schema(self):
        self.ready("1.10.0")
        self.assertEqual(self.service.state()["rollback"]["version"], "1.10.0")
        self.service.receipts[0]["compatibility"]["dataSchema"] = 2
        self.assertIsNone(self.service.state()["rollback"])

    def test_installed_release_can_explicitly_forbid_rollback(self):
        self.ready("1.10.0")
        self.ready("2.0.0")
        self.service._ready("2.0.0")["compatibility"]["rollbackSafe"] = False
        self.assertIsNone(self.service.state()["rollback"])

    def test_data_backup_preserves_live_database_and_launcher(self):
        target = self.root / "installed" / "BlockHarbor.exe"
        target.parent.mkdir()
        target.write_bytes(b"current working launcher")
        self.store.set("nickname", "Timon")
        self.service._preserve_current(target)
        self.assertEqual(target.read_bytes(), b"current working launcher")
        copy = self.service.root / "rollback" / "BlockHarbor-2.0.0.exe"
        self.assertEqual(copy.read_bytes(), target.read_bytes())
        self.assertEqual(self.service.rollback_binary["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertTrue(list((self.service.root / "rollback").glob("settings-before-*.sqlite3")))
        self.assertEqual(self.store.get("nickname"), "Timon")

    def test_resume_process_does_not_auto_close_user_session(self):
        self.ready()
        self.service.pending = {"version": "2.1.0", "stage": "queued"}
        self.service._save()
        reopened = updates.UpdateService(self.store, self.transfers, "2.0.0")
        self.assertIsNone(reopened.pending)
        self.assertIn("Предыдущая", reopened.error)

    def test_completed_update_clears_pending_without_false_failure(self):
        self.ready()
        self.service.pending = {"version": "2.1.0", "stage": "launching"}
        self.service._save()
        reopened = updates.UpdateService(self.store, self.transfers, "2.1.0")
        self.assertIsNone(reopened.pending)
        self.assertEqual(reopened.error, "")

    def test_failed_durable_download_callback_is_restored_after_restart(self):
        info = self.info()
        with patch.object(self.transfers, "submit", return_value="task"):
            self.service._enqueue(info)
        reopened = updates.UpdateService(self.store, self.transfers, "2.0.0")
        self.assertEqual(reopened.pending_downloads[0]["version"], info["version"])
        path = reopened.cache / info["filename"]
        path.write_bytes(b"windows executable")
        recovered = updates.UpdateService(self.store, self.transfers, "2.0.0")
        self.assertIsNotNone(recovered._ready(info["version"]))
        self.assertEqual(recovered.pending_downloads, [])


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "installers" / "BlockHarbor-Setup-2.1.0.exe"
        self.source.parent.mkdir()
        self.source.write_bytes(b"installer")
        self.target = self.root / "installed" / "BlockHarbor.exe"
        self.target.parent.mkdir()
        self.target.write_bytes(b"old app")
        self.request_id = "a" * 24
        self.request = self.root / ("handoff-" + self.request_id + ".json")
        self.ready = self.root / ("ready-" + self.request_id + ".json")
        self.data = {"format": 1, "id": self.request_id, "kind": "installer", "file": str(self.source), "target": str(self.target), "ready": str(self.ready), "version": "2.1.0", "sha256": hashlib.sha256(b"installer").hexdigest(), "size": 9, "waitPids": [123, 456]}
        self.request.write_text(json.dumps(self.data))

    def tearDown(self):
        self.directory.cleanup()

    def test_handoff_opens_exact_handles_before_ready_and_waits_before_start(self):
        events = []
        test = self
        class Handles:
            def __init__(self, pids):
                events.append(("opened", pids))
                test.assertFalse(test.ready.exists())
            def wait(self):
                test.assertTrue(test.ready.exists())
                events.append("waited")
            def close(self):
                events.append("closed")
        process = Mock()
        process.wait.return_value = 0
        def spawn(command, **kwargs):
            self.assertIn("waited", events)
            self.assertEqual(command, [str(self.source)])
            events.append("started")
            return process
        with patch.object(update_helper, "ProcessHandles", Handles), patch.object(update_helper.subprocess, "Popen", side_effect=spawn):
            update_helper.run(self.request)
        self.assertEqual(events, [("opened", [123, 456]), "waited", "started", "closed"])
        self.assertFalse(self.request.exists())
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.target.read_bytes(), b"old app")

    def test_changed_payload_never_signals_readiness_or_launches(self):
        self.source.write_bytes(b"corrupted")
        with patch.object(update_helper.subprocess, "Popen") as launch, self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            update_helper.run(self.request)
        launch.assert_not_called()
        self.assertFalse(self.ready.exists())

    def test_helper_rejects_arbitrary_payload_path(self):
        self.data["file"] = str(self.target)
        self.request.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(ValueError, "перенаправлен"):
            update_helper._validate(self.request)


if __name__ == "__main__":
    unittest.main()

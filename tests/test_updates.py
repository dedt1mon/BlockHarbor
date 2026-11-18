import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from launcher import updates, update_helper
from launcher.lifecycle import OperationCancelled
from launcher.sessions import FileMutex
from launcher.store import Store
from launcher.transfers import TransferManager
from launcher.update_cache import CHECK_TTL, retry_at


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
            with self.assertRaisesRegex(ValueError, "Закрой.*Minecraft"):
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
        self.assertFalse(reopened.state()["startupResult"]["ok"])

    def test_completed_update_clears_pending_without_false_failure(self):
        self.ready()
        self.service.pending = {"version": "2.1.0", "stage": "launching"}
        self.service._save()
        reopened = updates.UpdateService(self.store, self.transfers, "2.1.0")
        self.assertIsNone(reopened.pending)
        self.assertEqual(reopened.error, "")
        self.assertTrue(reopened.state()["startupResult"]["ok"])
        self.assertIn("2.1.0", reopened.state()["startupResult"]["message"])

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


class PublicUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root)
        self.transfers = TransferManager(self.root)
        self.service = updates.UpdateService(self.store, self.transfers, "2.3.0")
        self.services = [self.service]
        self.payload = b"verified installer payload"
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.calls = []

    def tearDown(self):
        for service in self.services:
            service.close(timeout=0.2)
        self.transfers.close(timeout=0.2)
        self.directory.cleanup()

    class Response(io.BytesIO):
        def __init__(self, body, *, headers=None, status=200):
            super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
            self.headers, self.status = headers or {}, status
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return super().read(size)

    def metadata(self, version="2.4.0"):
        return {**manifest(version), "installer": {"name": "BlockHarbor-Setup-" + version + ".exe", "size": len(self.payload), "sha256": self.digest}}

    def peer(self, slot=2):
        service = updates.UpdateService(self.store, self.transfers, "2.3.0", namespace="window-" + str(slot))
        self.services.append(service)
        return service

    def test_exhausted_api_uses_existing_23_manifest_hash_and_one_byte_probe(self):
        tag, version = "v2.3.0", "2.3.0"
        filename = "BlockHarbor-Setup-" + version + ".exe"
        download = updates.WEB + "/releases/download/" + tag + "/" + filename
        probe = self.Response(self.payload[:1], headers={"Content-Range": "bytes 0-0/" + str(len(self.payload)), "Content-Length": "1"}, status=206)
        def request(url, hosts, **kwargs):
            self.calls.append((url, hosts, kwargs))
            if url.startswith(updates.API):
                raise HTTPError(url, 403, "rate limit exceeded", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700003600"}, io.BytesIO(b"{}"))
            if url.endswith("/latest/download/BlockHarbor-update.json"):
                return self.Response(manifest(version))
            if url.endswith("/SHA256SUMS.txt"):
                return self.Response((self.digest + "  " + filename + "\n").encode())
            self.assertEqual(url, download)
            self.assertEqual(kwargs["headers"]["Range"], "bytes=0-0")
            return probe
        with patch.object(updates.time, "time", return_value=1700000000), patch.object(updates, "open_https", side_effect=request):
            self.service.check()
        info = self.service.state()["release"]
        self.assertEqual((info["version"], info["size"], info["sha256"]), (version, len(self.payload), self.digest))
        self.assertTrue(info["compatible"])
        self.assertEqual(probe.read_sizes, [2])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.service.check_status["source"], "public")
        self.assertGreater(self.service.check_status["apiRetryAt"], 1700003600)
        self.assertEqual(self.service.error, "")

    def test_new_public_manifest_requires_no_installer_probe_or_extra_hash_request(self):
        with patch.object(updates, "_read_json", return_value=self.metadata()) as fetch, patch.object(updates, "_installer_size") as probe, patch.object(updates, "_checksum") as sums:
            info = updates.public_release()
        self.assertEqual(info["sha256"], self.digest)
        self.assertEqual(info["size"], len(self.payload))
        self.assertEqual(fetch.call_args.args[0], updates.WEB + "/releases/latest/download/BlockHarbor-update.json")
        probe.assert_not_called()
        sums.assert_not_called()

    def test_shared_cache_and_api_deadline_survive_clicks_peers_and_restarts(self):
        peer = self.peer()
        def request(url, _hosts, **_kwargs):
            self.calls.append(url)
            if url.startswith(updates.API):
                raise HTTPError(url, 403, "rate limit exceeded", {"Retry-After": "120", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700003600"}, io.BytesIO())
            self.assertEqual(url, updates.WEB + "/releases/latest/download/BlockHarbor-update.json")
            return self.Response(self.metadata())
        with patch.object(updates, "open_https", side_effect=request):
            with patch.object(updates.time, "time", return_value=1700000000):
                self.service.check()
                for _ in range(8):
                    self.service.check()
                    peer.check()
                    self.service.state()
                    peer.state()
                self.assertEqual(len(self.calls), 2)
                self.assertTrue(peer.check_status["cached"])
                first_checked = peer.checked
            with patch.object(updates.time, "time", return_value=1700000000 + CHECK_TTL + 1):
                reopened = self.peer(3)
                reopened.check()
                self.assertEqual(sum(url.startswith(updates.API) for url in self.calls), 1)
                self.assertEqual(len(self.calls), 3)
                self.assertGreater(reopened.checked, first_checked)
            with patch.object(updates.time, "time", return_value=1700003602):
                reopened.check()
                self.assertEqual(sum(url.startswith(updates.API) for url in self.calls), 2)

    def test_last_successful_api_response_can_exhaust_quota_for_rollback_lookup(self):
        def request(url, _hosts, **_kwargs):
            self.calls.append(url)
            if url.startswith(updates.API):
                return self.Response([release("2.4.0", payload=self.payload)], headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700003600"})
            if "/download/v2.4.0/" in url:
                return self.Response(self.metadata())
            self.assertIn("/download/v2.3.0/", url)
            return self.Response(self.metadata("2.3.0"))
        with patch.object(updates.time, "time", return_value=1700000000), patch.object(updates, "open_https", side_effect=request):
            self.service.check()
            self.service._version_release("2.3.0")
            self.service._version_release("2.3.0")
        self.assertEqual(sum(url.startswith(updates.API) for url in self.calls), 1)
        self.assertEqual(len(self.calls), 3)

    def test_failed_public_fallback_is_localized_and_has_shared_backoff(self):
        peer = self.peer()
        def request(url, _hosts, **_kwargs):
            self.calls.append(url)
            raise HTTPError(url, 429, "Too Many Requests", {"Retry-After": "120"}, io.BytesIO())
        with patch.object(updates.time, "time", return_value=1700000000), patch.object(updates, "open_https", side_effect=request):
            for service in [self.service, self.service, peer, peer]:
                with self.assertRaisesRegex(ValueError, "GitHub.*ограничил"):
                    service.check()
                self.assertNotIn("HTTP Error", service.state()["error"])
                self.assertGreaterEqual(service.check_status["retryAt"], 1700000120)
            self.assertEqual(len(self.calls), 2)

    def test_retry_headers_http_dates_and_bounded_exponential_backoff(self):
        now = 1700000000
        self.assertEqual(retry_at({"Retry-After": formatdate(now + 180, usegmt=True)}, now=now), now + 180)
        self.assertEqual(retry_at({"Retry-After": "30", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(now + 3600)}, now=now), now + 3601)
        self.assertEqual(retry_at({}, now=now, failures=3), now + 240)
        self.assertEqual(retry_at({"Retry-After": "NaN"}, now=now), now + 60)
        self.assertEqual(retry_at({"Retry-After": "999999999999999999"}, now=now), now + 86400)

    def test_manifest_installer_identity_types_and_api_consistency_are_required(self):
        for field, value in [("name", "other.exe"), ("size", True), ("size", updates.MAX_INSTALLER + 1), ("sha256", "a" * 63)]:
            comp = self.metadata()
            comp["installer"][field] = value
            with self.subTest(field=field, value=value), patch.object(updates, "_read_json", return_value=comp), self.assertRaises(ValueError):
                updates.public_release()
        comp = self.metadata()
        comp["installer"]["sha256"] = "a" * 64
        with patch.object(updates, "_read_json", return_value=comp), self.assertRaisesRegex(ValueError, "не совпадают"):
            updates.release_info(release("2.4.0", payload=self.payload))
        with patch.object(updates, "_read_json", return_value=self.metadata("2.4.0-rc.1")), self.assertRaisesRegex(ValueError, "Стабильный"):
            updates.public_release()

    def test_probe_does_not_read_ignored_range_and_rejects_ambiguous_sizes(self):
        response = self.Response(self.payload, headers={"Content-Length": str(len(self.payload))})
        with patch.object(updates, "open_https", return_value=response):
            self.assertEqual(updates._installer_size(updates.WEB + "/releases/download/v2.3.0/BlockHarbor-Setup-2.3.0.exe"), len(self.payload))
        self.assertEqual(response.read_sizes, [])
        for headers, status, body in [({}, 200, b""), ({"Content-Range": "bytes 1-1/100"}, 206, b"x"), ({"Content-Range": "bytes 0-0/100"}, 206, b"xx"), ({"Content-Length": str(updates.MAX_INSTALLER + 1)}, 200, b"")]:
            with self.subTest(headers=headers), patch.object(updates, "open_https", return_value=self.Response(body, headers=headers, status=status)), self.assertRaises(ValueError):
                updates._installer_size(updates.WEB + "/releases/download/v2.3.0/BlockHarbor-Setup-2.3.0.exe")

    def test_duplicate_checksums_never_offer_installer(self):
        line = self.digest + "  BlockHarbor-Setup-2.3.0.exe\n"
        with patch.object(updates, "_read_json", return_value=manifest("2.3.0")), patch.object(updates, "open_https", return_value=self.Response((line * 2).encode())), patch.object(updates, "_installer_size") as probe, self.assertRaisesRegex(ValueError, "единственная"):
            updates.public_release()
        probe.assert_not_called()

    def test_test_channel_uses_highest_feed_prerelease_and_exact_tag_manifest(self):
        feed = ('<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(
            '<entry><link rel="alternate" href="' + updates.WEB + '/releases/tag/v' + version + '"/><title>' + version + '</title><content type="html">&lt;p&gt;Changes&lt;/p&gt;</content></entry>'
            for version in ["2.3.0", "2.4.0-rc.2", "2.4.0-rc.1"]) + '</feed>').encode()
        with patch.object(updates, "open_https", return_value=self.Response(feed)), patch.object(updates, "_read_json", return_value=self.metadata("2.4.0-rc.2")) as manifest_fetch:
            info = updates.public_release("test")
        self.assertEqual(info["version"], "2.4.0-rc.2")
        self.assertTrue(info["prerelease"])
        self.assertEqual(info["notes"], "Changes")
        self.assertEqual(manifest_fetch.call_args.args[0], updates.WEB + "/releases/download/v2.4.0-rc.2/BlockHarbor-update.json")
        with patch.object(updates, "open_https", return_value=self.Response(feed)), patch.object(updates, "_read_json", return_value=self.metadata("2.3.0")) as manifest_fetch, self.assertRaisesRegex(ValueError, "Манифест"):
            updates.public_release("test")
        self.assertEqual(manifest_fetch.call_count, 1)

    def test_feed_rejects_entities_and_foreign_or_injected_release_links(self):
        for body in [b'<!DOCTYPE feed [<!ENTITY bad "x">]><feed/>', b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="https://github.com/other/repo/releases/tag/v99.0.0"/></entry></feed>', ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="' + updates.WEB + '/releases/tag/v9.0.0/../../evil"/></entry></feed>').encode()]:
            with self.subTest(body=body), patch.object(updates, "open_https", return_value=self.Response(body)), patch.object(updates, "_read_json") as manifest_fetch, self.assertRaises(ValueError):
                updates.public_release("test")
            manifest_fetch.assert_not_called()

    def test_closing_window_cancels_shared_cache_lock_wait(self):
        lock = FileMutex(self.service.source_cache.lock_path)
        self.assertTrue(lock.acquire())
        errors = []
        def check():
            try:
                self.service.check()
            except Exception as exc:
                errors.append(exc)
        with patch.object(updates, "open_https") as fetch:
            worker = threading.Thread(target=check)
            worker.start()
            time.sleep(0.08)
            self.service.close(timeout=0.2)
            worker.join(1)
            lock.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OperationCancelled)
        fetch.assert_not_called()


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

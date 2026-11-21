"""Automatic updates must preserve public IDs and ignore private data."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import community_catalog as catalog
from launcher.store import Store


class Server:
    origin = "https://catalog.test"
    public_id = "BH-B5E5524DA526"

    def __init__(self):
        self.blob = b""
        self.parts = {}
        self.metadata = None
        self.listing = None
        self.fail_update = False
        self.revisions = 0
        self.publish_count = 0

    def json(self, method, path, *, token=None, payload=None, block=None):
        if (method, path) == ("POST", "uploads"):
            self.metadata = dict(payload)
            self.publish_count += 1
            return {"upload": "a" * 32, "token": "b" * 64, "chunkSize": catalog.CHUNK}
        if path == "uploads/" + "a" * 32 + "/complete":
            self.blob = b"".join(self.parts[k] for k in sorted(self.parts))
            self.listing = {"id": self.public_id, **self.metadata}
            return self.listing.copy()
        if path.startswith("uploads/" + "a" * 32 + "/parts/"):
            self.parts[int(path.rsplit("/", 1)[-1])] = block
            return {}
        if (method, path) == ("POST", "builds/" + self.public_id + "/revisions"):
            if self.fail_update:
                raise catalog.CatalogError(0, "offline")
            self.metadata = dict(payload)
            self.parts = {}
            return {"upload": "c" * 32, "chunkSize": catalog.CHUNK}
        if path.startswith("revisions/" + "c" * 32 + "/parts/"):
            self.parts[int(path.rsplit("/", 1)[-1])] = block
            return {}
        if (method, path) == ("POST", "revisions/" + "c" * 32 + "/complete"):
            payload = b"".join(self.parts[k] for k in sorted(self.parts))
            assert hashlib.sha256(payload).hexdigest() == self.metadata["sha256"]
            self.blob = payload
            self.listing.update(self.metadata)
            self.revisions += 1
            return self.listing.copy()
        if (method, path) == ("DELETE", "builds/" + self.public_id):
            self.listing = None
            return {}
        raise AssertionError("Unexpected API request: " + method + " " + path)


class AutoPublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.pid = self.store.create("Моя сборка", "1.20.1", "fabric")
        self.api = Server()
        self.service = catalog.CommunityCatalog(self.store, self.api)
        self.sent = []
        self.listing = self.service.publish(self.pid, "Для друзей", "adventure", self.sent.append)

    def write(self, name, data):
        path = self.store.folder(self.pid) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_auto_update_preserves_id_and_metadata(self):
        old_sha = self.listing["sha256"]
        self.write("mods/extra.jar", b"mod-file-v1")
        self.assertIsNone(self.service.next_auto_update())
        with patch.object(catalog.time, "monotonic", return_value=self.service._scan_at + 10):
            self.assertIsNone(self.service.next_auto_update())
        with patch.object(catalog.time, "monotonic", return_value=self.service._scan_at + 16):
            # The candidate has been stable for at least 15 seconds.
            self.assertEqual(self.service.next_auto_update(), (self.pid, self.api.public_id))
        done = self.service.sync_existing(self.pid, self.api.public_id, self.sent.append)
        self.assertIn(self.api.public_id, done)
        self.assertEqual(self.api.publish_count, 1)
        self.assertEqual(self.api.revisions, 1)
        after = self.service.publications(self.pid)["publications"]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["id"], self.api.public_id)
        self.assertNotEqual(after[0]["sha256"], old_sha)
        self.assertFalse(after[0]["autoSync"]["error"])

    def test_private_worlds_and_logs_never_trigger_autoupdate(self):
        self.write("saves/home/level.dat", b"world changes")
        self.write("logs/latest.log", b"auth token and other private data")
        with patch.object(catalog.time, "monotonic", return_value=100):
            self.assertIsNone(self.service.next_auto_update())
        with patch.object(catalog.time, "monotonic", return_value=150):
            self.assertIsNone(self.service.next_auto_update())
        self.assertEqual(self.api.revisions, 0)

    def test_offline_keeps_old_snapshot_and_records_retry(self):
        old_sha = self.api.listing["sha256"]
        self.write("mods/new.jar", b"new version")
        self.api.fail_update = True
        with self.assertRaisesRegex(catalog.CatalogError, "offline"):
            self.service.sync_existing(self.pid, self.api.public_id, self.sent.append)
        self.assertEqual(self.api.listing["sha256"], old_sha)
        record = self.service.publications(self.pid)["publications"][0]
        self.assertGreater(record["autoSync"]["retryAt"], time.time())
        self.assertIn("offline", record["autoSync"]["error"])
        self.assertIsNone(self.service.next_auto_update())

    def test_unpublish_removes_autosync_registration(self):
        self.service.unpublish(self.pid, self.api.public_id)
        self.write("mods/another.jar", b"mod bytes")
        self.assertEqual(self.service.publications(self.pid)["publications"], [])
        self.assertIsNone(self.service.next_auto_update())

    def test_restart_restores_published_autosync_state(self):
        self.write("resourcepacks/green.zip", b"pack v1")
        restarted = catalog.CommunityCatalog(self.store, self.api)
        self.assertEqual(len(restarted.publications(self.pid)["publications"]), 1)
        with patch.object(catalog.time, "monotonic", return_value=100):
            self.assertIsNone(restarted.next_auto_update())
        with patch.object(catalog.time, "monotonic", return_value=120):
            self.assertEqual(restarted.next_auto_update(), (self.pid, self.api.public_id))


if __name__ == "__main__":
    unittest.main()

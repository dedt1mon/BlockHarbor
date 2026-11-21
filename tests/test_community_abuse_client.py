"""Public upload abuse limits and user-facing rate-block wiring."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from launcher import community_catalog as catalog
from launcher.store import Store


class RejectingOpener:
    def open(self, request, timeout=None):
        response = {
            "error": "Публикация временно заблокирована из-за слишком частых попыток.",
            "code": "PUBLISH_RATE_BAN",
            "retryAfterSeconds": 3600,
            "blockedUntil": int(time.time()) + 3600,
        }
        raise HTTPError(
            request.full_url, 429, "Too Many Requests", None,
            io.BytesIO(json.dumps(response).encode("utf-8")),
        )


class ClientAbuseTests(unittest.TestCase):
    def test_server_ban_metadata_reaches_client_exception(self):
        api = catalog.CatalogAPI("https://catalog.example")
        api.opener = RejectingOpener()
        with self.assertRaises(catalog.CatalogError) as caught:
            api.json("POST", "uploads", payload={"name": "Example"})
        error = caught.exception
        self.assertEqual(error.code, 429)
        self.assertEqual(error.reason_code, "PUBLISH_RATE_BAN")
        self.assertEqual(error.retry_after, 3600)
        self.assertGreater(error.blocked_until, time.time())
        self.assertIn("Публикация временно заблокирована", str(error))

    def test_legacy_archives_remain_compatible_with_smaller_new_upload_limit(self):
        self.assertEqual(catalog.MAX_PUBLIC_UPLOAD, 256 * 1024**2)
        self.assertGreater(catalog.MAX_ARCHIVE, catalog.MAX_PUBLIC_UPLOAD)
        old = {
            "id": "BH-AB1234567890", "name": "Большой старый модпак",
            "minecraft": "1.20.1", "loader": "fabric", "category": "adventure",
            "size": 512 * 1024**2, "sha256": "a" * 64,
        }
        self.assertEqual(catalog._listing(old)["size"], old["size"])

    def test_large_upload_is_rejected_before_contacting_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            pid = store.create("Сборка", "1.20.1", "fabric")
            service = catalog.CommunityCatalog(store)
            with patch.object(catalog.build_archive, "export_build",
                              side_effect=ValueError("Публичная сборка превышает лимит каталога по размеру")) as export:
                with self.assertRaisesRegex(ValueError, "256 МиБ"):
                    service.publish(pid, "Сборка", "adventure", lambda text: None)
            self.assertEqual(export.call_args.kwargs["max_bytes"], catalog.MAX_PUBLIC_UPLOAD)


    def test_capabilities_warn_for_legacy_and_unknown_server(self):
        class Legacy:
            origin = "https://catalog.example"
            def json(self, method, path, **kwargs):
                raise catalog.CatalogError(404, "No such endpoint")
        class Unknown:
            origin = "https://catalog.example"
            def json(self, method, path, **kwargs):
                raise catalog.CatalogError(503, "Unavailable")
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            self.assertEqual(catalog.CommunityCatalog(store, Legacy()).capabilities()["mode"], "legacy")
            self.assertFalse(catalog.CommunityCatalog(store, Unknown()).capabilities()["enabled"])

    def test_capabilities_verify_only_complete_live_policy(self):
        policy = {
            "publicRetentionSeconds": 3600, "manifestFormat": 1,
            "publishLimits": {
                "maxArchiveBytes": 256 * 1024**2,
                "maxActive": 3, "windowSeconds": 600,
                "maxAttempts": 10, "banSeconds": 5 * 3600,
            },
        }
        class PolicyAPI:
            origin = "https://catalog.example"
            def __init__(self, data):
                self.data = data
            def json(self, method, path, **kwargs):
                assert (method, path) == ("GET", "capabilities")
                return self.data
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            active = catalog.CommunityCatalog(store, PolicyAPI(policy))
            self.assertTrue(active.capabilities()["enabled"])
            changed = {**policy, "publishLimits": {**policy["publishLimits"], "banSeconds": 60}}
            self.assertFalse(catalog.CommunityCatalog(store, PolicyAPI(changed)).capabilities()["enabled"])

    def test_ban_is_rendered_as_a_modal_not_a_global_launcher_lock(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "ui" / "pack-hub.js").read_text(encoding="utf-8")
        application = (root / "main.py").read_text(encoding="utf-8")
        self.assertIn('modal("Публикация временно заблокирована"', script)
        self.assertIn('readPublicationCapabilities(form);', script)
        self.assertIn('Сервер не подтвердил срок автоматического удаления.', script)
        self.assertIn('community-policy-status', script)
        self.assertIn("job.publicationBan", script)
        self.assertIn('Понятно', script)
        self.assertIn("Остальные возможности BlockHarbor работают", script)
        self.assertIn('final["publicationBan"]', application)
        self.assertIn('exc.reason_code == "PUBLISH_RATE_BAN"', application)


if __name__ == "__main__":
    unittest.main()

import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher.community_catalog import CatalogAPI, CatalogError, CommunityCatalog
from launcher.experience import Experience
from launcher.store import Store


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "Профиль с пробелами")

    def test_old_history_migrates_without_deleting_rows(self):
        with self.store.connection() as db:
            db.execute("CREATE TABLE notifications(id INTEGER PRIMARY KEY,created REAL NOT NULL,message TEXT NOT NULL,level TEXT NOT NULL)")
            db.execute("INSERT INTO notifications VALUES(17,1000,'Старое сообщение','info')")
        service = Experience(self.store)
        self.assertEqual(service.state()["notificationUnread"], 1)
        snapshot = service.notifications()
        self.assertEqual(snapshot["lastId"], 17)
        self.assertFalse(snapshot["items"][0]["read"])
        service.read_notifications(17)
        self.assertEqual(Experience(Store(self.store.root)).notifications()["unread"], 0)

    def test_read_watermark_does_not_consume_concurrent_or_post_clear_message(self):
        service = Experience(self.store)
        service.notify("Первое")
        viewed = service.notifications()
        service.notify("Пришло после открытия")
        result = service.handle("experienceReadNotifications", {"throughId": viewed["lastId"]})
        self.assertEqual(result["unread"], 1)
        self.assertEqual([item["read"] for item in service.notifications()["items"]], [False, True])
        old_watermark = result["lastId"]
        service.handle("experienceClearNotifications", {})
        service.notify("Пришло после очистки")
        self.assertGreater(service.notifications()["lastId"], old_watermark)
        self.assertEqual(service.read_notifications(old_watermark)["unread"], 1)
        for invalid in (None, -1, True, "1", 2**64):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service.read_notifications(invalid)

    def test_server_event_deduplicates_across_windows_restart_and_clear(self):
        first, second = Experience(self.store), Experience(Store(self.store.root))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: (first if n % 2 else second).notify(
                "Срок публикации истёк", event_key="server:event-1"), range(20)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(first.notification_unread(), 1)
        first.handle("experienceClearNotifications", {})
        reopened = Experience(Store(self.store.root))
        self.assertFalse(reopened.notify("Срок публикации истёк", event_key="server:event-1"))
        self.assertTrue(reopened.notify("Срок публикации истёк", event_key="server:event-2"))
        self.assertEqual(reopened.notification_unread(), 1)

    def test_history_remains_bounded_and_ids_remain_increasing(self):
        service = Experience(self.store)
        for index in range(220):
            service.notify("Сообщение " + str(index))
        data = service.notifications()
        self.assertEqual(len(data["items"]), 200)
        self.assertEqual(data["unread"], 200)
        self.assertEqual(data["lastId"], 220)


class ReceiptAPI:
    origin = "https://catalog.example"
    public_id = "BH-A123456789BC"

    def __init__(self, expires):
        self.event = {"id": "publication-expired:" + self.public_id, "type": "publicationExpired",
                      "build": self.public_id, "created": expires, "removedAt": expires + 2}
        self.offline = False
        self.lose_ack = False
        self.calls = []

    def json(self, method, path, *, token=None, **kwargs):
        self.calls.append((method, path))
        assert path == "publications/" + self.public_id + "/events"
        assert token == "b" * 64
        if self.offline:
            raise CatalogError(0, "Нет сети")
        if self.event is None:
            raise CatalogError(404, "Подтверждение уже принято")
        if method == "GET":
            return {"events": [self.event]}
        self.event = None
        if self.lose_ack:
            self.lose_ack = False
            raise CatalogError(0, "Ответ подтверждения потерян")
        return {"acknowledged": True}


class PublicationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.experience = Experience(self.store)
        self.expires = int(time.time()) - 60
        self.api = ReceiptAPI(self.expires)
        self.community = CommunityCatalog(self.store, self.api)
        self.listing = {"id": self.api.public_id, "name": "Мир друзей", "minecraft": "1.20.1",
                        "loader": "fabric", "category": "adventure", "size": 100,
                        "sha256": "a" * 64, "expiresAt": self.expires}
        with self.store.connection() as db:
            self.community._remember_receipt(db, self.listing, "b" * 64)

    def retry_now(self):
        with self.store.connection() as db:
            db.execute("UPDATE community_receipts SET next_check=0")

    def test_offline_server_event_delivered_after_reconnect_and_restart(self):
        self.api.offline = True
        self.assertEqual(self.community.sync_notifications(self.experience.notify), 0)
        self.assertEqual(self.experience.notification_unread(), 0)
        self.api.offline = False
        self.retry_now()
        reopened = CommunityCatalog(Store(self.store.root), self.api)
        self.assertEqual(reopened.sync_notifications(self.experience.notify), 1)
        snapshot = self.experience.notifications()
        self.assertEqual(snapshot["unread"], 1)
        self.assertEqual(snapshot["items"][0]["created"], self.expires)
        self.assertIn("автоматически удалена с сервера", snapshot["items"][0]["message"])
        self.assertEqual(reopened.sync_notifications(self.experience.notify), 0)

    def test_lost_ack_does_not_duplicate_or_keep_retrying_missing_server_event(self):
        self.api.lose_ack = True
        self.assertEqual(self.community.sync_notifications(self.experience.notify), 0)
        self.assertEqual(self.experience.notification_unread(), 1)
        self.assertIsNone(self.api.event)
        self.retry_now()
        restarted = CommunityCatalog(Store(self.store.root), self.api)
        self.assertEqual(restarted.sync_notifications(self.experience.notify), 1)
        self.assertEqual(self.experience.notification_unread(), 1)
        self.assertEqual([method for method, _ in self.api.calls], ["GET", "DELETE", "DELETE"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM community_receipts").fetchone()[0], 0)

    def test_bad_receipt_is_not_acknowledged_or_announced(self):
        self.api.event["build"] = "BH-FFFFFFFFFFFF"
        self.assertEqual(self.community.sync_notifications(self.experience.notify), 0)
        self.assertEqual(self.experience.notification_unread(), 0)
        self.assertEqual(len(self.api.calls), 1)

    def test_old_publication_receipt_survives_listing_retirement(self):
        pid = self.store.create("Исходная сборка", "1.20.1", "fabric")
        with self.store.connection() as db:
            db.execute("DELETE FROM community_receipts")
            db.execute("INSERT INTO community_uploads VALUES(?,?,?,?,?,?)",
                       (self.api.origin, pid, "fingerprint", "a" * 32, "b" * 64, json.dumps(self.listing)))
        restarted = CommunityCatalog(Store(self.store.root), self.api)
        self.assertEqual(restarted.publications(pid)["publications"], [])
        self.assertEqual(restarted.sync_notifications(self.experience.notify), 1)
        self.assertTrue(self.store.folder(pid).exists())

    def test_pre_retention_owner_token_survives_server_migration_and_public_404(self):
        pid = self.store.create("Старая публикация", "1.20.1", "fabric")
        legacy = {key: value for key, value in self.listing.items() if key != "expiresAt"}
        with self.store.connection() as db:
            db.execute("DELETE FROM community_receipts")
            db.execute("INSERT INTO community_uploads VALUES(?,?,?,?,?,?)",
                       (self.api.origin, pid, "legacy", "a" * 32, "b" * 64, json.dumps(legacy)))
        original = self.api.json
        def migrated(method, path, **kwargs):
            if path.startswith("builds/"):
                raise CatalogError(404, "Срок публикации истёк")
            return original(method, path, **kwargs)
        self.api.json = migrated
        restarted = CommunityCatalog(Store(self.store.root), self.api)
        self.assertEqual(restarted.publications(pid)["publications"], [])
        self.assertEqual(restarted.sync_notifications(self.experience.notify), 1)
        self.assertEqual(self.experience.notification_unread(), 1)

    def test_ack_retires_upload_record_so_restart_cannot_requeue_it(self):
        pid = self.store.create("Завершённая публикация", "1.20.1", "fabric")
        with self.store.connection() as db:
            db.execute("INSERT INTO community_uploads VALUES(?,?,?,?,?,?)",
                       (self.api.origin, pid, "old", "a" * 32, "b" * 64, json.dumps(self.listing)))
        self.assertEqual(self.community.sync_notifications(self.experience.notify), 1)
        restarted = CommunityCatalog(Store(self.store.root), self.api)
        self.assertEqual(restarted.sync_notifications(self.experience.notify), 0)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM community_receipts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM community_uploads").fetchone()[0], 0)
        self.assertTrue(self.store.folder(pid).exists())


class CatalogRetryTests(unittest.TestCase):
    def test_safe_json_retry_is_bounded_and_reservations_are_not_repeated(self):
        api = CatalogAPI("https://catalog.example")
        for method, path, attempts in (("GET", "capabilities", 3), ("PUT", "uploads/id/parts/1", 3),
                                       ("POST", "uploads/id/complete", 3), ("POST", "uploads", 1)):
            with self.subTest(method=method, path=path):
                with patch.object(api, "request", side_effect=CatalogError(503, "Недоступно")) as request, patch("launcher.community_catalog.lifecycle.wait"):
                    with self.assertRaises(CatalogError):
                        api.json(method, path)
                self.assertEqual(request.call_count, attempts)

    def test_rate_ban_and_invalid_token_are_not_retried(self):
        api = CatalogAPI("https://catalog.example")
        for code in (403, 404, 410, 422, 429):
            with self.subTest(code=code), patch.object(api, "request", side_effect=CatalogError(code, "Отклонено")) as request:
                with self.assertRaises(CatalogError):
                    api.json("GET", "capabilities")
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()

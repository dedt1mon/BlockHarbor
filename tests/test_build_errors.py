import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from launcher import build_errors
from launcher.store import Store


class BuildErrorsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.pid = self.store.create("С друзьями", "1.20.1", "forge")

    def test_profile_isolation_grouping_redaction_retention_and_reopen(self):
        other = self.store.create("Вторая", "1.20.1", "forge")
        for i in range(52):
            build_errors.record_operation(self.store, self.pid, "Скачивание", "HTTP 503 accessToken=hiddenvalue " + str(i))
        rows = build_errors.history(Store(self.store.root), self.pid)["groups"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["events"]), 50)
        self.assertNotIn("hiddenvalue", json.dumps(rows))
        self.assertTrue(rows[0]["events"][0]["evidence"].endswith("51"))
        self.assertFalse(build_errors.history(self.store, other)["groups"])

    def test_neoforge_route_timeouts_are_visible_as_download_failures(self):
        message = (
            "Не удалось подготовить neoforge-21.1.267-installer.jar. "
            "Загрузка с neoforged.forgecdn.net не завершена. "
            "Проверь подключение и снова нажми «Играть». Скачанные файлы сохраняются. "
            "Проверенные маршруты: maven.neoforged.net (urllib): тайм-аут; "
            "maven.neoforged.net (requests): тайм-аут; "
            "neoforged.forgecdn.net (requests): тайм-аут; "
            "neoforged.forgecdn.net (urllib): тайм-аут. "
            "Полученная часть установщика сохранена для следующей попытки. "
            "Резервная загрузка также не завершена."
        )
        stage = "Скачиваем installer.jar · 0.0 МБ / 6.7 МБ"
        build_errors.record_operation(self.store, self.pid, stage, message)
        group = build_errors.history(Store(self.store.root), self.pid)["groups"][0]
        self.assertEqual(group["title"], "Не удалось получить файл или сведения о нём")
        self.assertEqual(group["events"][0]["stage"], stage)
        self.assertEqual(group["events"][0]["evidence"], message)

    def test_download_evidence_preserves_integrity_and_dependency_classification(self):
        cases = (
            ("Не удалось подготовить installer.jar: тайм-аут", "download"),
            ("Не удалось подготовить installer.jar: истекло время ожидания ответа", "download"),
            ("Загрузка с neoforged.forgecdn.net не завершена.", "download"),
            ("Контрольная сумма installer.jar не совпала. Ранее: тайм-аут.", "integrity"),
            ("Нет обязательной зависимости testlib. Ранее: тайм-аут.", "dependency"),
            ("Не удалось подготовить профиль запуска.", "operation"),
        )
        for message, category in cases:
            with self.subTest(message=message):
                build_errors.record_operation(self.store, self.pid, "Подготовка", message)
                with self.store.connection() as db:
                    actual = db.execute("SELECT category FROM build_errors ORDER BY id DESC LIMIT 1").fetchone()[0]
                self.assertEqual(actual, category)

    def test_large_crash_report_retains_root_exception_at_top(self):
        reports = self.store.folder(self.pid) / "crash-reports"
        reports.mkdir()
        (reports / "crash-2026-11-20_12.34.56-client.txt").write_text(
            "java.lang.OutOfMemoryError: Java heap space\n" + "environment data\n" * 10000
        )
        build_errors.record_exit(self.store, self.pid, 1, time.time() - 2)
        result = build_errors.history(self.store, self.pid)["groups"][0]
        self.assertIn("памяти", result["title"])
        self.assertIn("crash-reports/", result["events"][0]["evidence"])

    def test_clear_snapshot_preserves_new_errors_other_builds_and_game_logs(self):
        other = self.store.create("Вторая", "1.20.1", "forge")
        logs = self.store.folder(self.pid) / "logs"
        logs.mkdir()
        log = logs / "latest.log"
        log.write_text("Minecraft log remains available")
        build_errors.record_operation(self.store, self.pid, "Скачивание", "HTTP 503 old")
        snapshot = build_errors.history(self.store, self.pid)
        build_errors.record_operation(self.store, other, "Скачивание", "HTTP 503 other")
        build_errors.record_operation(self.store, self.pid, "Скачивание", "HTTP 503 new")

        result = build_errors.clear(self.store, self.pid, snapshot["lastId"])

        self.assertEqual(result["cleared"], 1)
        remaining = build_errors.history(self.store, self.pid)
        self.assertGreater(remaining["lastId"], snapshot["lastId"])
        self.assertEqual([event["evidence"] for event in remaining["groups"][0]["events"]], ["HTTP 503 new"])
        self.assertEqual(build_errors.history(self.store, other)["groups"][0]["events"][0]["evidence"], "HTTP 503 other")
        self.assertEqual(log.read_text(), "Minecraft log remains available")

    def test_clear_legacy_history_never_reuses_ids_after_reopen(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO build_errors VALUES(42,?,?,?,?,?,?,?)",
                       (self.pid, time.time(), "download", "Загрузка", "Повторить", "Скачивание", "HTTP 503 old"))
        snapshot = build_errors.history(self.store, self.pid)
        self.assertEqual(snapshot["lastId"], 42)
        self.assertEqual(build_errors.clear(self.store, self.pid, snapshot["lastId"])["cleared"], 1)
        self.assertEqual(build_errors.history(self.store, self.pid)["lastId"], 0)

        reopened = Store(self.store.root)
        build_errors.record_operation(reopened, self.pid, "Скачивание", "HTTP 503 new")
        self.assertGreater(build_errors.history(reopened, self.pid)["lastId"], 42)
        self.assertEqual(build_errors.clear(reopened, self.pid, snapshot["lastId"])["cleared"], 0)
        self.assertEqual(len(build_errors.history(reopened, self.pid)["groups"][0]["events"]), 1)

    def test_clear_requires_existing_profile_and_valid_snapshot(self):
        build_errors.record_operation(self.store, self.pid, "Скачивание", "HTTP 503")
        snapshot = build_errors.history(self.store, self.pid)
        with self.assertRaisesRegex(ValueError, "Сборка не найдена"):
            build_errors.clear(self.store, "missing", snapshot["lastId"])
        for invalid in (None, True, -1, "1", 1.5, 2**63):
            with self.subTest(through_id=invalid), self.assertRaises(ValueError):
                build_errors.clear(self.store, self.pid, invalid)
        self.assertEqual(build_errors.history(self.store, self.pid), snapshot)

    def test_only_current_crash_evidence_survives_following_launch(self):
        logs = self.store.folder(self.pid) / "logs"
        logs.mkdir()
        stale = logs / "latest.log"
        stale.write_text("OutOfMemoryError")
        os.utime(stale, (1, 1))
        stderr = logs / "launcher-stderr.log"
        stderr.write_text("java.lang.UnsupportedClassVersionError: Example is too new")
        build_errors.record_exit(self.store, self.pid, 1, time.time())
        stderr.write_text("successful next launch")
        row = build_errors.history(self.store, self.pid)["groups"][0]
        self.assertIn("Java", row["title"])
        self.assertIn("UnsupportedClassVersionError", row["events"][0]["evidence"])
        self.assertNotIn("OutOfMemoryError", str(row))

    def test_failed_job_visible_from_feature_api_clean_exit_creates_no_issue(self):
        from main import App
        app = App(self.store.root)
        self.addCleanup(app.close)
        def fail(status):
            status("Проверка дополнений")
            raise ValueError("Нет обязательной зависимости testlib")
        app.start_job(fail, profile=self.pid)
        deadline = time.monotonic() + 3
        while app.job_snapshot()["busy"] and time.monotonic() < deadline:
            time.sleep(.01)
        app._game_finished(0, "С друзьями", self.pid)
        entries = app.feature_action("buildErrors", {"id": self.pid}, read=True)["groups"]
        self.assertEqual(len(entries), 1)
        self.assertIn("testlib", entries[0]["events"][0]["evidence"])
        self.assertEqual(entries[0]["events"][0]["stage"], "Проверка дополнений")

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

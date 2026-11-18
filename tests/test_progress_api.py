"""Progress travels through the authenticated job endpoint, outside job locks."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from main import App, Handler, Server


class ProgressApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {"BLOCKHARBOR_BOOTSTRAP_PREPARED": "0"}):
            self.app = App(Path(self.temp.name) / "data")
        self.app.store.set("autoUpdate", "0")
        self.addCleanup(self.app.close)
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.opener = build_opener(ProxyHandler({}))

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, path="/api/job", *, authenticated=True):
        headers = {"Authorization": "Bearer " + self.app.token} if authenticated else {}
        request = Request(f"http://127.0.0.1:{self.server.server_port}" + path, headers=headers)
        with self.opener.open(request, timeout=5) as response:
            return json.load(response)

    def test_progress_snapshot_is_private_and_excludes_unrelated_transfer_history(self):
        tasks = [{"id": str(i), "category": "launcher-update", "state": "completed"} for i in range(10)]
        tasks += [{"id": "active", "category": "launcher-update", "state": "downloading", "bytes": 25, "total": 100},
                  {"id": "mod", "category": "mods", "state": "downloading"}]
        update = {
            "current": "2.3.0", "channel": "stable", "autoEnabled": True,
            "release": {"version": "2.4.0", "compatible": True, "download": "https://example.invalid/installer", "notes": "Long release notes"},
            "transfers": {"tasks": tasks},
            "checkStatus": {"source": "public", "message": "Официальный резервный источник", "retryAt": 1234},
            "startupResult": {"id": "completed-handoff", "ok": True, "message": "BlockHarbor обновлён."},
        }
        with patch.object(self.app.updates, "state", return_value=update):
            result = self.call()["updater"]
            self.assertEqual(result["release"], {"version": "2.4.0", "compatible": True})
            self.assertEqual(len(result["transfers"]["tasks"]), 7)
            self.assertEqual(result["transfers"]["tasks"][0]["id"], "active")
            self.assertEqual(result["checkStatus"], update["checkStatus"])
            self.assertEqual(result["startupResult"], update["startupResult"])
            with self.assertRaises(HTTPError) as error:
                self.call(authenticated=False)
            self.assertEqual(error.exception.code, 403)

    def test_updater_read_never_holds_jobs_lock_or_recurses_from_install_guard(self):
        def updater_state():
            self.assertFalse(self.app.jobs_lock.locked())
            # UpdateService calls this while holding its own lock. The internal
            # job read must not call back into the progress/update state again.
            self.assertTrue(self.app._can_update())
            return {"transfers": {"tasks": []}}

        with patch.object(self.app.updates, "state", side_effect=updater_state) as read:
            self.app.job_snapshot()
            read.assert_not_called()
            self.call()
            read.assert_called_once()

    def test_real_job_stage_and_final_error_are_delivered_alongside_update_progress(self):
        started, finish = threading.Event(), threading.Event()
        def work(status):
            status("Скачиваем Java: 50%")
            started.set()
            finish.wait(3)
            raise ValueError("Не удалось проверить загруженный файл Java.")

        self.addCleanup(finish.set)
        job = self.app.start_job(work)["job"]
        self.assertTrue(started.wait(2))
        busy = self.call()
        self.assertEqual(busy["id"], job)
        self.assertTrue(busy["busy"])
        self.assertEqual(busy["message"], "Скачиваем Java: 50%")
        self.assertIn("updater", busy)
        finish.set()
        # The owned worker may still be persisting notification history.
        deadline = threading.Event()
        for _ in range(100):
            result = self.call()
            if not result["busy"]:
                break
            deadline.wait(.02)
        self.assertFalse(result["busy"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Не удалось проверить загруженный файл Java.")

    def test_cold_bootstrap_result_is_consumed_from_environment_per_app(self):
        with patch.dict(os.environ, {"BLOCKHARBOR_BOOTSTRAP_PREPARED": "1"}):
            first = App(Path(self.temp.name) / "first")
            self.addCleanup(first.close)
            self.assertNotIn("BLOCKHARBOR_BOOTSTRAP_PREPARED", os.environ)
            result = first.ui_job_snapshot()["startupNotice"]
            self.assertTrue(result["ok"])
            self.assertEqual(first.ui_job_snapshot()["startupNotice"], result)
            second = App(Path(self.temp.name) / "second")
            self.addCleanup(second.close)
            self.assertIsNone(second.ui_job_snapshot()["startupNotice"])


if __name__ == "__main__":
    unittest.main()

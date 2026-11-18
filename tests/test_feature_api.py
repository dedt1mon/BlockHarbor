"""Exercise the actual authenticated HTTP boundary for the integrated features."""

import http.cookiejar
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from unittest.mock import Mock, patch

from main import App, Handler, Server


class FeatureApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = App(Path(temp.name))
        self.pid = self.app.store.create("Проверка API", "1.20.1", "vanilla")
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, path, data=None, *, auth=True, origin=None, raw=False):
        headers = {"Authorization":"Bearer " + self.app.token} if auth else {}
        if origin:
            headers["Origin"] = origin
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        with self.opener.open(Request(self.base + path, data=data, headers=headers), timeout=10) as response:
            contents = response.read()
            return contents if raw else json.loads(contents)

    def test_all_feature_reads_and_static_modules_are_accessible(self):
        for action in ("careState", "careWorlds", "experienceState", "experienceGallery", "experienceNotifications", "performanceState", "shareRoomState", "updateState", "diagnostics"):
            with self.subTest(action=action):
                result = self.call(f"/api/feature?action={action}&id={self.pid}")
                self.assertIsInstance(result, dict)
        state = self.call("/api/state")
        self.assertEqual(state["appVersion"], "2.4.8")
        for name in ("experience", "build-care", "pack-hub", "updates"):
            self.assertGreater(len(self.call("/" + name + ".js",auth=False,raw=True)), 100)

    def test_get_cannot_mutate_and_cross_origin_is_blocked(self):
        for path, auth, origin, expected in ((f"/api/feature?action=careRemove&id={self.pid}", True,None,400),("/api/feature?action=updateState",False,None,403),("/api/state",True,"https://other.example",403)):
            with self.assertRaises(HTTPError) as error:
                self.call(path,auth=auth,origin=origin)
            self.assertEqual(error.exception.code, expected)

    def test_mutations_are_blocked_during_game_but_cancel_controls_work(self):
        child = Mock()
        child.poll.return_value = None
        self.app.child, self.app.playing_id = child, self.pid
        with self.assertRaises(HTTPError) as error:
            self.call("/api/action", {"action":"careSnapshot","id":self.pid})
        self.assertIn("Закрой", error.exception.read().decode())
        self.app.job["busy"] = True
        self.assertIsInstance(self.call("/api/action", {"action":"transferClear"}), dict)
        self.app.job["busy"] = False
        self.app.child = None

    def test_mrpack_export_download_cookie_and_import_preview_roundtrip(self):
        ticket = self.call("/api/mrpack-export", {"id":self.pid})
        raw = self.call(ticket["download"], auth=False, raw=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = json.loads(archive.read("modrinth.index.json"))
            self.assertEqual(manifest["dependencies"]["minecraft"], "1.20.1")
        preview = self.call("/api/mrpack-import",raw)
        self.assertIn("token",preview)
        with self.assertRaises(HTTPError) as error:
            self.call(ticket["download"],auth=False,raw=True)
        self.assertEqual(error.exception.code,403)
        self.assertEqual(len(self.app.store.all()),1)  # preview never installs silently

    def test_auto_world_backup_is_deferred_outside_state_poll(self):
        self.app.store.set("careAutoWorldBackup:"+self.pid,"1")
        with patch.object(self.app.care,"backup_after_game",return_value="Копия готова") as backup:
            self.app._game_finished(0,"Мир",self.pid)
            self.assertEqual(self.app.pending_world_backups,{self.pid})
            backup.assert_not_called()
            with patch.object(self.app,"start_job",side_effect=lambda fn, **kwargs:fn(lambda _:None)):
                self.app.background_maintenance()
            backup.assert_called_once()

    def test_old_exit_code_is_not_attached_to_another_build(self):
        self.app.game_exit = {"profile":"other-profile", "code":17}
        result = self.call(f"/api/feature?action=diagnostics&id={self.pid}")
        self.assertIsNone(result["exitCode"])

    def test_deferred_world_backup_waits_if_a_game_has_relaunched(self):
        self.app.pending_world_backups.add(self.pid)
        with patch.object(self.app, "game_running", return_value=True), patch.object(self.app, "start_job") as start:
            self.app.background_maintenance()
        start.assert_not_called()
        self.assertIn(self.pid, self.app.pending_world_backups)


if __name__ == "__main__":
    unittest.main()

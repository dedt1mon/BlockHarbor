"""Exercise the actual authenticated HTTP boundary for the integrated features."""

import http.cookiejar
import io
import json
import re
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from unittest.mock import Mock, patch

from main import App, Handler, Server
from launcher.version import VERSION


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
        self.app.close()

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
        self.assertEqual(state["appVersion"], VERSION)
        index = self.call("/", auth=False, raw=True).decode("utf-8")
        assets = set(re.findall(r'(?:src|href)="(/[^\"]+\.(?:css|js))"', index))
        self.assertTrue(assets)
        for path in assets:
            with self.subTest(asset=path):
                self.assertGreater(len(self.call(path, auth=False, raw=True)), 100)

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

    def test_disabled_community_is_rejected_before_network_or_job_creation(self):
        self.assertIs(self.call("/api/state")["capabilities"]["communitySharing"], False)
        community = self.app.community
        with (patch.object(self.app, "start_job") as start,
              patch.object(community.api, "json") as registry,
              patch.object(community.api, "request") as download,
              patch.object(community.mirror, "search") as mirror):
            for action in sorted(community.ACTIONS):
                with self.subTest(action=action):
                    params = {"action": action, "id": self.pid, "build": "BH-A123456789BC"}
                    with self.assertRaises(HTTPError) as error:
                        self.call("/api/action", params)
                    self.assertEqual(error.exception.code, 400)
                    self.assertIn("временно отключена", error.exception.read().decode("utf-8"))
                    if action in community.READ_ACTIONS:
                        with self.assertRaises(HTTPError) as read_error:
                            self.call(f"/api/feature?action={action}&id={self.pid}")
                        self.assertEqual(read_error.exception.code, 400)
                        self.assertIn("временно отключена", read_error.exception.read().decode("utf-8"))
                    with self.assertRaisesRegex(ValueError, "временно отключена"):
                        community.handle(action, params)
            start.assert_not_called()
            registry.assert_not_called()
            download.assert_not_called()
            mirror.assert_not_called()
        self.assertFalse(self.app.job_snapshot()["busy"])

    def test_disabled_publication_poll_keeps_existing_owner_records_without_server_calls(self):
        community = self.app.community
        listing = {"id": "BH-A123456789BC", "name": "Старая публикация", "minecraft": "1.20.1",
                   "loader": "vanilla", "category": "adventure", "size": 100,
                   "sha256": "a" * 64, "expiresAt": 1}
        with self.app.store.connection() as db:
            db.execute("INSERT INTO community_uploads VALUES(?,?,?,?,?,?)",
                       (community.api.origin, self.pid, "existing", "a" * 32, "b" * 64, json.dumps(listing)))
            community._remember_receipt(db, listing, "b" * 64)
            uploads = [tuple(row) for row in db.execute("SELECT * FROM community_uploads")]
            receipts = [tuple(row) for row in db.execute("SELECT * FROM community_receipts")]
        with (patch.object(community.api, "json") as registry,
              patch.object(self.app.updates, "tick_auto"),
              patch.object(self.app.updates, "monitor_pending"),
              patch.object(self.app.experience, "notify") as notify,
              patch("launcher.community_catalog.threading.Thread") as thread):
            self.app.background_maintenance()
            community.tick_notifications(notify)
            self.assertEqual(community.sync_notifications(notify), 0)
            registry.assert_not_called()
            thread.assert_not_called()
            notify.assert_not_called()
        with self.app.store.connection() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM community_uploads")], uploads)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM community_receipts")], receipts)

    def test_community_pause_keeps_zip_roundtrip_and_modrinth_catalog_available(self):
        self.assertIs(self.call("/api/state")["capabilities"]["communitySharing"], False)
        folder = self.app.store.folder(self.pid)
        (folder / "mods/example.jar").write_bytes(b"example mod")
        (folder / "config").mkdir(exist_ok=True)
        (folder / "config/example.json").write_text('{"enabled":true}')
        world = folder / "saves/private-world/level.dat"
        world.parent.mkdir(parents=True)
        world.write_bytes(b"private world")
        ticket = self.call("/api/build-export", {"id": self.pid})
        raw = self.call(ticket["download"], auth=False, raw=True)
        imported = self.call("/api/build-import", raw)
        copied = self.app.store.folder(imported["id"])
        self.assertEqual((copied / "mods/example.jar").read_bytes(), b"example mod")
        self.assertEqual((copied / "config/example.json").read_text(), '{"enabled":true}')
        self.assertFalse((copied / "saves/private-world/level.dat").exists())
        with patch("launcher.pack_hub.modrinth._get", return_value={"hits": [], "total_hits": 0}) as search:
            result = self.call("/api/feature?action=packSearch&q=adventure")
        self.assertEqual(result["hits"], [])
        search.assert_called_once()
        self.assertEqual(search.call_args.args, ("/search",))
        self.assertEqual(search.call_args.kwargs["query"], "adventure")

    def test_auto_world_backup_is_deferred_outside_state_poll(self):
        self.app.store.set("careAutoWorldBackup:"+self.pid,"0")
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

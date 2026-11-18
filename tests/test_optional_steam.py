"""Steam compatibility stays separate from Minecraft installation and launch."""

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from launcher import build_archive, e4steam, game, pack_hub, room_bridge, skin_sync
from main import App, Handler, Server
from tests.test_build_archive import sample_zip
from tests.test_pack_hub import file_row, pack_file


class OptionalSteamPlanTests(unittest.TestCase):
    def test_unsupported_preparation_never_touches_files_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "not-created"
            with patch.object(e4steam, "ensure", side_effect=AssertionError("must not prepare Steam")):
                self.assertEqual(e4steam.ensure_if_supported(folder, "1.16.5", "fabric"), [])
            self.assertFalse(folder.exists())
        info = e4steam.compatibility("1.16.5", "fabric")
        self.assertFalse(info["supported"])
        self.assertTrue(info["canContinue"])
        self.assertEqual(info["reason"], "unsupported")

    def test_incomplete_catalog_is_unavailable_without_guessing_new_files(self):
        catalog = copy.deepcopy(e4steam._CATALOG)
        catalog["files"].pop("core/fabric-quilt/1.19-1.21.11")
        e4steam._plan.cache_clear()
        try:
            with patch.object(e4steam, "_CATALOG", catalog):
                self.assertFalse(e4steam.support("1.20.1", "fabric"))
                self.assertTrue(e4steam.support("1.20.1", "forge"))
        finally:
            e4steam._plan.cache_clear()
        self.assertTrue(e4steam.support("1.20.1", "fabric"))
        self.assertFalse(e4steam.support("1.20.99", "forge"))

    def test_supported_preparation_keeps_transport_and_integrity_errors(self):
        for error in (ValueError("checksum mismatch"), URLError("network unavailable")):
            with self.subTest(error=error), patch.object(e4steam, "ensure", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    e4steam.ensure_if_supported(Path("unused"), "1.20.1", "fabric")
                self.assertIs(caught.exception, error)

    def test_catalog_preflight_is_conservative_for_mixed_or_missing_metadata(self):
        with patch.object(pack_hub, "download_verified", side_effect=AssertionError("no archive download")):
            supported = pack_hub.steam_variants(["1.20.1"], ["forge", "fabric"])
            self.assertTrue(supported["supported"])
            self.assertFalse(supported["unknown"])
            mixed = pack_hub.steam_variants(["1.16.5", "1.20.1"], ["fabric"])
            self.assertFalse(mixed["supported"])
            self.assertFalse(mixed["unknown"])
            self.assertEqual([row["supported"] for row in mixed["variants"]], [False, True])
            for versions, loaders in ((None, ["fabric"]), (["1.20.1"], []), (["1.20.1"], ["unknown"]), ([None], ["forge"])):
                with self.subTest(versions=versions, loaders=loaders):
                    info = pack_hub.steam_variants(versions, loaders)
                    self.assertTrue(info["unknown"])
                    self.assertFalse(info["supported"])


class OptionalSteamAppTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = App(self.root)
        self.addCleanup(self.app.close)

    def finish(self, response):
        if "job" not in response:
            return response
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.app.job_snapshot()
            if result["id"] == response["job"] and not result["busy"]:
                return result
            time.sleep(0.01)
        self.fail("Preparation job did not finish")

    def test_unsupported_create_and_play_keep_selected_version_and_user_files(self):
        pairs = [("1.16.5", "fabric"), ("1.7.10", "forge"), ("26.4", "neoforge"), ("26w02a", "vanilla")]
        with (
            patch.object(e4steam, "ensure", side_effect=AssertionError("unsupported Steam must not be installed")),
            patch.object(room_bridge, "prepare", side_effect=AssertionError("unsupported bridge must not be installed")),
            patch.object(game, "install") as install,
            patch.object(game, "launch_local") as launch,
        ):
            for version, loader in pairs:
                with self.subTest(version=version, loader=loader):
                    created = self.app.action("create", {"name":version,"version":version,"loader":loader})
                    self.assertTrue(created["ok"])
                    pid = self.app.store.get("active")
                    folder = self.app.store.folder(pid)
                    own_mod = folder / "mods/player-file.jar"
                    own_mod.write_bytes(b"keep user file")
                    child = Mock()
                    child.poll.return_value = None
                    launch.return_value = child
                    result = self.finish(self.app.action("play", {"id":pid}))
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(install.call_args.args[:4], (version, loader, self.root / "game-cache", folder))
                    self.assertEqual(own_mod.read_bytes(), b"keep user file")
                    self.assertEqual([p.name for p in (folder / "mods").iterdir()], [own_mod.name])
                    self.assertEqual(self.app.store.profile(pid)["version"], version)
                    self.assertEqual(self.app.store.profile(pid)["loader"], loader)
                    self.assertEqual(self.app.store.get("steamNotice:" + pid), "")
                    child.poll.return_value = 0
                    self.app.job_snapshot()

    def test_notice_receipt_is_per_build_and_exact_version_loader_and_survives_restart(self):
        self.app.action("create", {"name":"Old Fabric","version":"1.16.5","loader":"fabric","allowWithoutSteam":True})
        pid = self.app.store.get("active")
        self.app.action("create", {"name":"Other","version":"1.16.5","loader":"fabric"})
        other = self.app.store.get("active")
        state = {p["id"]:p for p in self.app.state()["profiles"]}
        self.assertTrue(state[pid]["steamNoticeAcknowledged"])
        self.assertFalse(state[other]["steamNoticeAcknowledged"])
        reopened = App(self.root)
        self.addCleanup(reopened.close)
        self.assertTrue(next(p for p in reopened.state()["profiles"] if p["id"] == pid)["steamNoticeAcknowledged"])
        with self.app.store.connection() as db:
            db.execute("UPDATE profiles SET version='1.15.2' WHERE id=?", (pid,))
        self.assertFalse(next(p for p in self.app.state()["profiles"] if p["id"] == pid)["steamNoticeAcknowledged"])

    def test_play_records_consent_before_download_even_if_download_later_fails(self):
        pid = self.app.store.create("Old Forge", "1.7.10", "forge")
        def install(*args, **kwargs):
            self.assertEqual(self.app.store.get("steamNotice:" + pid), "1.7.10|forge")
            raise ValueError("Minecraft download failed")
        with patch.object(game, "install", side_effect=install), patch.object(game, "launch_local") as launch:
            result = self.finish(self.app.action("play", {"id":pid,"allowWithoutSteam":True}))
        self.assertFalse(result["ok"])
        self.assertIn("Minecraft download failed", result["message"])
        launch.assert_not_called()

    def test_invalid_acknowledgement_does_not_create_or_start_any_job(self):
        pid = self.app.store.create("Existing", "1.7.10", "forge")
        before = self.app.store.all()
        with patch.object(self.app, "start_job", side_effect=AssertionError("invalid acknowledgement must not start work")):
            for flag in (None, 0, 1, "true", [], {}):
                with self.subTest(flag=flag):
                    with self.assertRaises(ValueError):
                        self.app.action("create", {"name":"Old","version":"1.7.10","loader":"forge","allowWithoutSteam":flag})
                    with self.assertRaises(ValueError):
                        self.app.action("play", {"id":pid,"allowWithoutSteam":flag})
                    self.assertEqual(self.app.store.all(), before)

    def test_supported_create_and_play_still_prepare_steam_and_do_not_record_opt_out(self):
        child = Mock()
        child.poll.return_value = None
        with (
            patch.object(e4steam, "ensure", return_value=[Path("verified.jar")]) as ensure,
            patch.object(room_bridge, "prepare") as bridge,
            patch.object(skin_sync, "prepare"),
            patch.object(game, "install"),
            patch.object(game, "launch_local", return_value=child),
        ):
            created = self.finish(self.app.action("create", {"name":"Supported","version":"1.20.1","loader":"fabric","allowWithoutSteam":True}))
            self.assertTrue(created["ok"], created)
            pid = self.app.store.get("active")
            played = self.finish(self.app.action("play", {"id":pid,"allowWithoutSteam":True}))
            self.assertTrue(played["ok"], played)
            self.assertEqual(ensure.call_count, 2)
            self.assertEqual(ensure.call_args.args[1:3], ("1.20.1", "fabric"))
            bridge.assert_called_once_with(self.app.store.folder(pid), "1.20.1", "fabric")
            self.assertEqual(self.app.store.get("steamNotice:" + pid), "")

    def test_unsupported_zip_and_mrpack_import_keep_version_and_complete_downloads(self):
        with patch.object(e4steam, "ensure", side_effect=AssertionError("imports do not force Steam")):
            zipped = sample_zip([("mods/manual.jar", b"manual")], minecraft="1.7.10", loader="forge")
            imported = build_archive.import_build(self.app.store, zipped)
            self.assertEqual(self.app.store.profile(imported["id"])["version"], "1.7.10")
            self.assertEqual((self.app.store.folder(imported["id"]) / "mods/manual.jar").read_bytes(), b"manual")
            self.assertEqual(self.app.store.get("steamNotice:" + imported["id"]), "")
            prepared = self.app.packs.stage_import(pack_file([file_row()], dependencies={"minecraft":"1.16.5","fabric-loader":"0.16.0"}))
            self.assertFalse(prepared["steam"]["supported"])
            def download(url, target, hashes, size, **kwargs):
                target.write_bytes(b"download")
                return target
            with patch.object(pack_hub, "download_verified", side_effect=download) as transfer:
                result = self.app.packs.install_token(prepared["token"], allow_without_steam=True)
            transfer.assert_called_once()
            profile = self.app.store.profile(result["id"])
            self.assertEqual((profile["version"],profile["loader"]), ("1.16.5","fabric"))
            self.assertEqual(self.app.store.get("steamNotice:" + profile["id"]), "1.16.5|fabric")
            self.assertEqual((self.app.store.folder(profile["id"]) / "mods/example.jar").read_bytes(), b"download")

    def test_failed_pack_download_does_not_create_build_or_receipt(self):
        prepared = self.app.packs.stage_import(pack_file([file_row()], dependencies={"minecraft":"1.7.10","forge":"10.13.4.1614"}))
        with patch.object(pack_hub, "download_verified", side_effect=ValueError("checksum mismatch")):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.app.packs.install_token(prepared["token"], allow_without_steam=True)
        self.assertEqual(self.app.store.all(), [])
        self.assertFalse(any(key.startswith("steamNotice:") for key in self.app.store.settings()))

    def test_catalog_versions_warn_about_missing_metadata_before_archive_download(self):
        file = file_row()
        row = {"id":"Release","project_id":"Project","game_versions":None,"loaders":None,
               "files":[{"filename":"pack.mrpack","url":file["downloads"][0],"hashes":file["hashes"],"size":file["fileSize"]}]}
        with (
            patch.object(pack_hub.modrinth, "_get", side_effect=[{"id":"Project","project_type":"modpack"}, [row]]),
            patch.object(pack_hub, "download_verified", side_effect=AssertionError("release listing must not download the archive")),
        ):
            result = self.app.packs.versions("Project")
        release = result["versions"][0]
        self.assertEqual(release["minecraft"], [])
        self.assertEqual(release["loaders"], [])
        self.assertEqual(release["steam"], {"supported":False,"unknown":True,"variants":[]})
        self.assertEqual(self.app.store.all(), [])

    def test_compatibility_http_preflight_is_read_only_and_uses_actual_build_pair(self):
        pid = self.app.store.create("Old", "1.16.5", "fabric")
        e4steam.acknowledge_notice(self.app.store, pid, "1.16.5", "fabric")
        server = Server(("127.0.0.1", 0), Handler)
        server.app = self.app
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close_server():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.addCleanup(close_server)
        opener = build_opener(ProxyHandler({}))
        def get(params, authenticated=True):
            request = Request(f"http://127.0.0.1:{server.server_port}/api/steam/compatibility?" + urlencode(params),
                              headers={"Authorization":"Bearer " + self.app.token} if authenticated else {})
            with opener.open(request, timeout=3) as response:
                return json.load(response)
        before = self.app.store.settings()
        with (
            patch.object(e4steam, "urlopen", side_effect=AssertionError("preflight must not download")),
            patch.object(e4steam, "ensure", side_effect=AssertionError("preflight must not prepare")),
            patch.object(self.app, "start_job", side_effect=AssertionError("preflight must not start a job")),
        ):
            info = get({"version":"1.7.10","loader":"forge"})
            self.assertTrue(info["canContinue"])
            self.assertFalse(info["supported"])
            self.assertFalse(info["acknowledged"])
            exact = get({"id":pid,"version":"1.20.1","loader":"forge"})
            self.assertEqual((exact["version"],exact["loader"],exact["id"]), ("1.16.5","fabric",pid))
            self.assertTrue(exact["acknowledged"])
            self.assertTrue(get({"version":"1.20.1","loader":"fabric"})["supported"])
            for params in ({"version":"1.7.10"}, {"version":"1.7.10","loader":"missing"}, {"id":"not-a-profile"}):
                with self.subTest(params=params), self.assertRaises(HTTPError) as error:
                    get(params)
                self.assertEqual(error.exception.code, 400)
            with self.assertRaises(HTTPError) as error:
                get({"id":pid}, authenticated=False)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.app.store.settings(), before)


if __name__ == "__main__":
    unittest.main()

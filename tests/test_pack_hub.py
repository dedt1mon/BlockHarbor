import hashlib
import io
import json
import tempfile
import threading
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import pack_hub as packs
from launcher.store import Store


def file_row(path="mods/example.jar", content=b"download", **extra):
    return {"path": path, "hashes": {kind: hashlib.new(kind, content).hexdigest() for kind in ("sha1", "sha512")},
            "fileSize": len(content), "downloads": ["https://cdn.modrinth.com/data/Example/versions/Version/example.jar"], **extra}


def pack_file(files=(), overrides=(), **extra):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(packs.INDEX, json.dumps({"formatVersion": 1, "game": "minecraft", "versionId": "1.0", "name": "Пак",
                                               "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.0"}, "files": list(files), **extra}))
        for name, content in overrides:
            archive.writestr(name, content)
    stream.seek(0)
    return stream


class PackHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.hub = packs.PackHub(self.store)

    def download(self, url, target, hashes, size, **kwargs):
        content = b"download"
        self.assertEqual(size, len(content))
        self.assertEqual(hashes["sha512"], hashlib.sha512(content).hexdigest())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def test_empty_install_remains_empty_until_user_selects_pack(self):
        self.assertEqual(self.store.all(), [])
        prepared = self.hub.stage_import(pack_file())
        self.assertEqual(self.store.all(), [])
        result = self.hub.install_token(prepared["token"])
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(result["id"], self.store.get("active"))

    def test_optional_files_client_overrides_and_loader_pin(self):
        files = [file_row(), file_row("mods/optional.jar", env={"client": "optional", "server": "unsupported"}),
                 file_row("mods/server.jar", env={"client": "unsupported", "server": "required"})]
        overrides = [("overrides/config/demo.json", b"common"), ("client-overrides/config/demo.json", b"client"),
                     ("server-overrides/config/demo.json", b"server")]
        prepared = self.hub.stage_import(pack_file(files, overrides))
        self.assertEqual(prepared["unsupported"], 1)
        self.assertEqual(len(prepared["optional"]), 1)
        with patch.object(packs, "download_verified", side_effect=self.download) as download:
            result = self.hub.install_token(prepared["token"], optional=["mods/optional.jar"])
        folder = self.store.folder(result["id"])
        self.assertEqual(download.call_count, 2)
        self.assertEqual((folder / "config/demo.json").read_bytes(), b"client")
        self.assertFalse((folder / "mods/server.jar").exists())
        self.assertEqual(json.loads((folder / packs.build_archive.STAMP).read_text())["loader_version"], "0.16.0")
        self.assertFalse(any(self.hub.staging.glob("preview-*")))

    def test_optional_not_selected_by_default(self):
        prepared = self.hub.stage_import(pack_file([file_row(env={"client": "optional", "server": "optional"})]))
        with patch.object(packs, "download_verified") as download:
            result = self.hub.install_token(prepared["token"])
        download.assert_not_called()
        self.assertFalse((self.store.folder(result["id"]) / "mods/example.jar").exists())

    def test_failed_download_never_publishes_or_changes_active(self):
        old = self.store.create("Старая", "1.20.1", "vanilla")
        before = set(self.store.instances.iterdir())
        prepared = self.hub.stage_import(pack_file([file_row()]))
        with patch.object(packs, "download_verified", side_effect=ValueError("checksum")), self.assertRaisesRegex(ValueError, "checksum"):
            self.hub.install_token(prepared["token"])
        self.assertEqual(self.store.get("active"), old)
        self.assertEqual(set(self.store.instances.iterdir()), before)
        self.assertEqual(self.hub.preview(prepared["token"])["name"], "Пак")

    def test_download_checks_both_hashes_and_size_before_atomic_publication(self):
        target = self.store.root / "target.jar"
        target.write_bytes(b"existing")
        row = file_row(content=b"actual")
        with patch.object(packs.modrinth, "_open", return_value=io.BytesIO(b"actual")):
            packs.download_verified(row["downloads"][0], target, row["hashes"], row["fileSize"])
        self.assertEqual(target.read_bytes(), b"actual")
        for data, hashes, size in [(b"actual-extra", row["hashes"], row["fileSize"]), (b"short", row["hashes"], row["fileSize"]),
                                   (b"actual", {**row["hashes"], "sha1": "0" * 40}, row["fileSize"])]:
            with self.subTest(data=data), patch.object(packs.modrinth, "_open", return_value=io.BytesIO(data)), self.assertRaises(ValueError):
                packs.download_verified(row["downloads"][0], target, hashes, size)
            self.assertEqual(target.read_bytes(), b"actual")
        self.assertFalse(list(self.store.root.glob("*.part-*")))

    def test_path_traversal_aliases_symlinks_and_file_directory_conflicts(self):
        link = zipfile.ZipInfo("overrides/config/link.cfg")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        cases = [pack_file([file_row("../outside.jar")]), pack_file([file_row("mods\\outside.jar")]),
                 pack_file([file_row("C:/evil.jar")]), pack_file([file_row("mods/CON.jar")]),
                 pack_file([file_row("mods/A.jar"), file_row("mods/a.jar")]),
                 pack_file([], [(link, b"outside")]), pack_file([], [("overrides/config/foo", b"a"), ("client-overrides/config/foo/bar", b"b")]),
                 pack_file([], [("overrides/config/A/a.json", b"a"), ("client-overrides/config/a/b.json", b"b")])]
        for source in cases:
            with self.subTest(), self.assertRaises(ValueError):
                self.hub.stage_import(source)
        self.assertEqual(self.store.all(), [])
        self.assertFalse(list(self.hub.staging.glob("preview-*")))

    def test_duplicates_unknown_dependency_and_invalid_hashes_rejected(self):
        cases = [pack_file([file_row(hashes={"sha512": "a" * 128})]),
                 pack_file(dependencies={"minecraft": "1.20.1", "future-runtime": "2"}),
                 pack_file(dependencies={"minecraft": "1.20.1", "forge": "47.1", "fabric-loader": "0.16.0"}),
                 pack_file([file_row(env={"client": "maybe"})]), pack_file([file_row(fileSize=-1)])]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            duplicate = pack_file([], [(packs.INDEX, b"{}")])
        cases.append(duplicate)
        for source in cases:
            with self.subTest(), self.assertRaises(ValueError):
                self.hub.stage_import(source)

    def test_url_validation_does_not_accept_credentials_ports_http_or_localhost(self):
        for url in ("http://cdn.modrinth.com/a", "https://localhost/a", "https://cdn.modrinth.com.evil/a",
                    "https://user:pass@cdn.modrinth.com/a", "https://cdn.modrinth.com:443/a", "https://cdn.modrinth.com/a b"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.hub.stage_import(pack_file([file_row(downloads=[url])]))

    def test_archive_changed_after_preview_and_unlisted_optional_rejected(self):
        prepared = self.hub.stage_import(pack_file())
        with self.assertRaises(ValueError):
            self.hub.install_token(prepared["token"], optional=["mods/unlisted.jar"])
        self.hub.previews[prepared["token"]]["path"].write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.hub.install_token(prepared["token"])

    def test_export_round_trip_excludes_private_settings_and_worlds(self):
        pid = self.store.create("Моя сборка", "1.20.1", "vanilla")
        folder = self.store.folder(pid)
        (folder / "resourcepacks/grass.zip").write_bytes(b"pack")
        (folder / "config").mkdir()
        (folder / "config/render.json").write_text('{"fog":true}')
        (folder / "config/accounts.json").write_text('{"password":"private"}')
        (folder / "saves/World").mkdir(parents=True)
        (folder / "saves/World/level.dat").write_bytes(b"world")
        stream = io.BytesIO()
        with patch.object(packs.modrinth, "_recognized", side_effect=ValueError("offline")):
            filename = self.hub.export_pack(pid, stream)
        self.assertTrue(filename.endswith(".mrpack"))
        with zipfile.ZipFile(stream) as archive:
            self.assertEqual(set(archive.namelist()), {packs.INDEX, "overrides/resourcepacks/grass.zip", "overrides/config/render.json"})
        prepared = self.hub.stage_import(stream)
        result = self.hub.install_token(prepared["token"])
        copied = self.store.folder(result["id"])
        self.assertEqual((copied / "resourcepacks/grass.zip").read_bytes(), b"pack")
        self.assertFalse((copied / "saves").exists())

    def test_transfer_adapter_passes_whitelist_hashes_and_real_bytes(self):
        class Transfers:
            def download(inner, url, destination, **kwargs):
                self.assertEqual(kwargs["hosts"], packs.DOWNLOAD_HOSTS)
                self.assertEqual(kwargs["size"], 8)
                self.assertEqual(set(kwargs["hashes"]), {"sha1", "sha512"})
                destination.write_bytes(b"download")
                return destination
        row = file_row()
        target = self.store.root / "test.jar"
        packs.download_verified(row["downloads"][0], target, row["hashes"], 8, transfers=Transfers())
        self.assertEqual(target.read_bytes(), b"download")

    def test_discard_during_download_does_not_block_or_delete_live_archive(self):
        prepared = self.hub.stage_import(pack_file([file_row()]))
        token = prepared["token"]
        entered, release = threading.Event(), threading.Event()
        outcome = []
        def download(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Discard waited on the network lock")
            return self.download(*args, **kwargs)
        def install():
            try:
                outcome.append(self.hub.install_token(token))
            except BaseException as error:
                outcome.append(error)
        with patch.object(packs, "download_verified", side_effect=download):
            worker = threading.Thread(target=install)
            worker.start()
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.hub.preview(token)["name"], "Пак")
            self.hub.discard(token)
            self.assertTrue(self.hub.previews[token]["path"].exists())
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome[0], dict)
        self.assertNotIn(token, self.hub.previews)


if __name__ == "__main__":
    unittest.main()

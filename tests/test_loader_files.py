import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import build_archive, loader_files, preparation
from launcher.store import Store


def digest(data):
    return {"size": len(data), "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest()}


def library(name, data):
    parts, _, extension = name.partition("@")
    group, artifact, version = parts.split(":")
    path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.{extension or 'jar'}"
    return {"name": name, "downloads": {"artifact": {
        "path": path, "url": preparation._NEO_ORIGIN + path,
        "sha1": digest(data)["sha1"], "size": len(data)}}}


def rewrite_zip(source, mutate):
    with zipfile.ZipFile(source) as zipped:
        entries = {name: zipped.read(name) for name in zipped.namelist()}
    mutate(entries)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as zipped:
        for name, data in entries.items():
            zipped.writestr(name, data)
    result.seek(0)
    return result


class LoaderFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = Store(self.root / "sender")
        self.recipient = Store(self.root / "recipient")
        self.pid = self.source.create("Сборка друзей", "1.21.1", "neoforge")
        self.instance = self.source.folder(self.pid)
        self.cache = self.source.root / "game-cache"
        self.pin = "21.1.267"
        self.url = loader_files.installer_url(self.pin)
        self.jar_data, self.zip_data = b"verified loader library", b"verified NeoForm data"
        self.libraries = [library("org.example:loader:1.0", self.jar_data),
                          library("net.neoforged:neoform:1.0@zip", self.zip_data)]
        installer = io.BytesIO()
        with zipfile.ZipFile(installer, "w") as zipped:
            zipped.writestr("install_profile.json", json.dumps({
                "spec": 1, "minecraft": "1.21.1", "version": "neoforge-" + self.pin,
                "json": "/version.json", "libraries": self.libraries}))
            zipped.writestr("version.json", json.dumps({
                "id": "neoforge-" + self.pin, "inheritsFrom": "1.21.1", "libraries": self.libraries}))
        self.installer_data = installer.getvalue()
        self.trust = patch.dict(preparation._NEO_INSTALLER_SHA1, {
            preparation._neo_path(self.url): digest(self.installer_data)["sha1"]})
        self.trust.start()
        self.addCleanup(self.trust.stop)
        installer_path, record = loader_files._installer_paths(self.cache, self.url)
        installer_path.parent.mkdir(parents=True)
        installer_path.write_bytes(self.installer_data)
        preparation.Download._remember_installer_digest(record, self.url, digest(self.installer_data)["sha1"])
        for entry, data in zip(self.libraries, (self.jar_data, self.zip_data)):
            path = self.cache / "libraries" / entry["downloads"]["artifact"]["path"]
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
        (self.instance / build_archive.STAMP).write_text(json.dumps({
            "version": "1.21.1", "loader": "neoforge", "loader_version": self.pin,
            "id": "neoforge-" + self.pin, "cache": str(self.cache)}))
        (self.instance / "mods" / "example.jar").write_bytes(b"mod")

    def export(self):
        output = io.BytesIO()
        build_archive.export_build(self.source, self.pid, output, include_loader_files=True)
        return output

    def imported(self, payload=None):
        result = build_archive.import_build(self.recipient, payload or self.export())
        return self.recipient.folder(result["id"])

    def apply(self, instance):
        return loader_files.apply(instance, self.recipient.root / "game-cache", "1.21.1", "neoforge", self.pin)

    def test_round_trip_transfers_inputs_without_trusting_or_copying_runtime_state(self):
        # None of these completed/runtime/private files belongs in a transferable input bundle.
        forbidden = {"versions/neoforge-21.1.267/neoforge-21.1.267.json": b"private profile",
                     "versions/neoforge-21.1.267/.blockharbor-loader-ready.json": b"receipt",
                     "versions/1.21.1/1.21.1.jar": b"Minecraft", "runtime/java/bin/java.exe": b"java",
                     "libraries/net/minecraft/client/1.21.1/client-1.21.1-patched.jar": b"patched",
                     "launcher_profiles.json": b"account"}
        for name, data in forbidden.items():
            path = self.cache / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        payload = self.export()
        with zipfile.ZipFile(payload) as zipped:
            manifest = json.loads(zipped.read(build_archive.MANIFEST))
            self.assertEqual(manifest["formatVersion"], 2)
            self.assertTrue(manifest["loaderFiles"])
            bundle = json.loads(zipped.read("loader-files/manifest.json"))
            self.assertEqual(len(bundle["libraries"]), 2)
            self.assertNotIn(str(self.source.root).encode(), zipped.read("loader-files/manifest.json"))
        folder = self.imported(payload)
        target_cache = self.recipient.root / "game-cache"
        self.assertFalse(target_cache.exists())
        self.assertEqual(json.loads((folder / build_archive.STAMP).read_text()),
                         {"version": "1.21.1", "loader": "neoforge", "loader_version": self.pin})
        with patch.object(preparation, "_installer_checksum", side_effect=AssertionError("Network forbidden")):
            self.assertEqual(self.apply(folder), 3)
            self.assertEqual(self.apply(folder), 0)
        installer, record = loader_files._installer_paths(target_cache, self.url)
        self.assertEqual(installer.read_bytes(), self.installer_data)
        self.assertEqual(preparation.Download._cached_installer_digest(record, self.url), digest(self.installer_data)["sha1"])
        for entry, data in zip(self.libraries, (self.jar_data, self.zip_data)):
            self.assertEqual((target_cache / "libraries" / entry["downloads"]["artifact"]["path"]).read_bytes(), data)
        for name in forbidden:
            self.assertFalse((target_cache / name).exists(), name)

    def test_export_without_cached_installer_stays_ordinary_compatible_zip(self):
        loader_files._installer_paths(self.cache, self.url)[0].unlink()
        payload = self.export()
        with zipfile.ZipFile(payload) as zipped:
            manifest = json.loads(zipped.read(build_archive.MANIFEST))
            self.assertEqual(manifest["formatVersion"], 1)
            self.assertNotIn("loaderFiles", manifest)
            self.assertEqual(set(zipped.namelist()), {build_archive.MANIFEST, "mods/example.jar"})

    def test_corrupted_cached_installer_does_not_get_exported(self):
        loader_files._installer_paths(self.cache, self.url)[0].write_bytes(b"invalid installer")
        with zipfile.ZipFile(self.export()) as zipped:
            self.assertFalse(any(name.startswith("loader-files/") for name in zipped.namelist()))

    def test_old_completed_build_warms_exact_installer_once_before_export(self):
        installer, record = loader_files._installer_paths(self.cache, self.url)
        installer.unlink()
        record.unlink()
        installed = "neoforge-" + self.pin
        profile = self.cache / "versions" / installed / (installed + ".json")
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"id": installed, "inheritsFrom": "1.21.1"}))
        fetched = []
        def receive(url, output, limit, notify, **kwargs):
            fetched.append(url)
            output.write(self.installer_data)
            notify(len(self.installer_data), len(self.installer_data))
        with (patch.object(preparation, "_receive", receive),
              patch.object(preparation, "_receive_requests", side_effect=AssertionError("Primary succeeds"))):
            self.assertTrue(loader_files.prepare_export(self.source, self.pid))
            self.assertTrue(loader_files.prepare_export(self.source, self.pid))
        self.assertEqual(fetched, [self.url])
        self.assertEqual(installer.read_bytes(), self.installer_data)
        self.assertFalse(list(self.cache.glob(".blockharbor-export-*")))
        folder = self.imported()
        with patch.object(preparation, "_installer_checksum", side_effect=AssertionError("No network")):
            self.assertEqual(self.apply(folder), 3)

    def test_export_preparation_does_not_fetch_for_imported_pin_or_change_it(self):
        installer, _ = loader_files._installer_paths(self.cache, self.url)
        installer.unlink()
        (self.instance / build_archive.STAMP).write_text(json.dumps({
            "version": "1.21.1", "loader": "neoforge", "loader_version": self.pin}))
        with patch.object(preparation.Download, "__call__", side_effect=AssertionError("Unprepared build")):
            self.assertFalse(loader_files.prepare_export(self.source, self.pid))
        self.assertEqual(json.loads((self.instance / build_archive.STAMP).read_text())["loader_version"], self.pin)

    def test_optional_export_preparation_failure_keeps_ordinary_zip_available(self):
        installer, _ = loader_files._installer_paths(self.cache, self.url)
        installer.unlink()
        installed = "neoforge-" + self.pin
        profile = self.cache / "versions" / installed / (installed + ".json")
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"id": installed, "inheritsFrom": "1.21.1"}))
        messages = []
        with patch.object(preparation.Download, "__call__", side_effect=preparation.DownloadError("offline")):
            self.assertFalse(loader_files.prepare_export(self.source, self.pid, messages.append))
        self.assertIn("обычный ZIP", messages[-1])
        with zipfile.ZipFile(self.export()) as zipped:
            self.assertEqual(json.loads(zipped.read(build_archive.MANIFEST))["formatVersion"], 1)

    def test_archive_damage_rolls_back_new_instance(self):
        payload = self.export()
        def damage(entries):
            key = "loader-files/objects/" + digest(self.jar_data)["sha256"]
            entries[key] = bytes([entries[key][0] ^ 1]) + entries[key][1:]
        payload = rewrite_zip(payload, damage)
        before = self.recipient.all()
        with self.assertRaisesRegex(ValueError, "повреждён"):
            self.imported(payload)
        self.assertEqual(self.recipient.all(), before)
        self.assertEqual(list(self.recipient.instances.iterdir()), [])
        self.assertFalse((self.recipient.root / "game-cache").exists())

    def test_archive_hashes_cannot_authorize_a_different_installer(self):
        def replace(entries):
            meta = json.loads(entries["loader-files/manifest.json"])
            previous = meta["installer"]["sha256"]
            changed = self.installer_data + b"untrusted replacement"
            meta["installer"] = digest(changed)
            del entries["loader-files/objects/" + previous]
            entries["loader-files/objects/" + meta["installer"]["sha256"]] = changed
            entries["loader-files/manifest.json"] = json.dumps(meta).encode()
        folder = self.imported(rewrite_zip(self.export(), replace))
        with patch.object(preparation, "_installer_checksum", side_effect=AssertionError("Known pin must be local")):
            self.assertEqual(self.apply(folder), 0)
        self.assertFalse((self.recipient.root / "game-cache").exists())

    def test_archive_cannot_add_an_undeclared_library(self):
        fake = b"untrusted library"
        path = "evil/executable/1.0/executable-1.0.jar"
        def add(entries):
            meta = json.loads(entries["loader-files/manifest.json"])
            item = {"path": path, **digest(fake)}
            meta["libraries"].append(item)
            entries["loader-files/objects/" + item["sha256"]] = fake
            entries["loader-files/manifest.json"] = json.dumps(meta).encode()
        folder = self.imported(rewrite_zip(self.export(), add))
        self.assertEqual(self.apply(folder), 3)
        self.assertFalse((self.recipient.root / "game-cache/libraries" / path).exists())

    def test_mismatched_pin_missing_object_and_extra_receipt_are_rejected(self):
        def mismatch(entries):
            meta = json.loads(entries["loader-files/manifest.json"])
            meta["loaderVersion"] = "21.1.266"
            entries["loader-files/manifest.json"] = json.dumps(meta).encode()
        def missing(entries):
            entries.pop("loader-files/objects/" + digest(self.jar_data)["sha256"])
        def receipt(entries):
            entries["loader-files/.blockharbor-loader-ready.json"] = b"{}"
        def traversal(entries):
            meta = json.loads(entries["loader-files/manifest.json"])
            meta["libraries"][0]["path"] = "../versions/evil.jar"
            entries["loader-files/manifest.json"] = json.dumps(meta).encode()
        for operation in (mismatch, missing, receipt, traversal):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                self.imported(rewrite_zip(self.export(), operation))
        self.assertEqual(self.recipient.all(), [])

    def test_unknown_digest_retains_candidate_and_never_seeds_trust_from_archive(self):
        folder = self.imported()
        with (patch.dict(preparation._NEO_INSTALLER_SHA1, {}, clear=True),
              patch.object(preparation, "_installer_checksum", side_effect=preparation.DownloadError("offline"))):
            self.assertEqual(self.apply(folder), 0)
        self.assertTrue((folder / loader_files.STAGED / "manifest.json").is_file())
        self.assertFalse((self.recipient.root / "game-cache").exists())

    def test_repair_corrupt_cached_library_from_verified_bundle(self):
        folder = self.imported()
        self.assertEqual(self.apply(folder), 3)
        library = self.recipient.root / "game-cache/libraries" / self.libraries[0]["downloads"]["artifact"]["path"]
        library.write_bytes(b"broken")
        self.assertEqual(self.apply(folder), 1)
        self.assertEqual(library.read_bytes(), self.jar_data)

    def test_library_cache_redirect_cannot_write_outside_cache(self):
        folder = self.imported()
        cache = self.recipient.root / "game-cache"
        cache.mkdir()
        elsewhere = self.root / "unrelated"
        elsewhere.mkdir()
        try:
            (cache / "libraries").symlink_to(elsewhere, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows denied symlink creation")
            raise
        with self.assertRaisesRegex(ValueError, "перенаправлена"):
            self.apply(folder)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_bundle_counts_towards_global_archive_limits(self):
        with patch.object(build_archive, "MAX_TOTAL", len(self.installer_data)), self.assertRaisesRegex(ValueError, "лимиты"):
            self.export()


if __name__ == "__main__":
    unittest.main()

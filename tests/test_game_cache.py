"""A ZIP's loader pin may reuse a completed local install, never partial output."""

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import minecraft_launcher_lib as mc

from launcher import build_archive, game, preparation
from launcher.store import Store


class SharedLoaderCacheTests(unittest.TestCase):
    version = "1.21.1"
    pin = "21.1.267"
    installed = "neoforge-21.1.267"

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(directory) / "launcher")
        self.cache = self.store.root / "game-cache"
        self.pid = self.store.create("Исходная сборка", self.version, "neoforge")
        self.instance = self.store.folder(self.pid)
        self.imported_pin = {"version": self.version, "loader": "neoforge", "loader_version": self.pin}
        game._atomic_json(self.instance / game.STAMP, self.imported_pin)
        self.java = self.cache / "runtime" / "fixture-runtime" / "bin" / "java"
        self.base_bytes, self.loader_bytes = b"verified base library", b"verified loader library"
        self.base_library = self._library("base", self.base_bytes)
        self.loader_library = self._library("loader", self.loader_bytes)
        self.provider = mc.mod_loader.get_mod_loader("neoforge")
        self.enterContext(patch.object(mc.mod_loader, "get_mod_loader", return_value=self.provider))
        self.enterContext(patch.object(mc.runtime, "get_executable_path", return_value=str(self.java)))
        self.enterContext(patch.object(preparation, "runtime_problem", side_effect=self._runtime_problem))
        self.prepare = self.enterContext(patch.object(game, "_prepare_files", side_effect=self._prepare))
        self.vendor = self.enterContext(patch.object(self.provider._base, "install", side_effect=self._install_loader))
        for target in ("launcher.preparation.urlopen", "launcher.game._get_json", "launcher.loader_catalog._fetch_provider"):
            self.enterContext(patch(target, side_effect=AssertionError("unexpected network")))

    @staticmethod
    def _library(name, content):
        return {"name": "fixture:" + name + ":1", "downloads": {"artifact": {
            "path": f"fixture/{name}/1/{name}-1.jar", "size": len(content),
            "sha1": hashlib.sha1(content).hexdigest(), "url": "https://example.invalid/" + name,
        }}}

    def _put_library(self, entry, content):
        path = self.cache / "libraries" / entry["downloads"]["artifact"]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _runtime_problem(self, _cache, _component, _executable):
        return None if self.java.is_file() and self.java.read_bytes() == b"managed Java" else "Java fixture"

    def _prepare(self, version, _cache, _callbacks, _status):
        if version != self.version:
            self.assertEqual(version, self.installed)
            return
        game._atomic_json(game._manifest_path(self.cache, self.version), {
            "id": self.version, "type": "release", "mainClass": "fixture.Main",
            "javaVersion": {"component": "fixture-runtime", "majorVersion": 21},
            "libraries": [self.base_library],
            "downloads": {"client": {"size": 4, "sha1": hashlib.sha1(b"game").hexdigest()}},
            "arguments": {"game": [], "jvm": ["-cp", "${classpath}"]},
        })
        game._manifest_path(self.cache, self.version).with_suffix(".jar").write_bytes(b"game")
        self._put_library(self.base_library, self.base_bytes)
        self.java.parent.mkdir(parents=True, exist_ok=True)
        self.java.write_bytes(b"managed Java")

    def _install_loader(self, _version, _cache, **kwargs):
        self.assertEqual(kwargs["loader_version"], self.pin)
        game._atomic_json(game._manifest_path(self.cache, self.installed), {
            "id": self.installed, "inheritsFrom": self.version, "jar": self.version,
            "mainClass": "fixture.Loader", "libraries": [self.loader_library],
        })
        self._put_library(self.loader_library, self.loader_bytes)

    def _install(self, instance=None):
        return game.install(self.version, "neoforge", self.cache, instance or self.instance)

    def _import(self, *, extra_manifest=None):
        exported = io.BytesIO()
        build_archive.export_build(self.store, self.pid, exported)
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(archive.namelist(), [build_archive.MANIFEST])
            manifest = json.loads(archive.read(build_archive.MANIFEST))
        if extra_manifest:
            manifest.update(extra_manifest)
            exported = io.BytesIO()
            with zipfile.ZipFile(exported, "w") as archive:
                archive.writestr(build_archive.MANIFEST, json.dumps(manifest))
        result = build_archive.import_build(self.store, exported)
        folder = self.store.folder(result["id"])
        self.assertEqual(game._record(folder), self.imported_pin)
        return folder

    def test_zip_pin_reuses_completed_shared_cache_without_reinstalling_or_network(self):
        self.assertEqual(self._install(), self.installed)
        self.vendor.assert_called_once()
        imported = self._import(extra_manifest={"id": "untrusted-profile", "cache": "/untrusted/cache"})
        self.vendor.reset_mock()
        self.prepare.reset_mock()
        with patch.object(game.loader_catalog, "require_supported", side_effect=AssertionError("cached install consulted catalog")):
            self.assertEqual(self._install(imported), self.installed)
        self.vendor.assert_not_called()
        self.prepare.assert_not_called()
        record = game._record(imported)
        self.assertEqual(record["loader_version"], self.pin)
        self.assertEqual(record["id"], self.installed)
        self.assertEqual(record["cache"], str(self.cache))

    def test_failed_vendor_cannot_publish_success_even_if_all_launch_files_exist(self):
        def fail_after_writing(*args, **kwargs):
            self._install_loader(*args, **kwargs)
            raise subprocess.CalledProcessError(1, ["java", "-jar", "fixture-installer.jar"])

        self.vendor.side_effect = fail_after_writing
        with self.assertRaisesRegex(RuntimeError, "завершился с кодом 1"):
            self._install()
        self.assertIsNone(game._missing_install_file(self.cache, self.version, self.installed))
        self.assertFalse(game._loader_receipt_path(self.cache, self.installed).exists())
        imported = self._import()
        self.vendor.side_effect = self._install_loader
        self.vendor.reset_mock()
        self.assertEqual(self._install(imported), self.installed)
        self.vendor.assert_called_once()

    def test_corrupt_library_and_missing_java_force_repair_before_shared_reuse(self):
        self._install()
        for damage in ("library", "java"):
            with self.subTest(damage=damage):
                if damage == "library":
                    self._put_library(self.loader_library, b"x" * len(self.loader_bytes))
                else:
                    self.java.unlink()
                imported = self._import()
                self.vendor.reset_mock()
                self.assertEqual(self._install(imported), self.installed)
                self.vendor.assert_called_once()
                self.assertIsNone(game._missing_install_file(self.cache, self.version, self.installed))

    def test_modified_profile_or_receipt_cannot_authorize_a_different_recipe(self):
        self._install()
        for damage in ("profile", "receipt", "malformed", "deep"):
            with self.subTest(damage=damage):
                if damage == "profile":
                    path = game._manifest_path(self.cache, self.installed)
                    data = json.loads(path.read_bytes())
                    data["mainClass"] = "untrusted.ModifiedMain"
                    game._atomic_json(path, data)
                else:
                    path = game._loader_receipt_path(self.cache, self.installed)
                    if damage == "malformed":
                        path.write_bytes(b"partial JSON")
                    elif damage == "deep":
                        path.write_bytes(b"[" * 2000 + b"]" * 2000)
                    else:
                        data = json.loads(path.read_bytes())
                        data["loader_version"] = "21.1.1"
                        game._atomic_json(path, data)
                imported = self._import()
                self.vendor.reset_mock()
                self.assertEqual(self._install(imported), self.installed)
                self.vendor.assert_called_once()
                self.assertEqual(self.vendor.call_args.kwargs["loader_version"], self.pin)

    def test_reinstallation_revokes_old_receipt_before_vendor_failure(self):
        self._install()
        self._put_library(self.loader_library, b"broken")
        imported = self._import()

        def fail_after_writing(*args, **kwargs):
            self._install_loader(*args, **kwargs)
            raise subprocess.CalledProcessError(1, ["java", "-jar", "fixture-installer.jar"])

        self.vendor.side_effect = fail_after_writing
        with self.assertRaisesRegex(RuntimeError, "завершился с кодом 1"):
            self._install(imported)
        self.assertFalse(game._loader_receipt_path(self.cache, self.installed).exists())
        self.assertIsNone(game._missing_install_file(self.cache, self.version, self.installed))

    def test_existing_verified_instance_upgrades_cache_for_future_zip_imports(self):
        self._install()
        receipt = game._loader_receipt_path(self.cache, self.installed)
        receipt.unlink()
        self.vendor.reset_mock()
        self.prepare.reset_mock()
        self.assertEqual(self._install(), self.installed)
        self.vendor.assert_not_called()
        self.prepare.assert_not_called()
        self.assertTrue(receipt.is_file())
        self.assertEqual(self._install(self._import()), self.installed)
        self.vendor.assert_not_called()

    def test_redirected_profile_directory_cannot_delete_an_outside_receipt(self):
        outside = self.store.root / "outside"
        outside.mkdir()
        receipt = outside / game.LOADER_RECEIPT
        receipt.write_bytes(b"unrelated user file")
        profile = game._manifest_path(self.cache, self.installed).parent
        profile.parent.mkdir(parents=True)
        try:
            profile.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("Directory symlinks are unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "вне кэша"):
            self._install()
        self.assertEqual(receipt.read_bytes(), b"unrelated user file")
        self.vendor.assert_not_called()
        self.prepare.assert_not_called()

    def test_failed_receipt_publication_keeps_verified_installation_but_does_not_authorize_reuse(self):
        replace = game.os.replace

        def fail_receipt(source, target):
            if Path(target).name == game.LOADER_RECEIPT:
                raise PermissionError("fixture: cache receipt cannot be published")
            return replace(source, target)

        with patch.object(game.os, "replace", side_effect=fail_receipt):
            self.assertEqual(self._install(), self.installed)
        self.assertEqual(game._record(self.instance)["id"], self.installed)
        receipt = game._loader_receipt_path(self.cache, self.installed)
        self.assertFalse(receipt.exists())
        self.assertEqual(list(receipt.parent.glob(".blockharbor-*")), [])
        self.vendor.reset_mock()
        self.assertEqual(self._install(self._import()), self.installed)
        self.vendor.assert_called_once()


if __name__ == "__main__":
    unittest.main()

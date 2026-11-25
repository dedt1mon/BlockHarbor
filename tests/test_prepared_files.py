"""A complete local game must not depend on a fresh Java catalog request."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import minecraft_launcher_lib as mc

from launcher import game


class PreparedFilesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.version = "1.21.1"
        self.client = game._manifest_path(self.root, self.version).with_suffix(".jar")
        self.client.parent.mkdir(parents=True)
        self.client.write_bytes(b"verified Minecraft client")
        self.library = self.root / "libraries" / "fixture" / "base" / "1" / "base-1.jar"
        self.library.parent.mkdir(parents=True)
        self.library.write_bytes(b"verified base library")
        self.metadata = {
            "id": self.version, "type": "release", "mainClass": "fixture.Main",
            "arguments": {"game": [], "jvm": ["-cp", "${classpath}"]},
            "downloads": {"client": self.entry(self.client)},
            "libraries": [{"name": "fixture:base:1", "downloads": {"artifact": {
                **self.entry(self.library), "path": "fixture/base/1/base-1.jar",
            }}}],
        }
        game._manifest_path(self.root, self.version).write_text(json.dumps(self.metadata), encoding="utf-8")
        self.messages = []

    @staticmethod
    def entry(path):
        data = path.read_bytes()
        return {"size": len(data), "sha1": hashlib.sha1(data).hexdigest()}

    def test_complete_vanilla_does_not_consult_vendor_or_live_java_catalog(self):
        with patch.object(mc.install, "install_minecraft_version", side_effect=AssertionError("unnecessary live preparation")):
            game._prepare_files(self.version, self.root, {}, self.messages.append)
        self.assertEqual(self.messages, ["Проверяем локальные файлы игры…"])

    def test_complete_inherited_loader_verifies_base_and_declared_libraries_locally(self):
        installed = "neoforge-21.1.267"
        path = game._manifest_path(self.root, installed)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": installed, "inheritsFrom": self.version,
                                    "mainClass": "fixture.Loader", "libraries": []}), encoding="utf-8")
        with patch.object(mc.install, "install_minecraft_version", side_effect=AssertionError("unnecessary live preparation")):
            game._prepare_files(installed, self.root, {}, self.messages.append)
        self.assertEqual(self.messages, ["Проверяем локальные файлы игры…"])
        alias = path.with_suffix(".jar")
        self.assertEqual(alias.read_bytes(), self.client.read_bytes())
        alias.write_bytes(b"x" * self.client.stat().st_size)
        self.assertEqual(game._missing_install_file(self.root, self.version, installed), alias.name)
        with patch.object(mc.install, "install_minecraft_version", side_effect=AssertionError("unnecessary live preparation")):
            game._prepare_files(installed, self.root, {}, self.messages.append)
        self.assertEqual(alias.read_bytes(), self.client.read_bytes())
        self.assertIsNone(game._missing_install_file(self.root, self.version, installed))

    def test_explicit_or_custom_client_is_not_replaced_by_vanilla(self):
        for extra in ({"jar": "custom"}, {"downloads": {"client": {"sha1": "0" * 40}}}):
            with self.subTest(override=extra):
                installed = "neoforge-21.1.267"
                path = game._manifest_path(self.root, installed).with_suffix(".jar")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"custom client")
                game._reuse_inherited_client(self.root, installed,
                    {"id": installed, "inheritsFrom": self.version, **extra})
                self.assertEqual(path.read_bytes(), b"custom client")

    def test_missing_or_same_size_corrupt_file_reaches_the_verified_repair_path(self):
        for broken in (self.client, self.library):
            with self.subTest(file=broken.name):
                original = broken.read_bytes()
                broken.write_bytes(b"x" * len(original))
                with patch.object(mc.install, "install_minecraft_version") as install:
                    game._prepare_files(self.version, self.root, {}, self.messages.append)
                install.assert_called_once_with(self.version, self.root, callback={})
                broken.write_bytes(original)
        self.library.unlink()
        with patch.object(mc.install, "install_minecraft_version") as install:
            game._prepare_files(self.version, self.root, {}, self.messages.append)
        install.assert_called_once_with(self.version, self.root, callback={})


if __name__ == "__main__":
    unittest.main()

"""Runtime constraints are checked using actual installation metadata, not guesses."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from launcher import compatibility, game
from launcher.store import Store


class PlatformCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root)
        self.pid = self.store.create("Проверка Java", "1.20.1", "fabric")
        self.jar = self.root / "mod.jar"

    def report(self, metadata, *, profile=None, label="fabric.mod.json"):
        with zipfile.ZipFile(self.jar, "w") as archive:
            archive.writestr(label, json.dumps(metadata))
        return compatibility.local_report(profile or self.store.profile(self.pid), {}, {"mod.jar": self.jar})

    def test_known_java_and_loader_requirements_are_hard_constraints(self):
        profile = {**self.store.profile(self.pid), "java_major": 17, "loader_version": "0.16.9"}
        for dependency, bound in (("java", ">=21"), ("fabricloader", ">=0.17")):
            with self.subTest(dependency=dependency):
                report = self.report({"id": "example", "version": "1", "depends": {dependency: bound}}, profile=profile)
                self.assertTrue(report["blocked"])
                self.assertIn(dependency, report["conflicts"][0])
                self.assertFalse(report["required"])
        good = self.report({"id": "example", "version": "1", "depends": {"java": ">=17", "fabricloader": ">=0.16"}}, profile=profile)
        self.assertFalse(good["blocked"])
        self.assertFalse(good["warnings"])

    def test_uninstalled_platform_bound_requires_review_instead_of_being_ignored(self):
        result = self.report({"id": "example", "version": "1", "depends": {"java": ">=21", "fabricloader": ">=0.17"}})
        self.assertFalse(result["blocked"])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertFalse(result["required"])
        self.assertTrue(all("не подтверждена" in warning for warning in result["warnings"]))

    def test_fabric_and_quilt_dedicated_server_jars_are_not_client_mods(self):
        for loader, label, metadata in (
            ("fabric", "fabric.mod.json", {"id": "example", "version": "1", "environment": "server"}),
            ("quilt", "quilt.mod.json", {"quilt_loader": {"id": "example", "version": "1"}, "minecraft": {"environment": "dedicated_server"}}),
        ):
            with self.subTest(loader=loader):
                result = self.report(metadata, label=label, profile={**self.store.profile(self.pid), "loader": loader})
                self.assertTrue(result["blocked"])
                self.assertIn("выделенного сервера", result["conflicts"][0])
                self.assertFalse(result["warnings"])

    def test_quilt_version_does_not_impersonate_fabric_compatibility_version(self):
        profile = {**self.store.profile(self.pid), "loader": "quilt", "loader_version": "0.29.3"}
        result = self.report({"id": "example", "version": "1", "depends": {"fabricloader": ">=0.28"}}, profile=profile)
        self.assertFalse(result["blocked"])
        self.assertTrue(result["warnings"])

    def test_fabric_caret_keeps_the_vendor_semantics_for_zero_major(self):
        # Fabric explicitly has no npm-style special case for major zero.
        self.assertTrue(compatibility._matches("0.4.2", "^0.3"))
        self.assertFalse(compatibility._matches("1.0", "^0.3"))
        self.assertFalse(compatibility._matches("2.2", "~2.1"))

    def setup_installation(self):
        cache = (self.root / "game-cache").resolve()
        installed = "fabric-loader-0.16.9-1.20.1"
        marker = self.store.folder(self.pid) / game.STAMP
        record = {"version": "1.20.1", "loader": "fabric", "loader_version": "0.16.9", "id": installed, "cache": str(cache), "java": None}
        game._atomic_json(marker, record)
        game._atomic_json(game._manifest_path(cache, installed), {"id": installed, "inheritsFrom": "1.20.1"})
        game._atomic_json(game._manifest_path(cache, "1.20.1"), {"id": "1.20.1", "javaVersion": {"majorVersion": 17}})
        return record, marker, cache

    def test_installation_context_requires_matching_profile_and_manifests(self):
        record, marker, cache = self.setup_installation()
        profile = compatibility.installation_profile(self.store, self.pid)
        self.assertEqual(profile["java_major"], 17)
        self.assertEqual(profile["loader_version"], "0.16.9")
        self.assertNotIn("java_major", self.store.profile(self.pid))
        record["version"] = "1.21.1"
        game._atomic_json(marker, record)
        self.assertNotIn("loader_version", compatibility.installation_profile(self.store, self.pid))
        record["version"] = "1.20.1"
        game._atomic_json(marker, record)
        game._manifest_path(cache, record["id"]).unlink()
        self.assertNotIn("loader_version", compatibility.installation_profile(self.store, self.pid))

    def test_imported_java_path_or_external_cache_is_not_probed_or_trusted(self):
        record, marker, cache = self.setup_installation()
        record["java"] = "C:/untrusted/imported.exe"
        game._atomic_json(marker, record)
        self.assertNotIn("java_major", compatibility.installation_profile(self.store, self.pid))
        record["cache"] = str(self.root / "other-cache")
        game._atomic_json(marker, record)
        self.assertNotIn("loader_version", compatibility.installation_profile(self.store, self.pid))


if __name__ == "__main__":
    unittest.main()

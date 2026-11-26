import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import prepare_neoforge_bundle as bundle


class NeoForgeBundleBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache, self.stage = self.root / "cache", self.root / "stage"
        self.lock = self.root / "lock.json"
        self.payloads = {}
        requirements = patch.object(bundle, "REQUIRED_SOURCE_PROJECTS", frozenset({"Example"}))
        requirements.start()
        self.addCleanup(requirements.stop)

        def entry(kind, path, data, origin=bundle.ORIGIN):
            url = origin + path
            self.payloads[url] = data
            sha256 = hashlib.sha256(data).hexdigest()
            return {"kind": kind, "path": path, "url": url, "size": len(data),
                    "sha1": hashlib.sha1(data).hexdigest(), "sha256": sha256,
                    "object": sha256, "notices": ["Example library"]}

        library = entry("library", "org/example/library/1.0/library-1.0.jar", b"verified library")
        artifact = {key: library[key] for key in ("path", "url", "sha1", "size")}
        libraries = [{"name": "org.example:library:1.0", "downloads": {"artifact": artifact}}]
        installer_bytes = io.BytesIO()
        with zipfile.ZipFile(installer_bytes, "w") as archive:
            archive.writestr("install_profile.json", json.dumps({
                "minecraft": "1.21.1", "version": "neoforge-21.1.267", "libraries": libraries}))
            archive.writestr("version.json", json.dumps({
                "inheritsFrom": "1.21.1", "id": "neoforge-21.1.267", "libraries": libraries}))
        installer = entry("installer", "net/neoforged/neoforge/21.1.267/neoforge-21.1.267-installer.jar",
                          installer_bytes.getvalue())
        source_bytes = io.BytesIO()
        with zipfile.ZipFile(source_bytes, "w") as archive:
            archive.writestr("example/pom.xml", "build configuration")
            archive.writestr("example/LICENSE", "License text")
            archive.writestr("example/src/Library.java", "class Library {}")
        source = entry("source", "example/library/archive/" + "a" * 40 + ".zip",
                       source_bytes.getvalue(), "https://github.com/")
        source["sourceProject"] = "Example"
        self.manifest = {"format": 1, "minecraft": "1.21.1", "loader": "neoforge",
                         "loaderVersion": "21.1.267", "directory": "neoforge-21.1.267",
                         "files": [installer, library, source],
                         "notices": [{"name": "Example library", "text": "License text",
                                      "source": source["url"]}]}
        self.write_lock()
        self.downloads = []

    def write_lock(self):
        data = bundle._documents(self.manifest)["THIRD-PARTY-NOTICES.txt"].encode("utf-8")
        self.manifest["documentation"] = {"THIRD-PARTY-NOTICES.txt": {
            "size": len(data), "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest()}}
        self.lock.write_text(json.dumps(self.manifest), encoding="utf-8")

    def download(self, url, destination, size):
        self.downloads.append(url)
        destination.write_bytes(self.payloads[url])

    def prepare(self):
        with patch.object(bundle, "_download", self.download):
            return bundle.prepare(self.cache, self.stage, self.lock)

    def test_cold_build_stages_exact_runtime_sources_and_notices_then_reuses_verified_cache(self):
        directory = self.prepare()
        self.assertCountEqual(self.downloads, self.payloads)
        self.assertEqual(bundle.verify_staged(self.stage, self.lock), directory)
        for entry in self.manifest["files"]:
            self.assertEqual((directory / "objects" / entry["object"]).read_bytes(),
                             self.payloads[entry["url"]])
        notices = (directory / "THIRD-PARTY-NOTICES.txt").read_text("utf-8")
        self.assertIn("License text", notices)
        self.assertIn(self.manifest["files"][-1]["object"], notices)
        self.downloads.clear()
        self.prepare()
        self.assertEqual(self.downloads, [])

    def test_same_size_corrupt_cache_is_replaced_and_reverified(self):
        self.prepare()
        library = self.manifest["files"][1]
        cached = self.cache / "neoforge-bundle" / "objects" / library["object"]
        cached.write_bytes(b"x" * library["size"])
        self.downloads.clear()
        directory = self.prepare()
        self.assertEqual(self.downloads, [library["url"]])
        self.assertEqual(cached.read_bytes(), self.payloads[library["url"]])
        self.assertEqual((directory / "objects" / library["object"]).read_bytes(), cached.read_bytes())

    def test_checksum_mismatch_fails_build_without_publishing_cache_object(self):
        entry = self.manifest["files"][0]
        self.payloads[entry["url"]] = b"x" * entry["size"]
        with self.assertRaisesRegex(bundle.BundleBuildError, "SHA-1/SHA-256 mismatch"):
            self.prepare()
        self.assertEqual(self.downloads.count(entry["url"]), 3)
        self.assertFalse((self.cache / "neoforge-bundle" / "objects" / entry["object"]).exists())
        self.assertFalse((self.stage / "bundled").exists())
        self.assertEqual(list(self.cache.rglob(".download-*")), [])

    def test_missing_or_changed_staged_source_is_a_release_failure(self):
        directory = self.prepare()
        source = self.manifest["files"][-1]
        staged = directory / "objects" / source["object"]
        staged.unlink()
        with self.assertRaisesRegex(bundle.BundleBuildError, "Missing or damaged staged"):
            bundle.verify_staged(self.stage, self.lock)
        staged.write_bytes(b"x" * source["size"])
        with self.assertRaisesRegex(bundle.BundleBuildError, "Missing or damaged staged"):
            bundle.verify_staged(self.stage, self.lock)

    def test_missing_runtime_library_in_lock_is_detected_from_official_installer(self):
        self.manifest["files"].pop(1)
        self.write_lock()
        with self.assertRaisesRegex(bundle.BundleBuildError, "missing=.*org/example/library"):
            self.prepare()
        self.assertFalse((self.stage / "bundled").exists())

    def test_changed_library_hash_in_lock_cannot_override_installer_metadata(self):
        entry = self.manifest["files"][1]
        data = b"replacement pretending to be upstream"
        self.payloads[entry["url"]] = data
        entry.update(size=len(data), sha1=hashlib.sha1(data).hexdigest(),
                     sha256=hashlib.sha256(data).hexdigest(), object=hashlib.sha256(data).hexdigest())
        self.write_lock()
        with self.assertRaisesRegex(bundle.BundleBuildError, "does not match official installer"):
            self.prepare()

    def test_bad_lock_paths_urls_hashes_and_pins_fail_before_fetch(self):
        mutations = [
            lambda m: m.update(directory="../outside"),
            lambda m: m.update(loaderVersion="latest"),
            lambda m: m["files"][1].update(path="org/../../escape.jar"),
            lambda m: m["files"][1].update(path="org/CON/library.jar"),
            lambda m: m["files"][1].update(url="http://maven.neoforged.net/releases/" + m["files"][1]["path"]),
            lambda m: m["files"][1].update(url=m["files"][1]["url"] + "?untrusted=1"),
            lambda m: m["files"][1].update(object="a" * 64),
            lambda m: m["files"].append(copy.deepcopy(m["files"][1])),
        ]
        initial = copy.deepcopy(self.manifest)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.manifest = copy.deepcopy(initial)
                mutate(self.manifest)
                self.write_lock()
                with self.assertRaises(bundle.BundleBuildError):
                    self.prepare()
        self.assertEqual(self.downloads, [])

    def test_notice_truncation_is_detected_before_packaging(self):
        directory = self.prepare()
        (directory / "THIRD-PARTY-NOTICES.txt").write_text("truncated", encoding="utf-8")
        with self.assertRaisesRegex(bundle.BundleBuildError, "documentation"):
            bundle.verify_staged(self.stage, self.lock)

    def test_removing_required_source_or_license_does_not_waive_release_obligation(self):
        self.manifest["files"].pop()
        self.write_lock()
        with self.assertRaisesRegex(bundle.BundleBuildError, "Missing corresponding source"):
            self.prepare()
        self.assertEqual(self.downloads, [])

    def test_source_archive_requires_compilation_scripts_and_license(self):
        source = self.manifest["files"][-1]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("src/Library.java", "class Library {}")
        data = archive.getvalue()
        source.update(size=len(data), sha1=hashlib.sha1(data).hexdigest(),
                      sha256=hashlib.sha256(data).hexdigest(), object=hashlib.sha256(data).hexdigest())
        self.payloads[source["url"]] = data
        self.write_lock()
        with self.assertRaisesRegex(bundle.BundleBuildError, "source, license or build scripts"):
            self.prepare()

    def test_runtime_notice_reference_cannot_be_dropped(self):
        self.manifest["files"][1]["notices"] = []
        self.write_lock()
        with self.assertRaisesRegex(bundle.BundleBuildError, "Missing license notice"):
            self.prepare()
        self.assertEqual(self.downloads, [])

    def test_notice_digest_is_a_trusted_lock_for_zip_exports(self):
        self.manifest["documentation"]["THIRD-PARTY-NOTICES.txt"]["sha256"] = "0" * 64
        self.lock.write_text(json.dumps(self.manifest), encoding="utf-8")
        with self.assertRaisesRegex(bundle.BundleBuildError, "notice hashes"):
            self.prepare()
        self.assertEqual(self.downloads, [])

    def test_original_notice_line_endings_match_locked_distribution_bytes(self):
        self.manifest["notices"][0]["text"] = "Original upstream notice\r\nLicense terms\r\n"
        self.write_lock()
        directory = self.prepare()
        data = (directory / "THIRD-PARTY-NOTICES.txt").read_bytes()
        expected = self.manifest["documentation"]["THIRD-PARTY-NOTICES.txt"]
        self.assertIn(b"notice\r\nLicense", data)
        self.assertEqual(hashlib.sha256(data).hexdigest(), expected["sha256"])

    def test_exe_pack_preserves_existing_bundled_components_and_uses_only_verified_neoforge_stage(self):
        directory = self.prepare()
        sys.path.insert(0, str(Path(bundle.__file__).parent))
        try:
            import build_windows
        finally:
            sys.path.pop(0)
        project = self.root / "project"
        bridge = project / "bundled" / "bridge" / "bridge.jar"
        bridge.parent.mkdir(parents=True)
        bridge.write_bytes(b"existing bridge component")
        entry = self.manifest["files"][0]
        stale = project / "bundled" / "neoforge-21.1.267" / "objects" / entry["object"]
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"untrusted local smoke copy")
        (self.stage / "BlockHarbor.exe").write_bytes(b"test PE stub")
        target = self.root / "BlockHarbor.exe"
        with (patch.object(build_windows, "PROJECT", project),
              patch.object(build_windows, "verify_staged", lambda stage: bundle.verify_staged(stage, self.lock))):
            build_windows.pack_program(self.stage, target)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.read("bundled/bridge/bridge.jar"), bridge.read_bytes())
                name = "bundled/neoforge-21.1.267/objects/" + entry["object"]
                self.assertEqual(archive.read(name), self.payloads[entry["url"]])
            (directory / "objects" / self.manifest["files"][-1]["object"]).unlink()
            broken_target = self.root / "incomplete.exe"
            with self.assertRaisesRegex(bundle.BundleBuildError, "Missing or damaged staged"):
                build_windows.pack_program(self.stage, broken_target)
            self.assertFalse(broken_target.exists())


if __name__ == "__main__":
    unittest.main()

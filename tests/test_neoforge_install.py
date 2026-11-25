import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import lifecycle, neoforge_install, preparation


class NeoForgeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "game-cache"
        self.cache.mkdir()
        self.jar = self.root / "neoforge-installer.jar"
        self.library = b"official verified dependency"
        self.client = b"official minecraft client"
        self.mapping = b"official minecraft client mappings"
        self.relative = "net/neoforged/test/1/test-1.jar"
        self.artifact = {
            "path": self.relative,
            "url": "https://maven.neoforged.net/releases/" + self.relative,
            "sha1": hashlib.sha1(self.library).hexdigest(),
            "size": len(self.library),
        }
        self.profile = {
            "spec": 1, "version": "neoforge-21.1.267", "minecraft": "1.21.1", "json": "/version.json",
            "libraries": [{"name": "net.neoforged:test:1", "downloads": {"artifact": self.artifact}}],
            "data": {"MOJMAPS": {"client": "[net.minecraft:client:1.21.1-20240808.144430:mappings@txt]"}},
            "processors": [{"jar": "net.neoforged.installertools:installertools:2.1.2", "args": [
                "--task", "DOWNLOAD_MOJMAPS", "--version", "1.21.1", "--side", "{SIDE}", "--output", "{MOJMAPS}",
            ]}],
        }
        self.descriptor = {"id": "neoforge-21.1.267", "inheritsFrom": "1.21.1", "libraries": copy.deepcopy(self.profile["libraries"])}
        self.command = ["java", "-jar", str(self.jar), "--install-client", str(self.cache)]
        metadata = {"id": "1.21.1", "downloads": {}}
        self.payloads = {self.artifact["url"]: self.library}
        for name, payload in (("client", self.client), ("client_mappings", self.mapping)):
            digest = hashlib.sha1(payload).hexdigest()
            url = "https://piston-data.mojang.com/v1/objects/" + digest + "/" + name
            metadata["downloads"][name] = {"url": url, "sha1": digest, "size": len(payload)}
            self.payloads[url] = payload
        self.metadata = metadata
        path = self.cache / "versions/1.21.1/1.21.1.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(metadata))
        self.downloader = Mock()
        self.downloader.side_effect = self.fetch
        self.make_installer()

    def fetch(self, url, target, *, sha1, minecraft_directory):
        self.assertEqual(Path(minecraft_directory), self.cache)
        payload = self.payloads[url]
        self.assertEqual(hashlib.sha1(payload).hexdigest(), sha1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return True

    def make_installer(self, installer_version="3.0.48", capabilities=True):
        with zipfile.ZipFile(self.jar, "w") as archive:
            archive.writestr("install_profile.json", json.dumps(self.profile))
            archive.writestr("version.json", json.dumps(self.descriptor))
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\nMain-Class: net.minecraftforge.installer.SimpleInstaller\r\n\r\nName: net/minecraftforge/installer/\r\nImplementation-Vendor: NeoForge\r\nImplementation-Version: " + installer_version + "\r\n")
            if capabilities:
                for name, marker in {
                    "SimpleInstaller.class": b"ENABLING OFFLINE MODE",
                    "Downloader$LocalSource.class": b"/maven/",
                    "actions/PostProcessors.class": b"Skipping mojmaps download due to local cache hit.",
                }.items():
                    archive.writestr("net/minecraftforge/installer/" + name, marker)

    def test_dependencies_and_mappings_are_verified_before_offline_java(self):
        original = self.jar.read_bytes()
        with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache) as command:
            self.assertEqual(command[:2], ["java", "-cp"])
            self.assertIn("--offline", command)
            self.assertEqual(command[-2:], ["--install-client", str(self.cache)])
            jar, resource = command[2].split(os.pathsep)
            self.assertEqual(jar, str(self.jar))
            resource = Path(resource)
            self.assertEqual((resource / "maven/minecraft/1.21.1/client_mappings.txt").read_bytes(), self.mapping)
            self.assertEqual((self.cache / "libraries" / self.relative).read_bytes(), self.library)
            self.assertEqual((self.cache / "versions/1.21.1/1.21.1.jar").read_bytes(), self.client)
            self.assertEqual(self.jar.read_bytes(), original)
        self.assertFalse(resource.exists())
        self.assertEqual((self.cache / "installer-inputs/1.21.1/client_mappings.txt").read_bytes(), self.mapping)
        self.assertEqual(self.downloader.call_count, 3)

    def test_installer_artifacts_deduplicate_both_authoritative_manifests(self):
        artifacts = neoforge_install.installer_artifacts(self.jar, "1.21.1", "21.1.267")
        self.assertEqual(artifacts, [self.artifact])
        for mc, pin in (("1.21.2", "21.1.267"), ("1.21.1", "21.1.266")):
            with self.subTest(mc=mc, pin=pin), self.assertRaises(preparation.DownloadError):
                neoforge_install.installer_artifacts(self.jar, mc, pin)

    def test_failed_dependency_does_not_enter_java_body(self):
        self.downloader.side_effect = preparation.DownloadError("origin and mirror unavailable")
        entered = False
        with self.assertRaisesRegex(preparation.DownloadError, "mirror unavailable"):
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache):
                entered = True
        self.assertFalse(entered)
        self.assertFalse((self.cache / "installer-inputs").exists())

    def test_corrupt_payload_stops_before_java_even_if_downloader_claims_success(self):
        def corrupt(_url, target, **_kwargs):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"bad")
        self.downloader.side_effect = corrupt
        with self.assertRaisesRegex(preparation.DownloadError, "контрольная сумма"):
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache):
                self.fail("corrupt dependency accepted")

    def test_unsupported_installer_prefetches_without_forcing_offline(self):
        for version, capability in (("3.0.99", True), ("3.0.48", False)):
            self.downloader.reset_mock()
            self.make_installer(version, capability)
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache) as command:
                self.assertEqual(command, self.command)
            self.assertEqual(self.downloader.call_count, 1)

    def test_unknown_processor_keeps_original_vendor_contract(self):
        self.profile["processors"].append({"jar": "publisher:network-tool:1", "args": []})
        self.make_installer()
        with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache) as command:
            self.assertEqual(command, self.command)
        self.assertEqual(self.downloader.call_count, 1)

    def test_classpath_separator_in_game_directory_preserves_working_jar_command(self):
        moved = self.root / ("game" + os.pathsep + "cache")
        self.cache.rename(moved)
        self.cache = moved
        self.command[-1] = str(moved)
        with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache) as command:
            self.assertEqual(command, self.command)
        self.assertEqual((self.cache / "libraries" / self.relative).read_bytes(), self.library)
        self.assertEqual(self.downloader.call_count, 1)

    def test_server_only_processor_does_not_disable_client_offline_contract(self):
        self.profile["processors"].append({"jar": "publisher:network-tool:1", "sides": ["server"], "args": []})
        self.make_installer()
        with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache) as command:
            self.assertIn("--offline", command)

    def test_unrelated_java_commands_are_unchanged(self):
        for command in (("java", "-version"), ["java", "-jar", "fabric.jar", "client"], ["java", "-cp", "processor.jar", "Main"]):
            with neoforge_install.adapt(command, downloader=self.downloader, cache=self.cache) as actual:
                self.assertEqual(actual, command)
        self.downloader.assert_not_called()

    def test_wrong_cache_argument_is_rejected(self):
        self.command[-1] = str(self.root / "outside")
        with self.assertRaisesRegex(preparation.DownloadError, "папка установки"):
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache):
                self.fail("outside cache accepted")

    def test_unsafe_or_unverifiable_library_metadata_is_rejected(self):
        variants = [
            {"path": "../escape.jar"}, {"path": "net/neoforged/test/1/wrong-1.jar"},
            {"sha1": ""}, {"size": 0}, {"size": True},
            {"url": "http://maven.neoforged.net/releases/" + self.relative},
            {"url": "https://user:password@maven.neoforged.net/releases/" + self.relative},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.profile["libraries"][0]["downloads"]["artifact"] = dict(self.artifact, **variant)
                self.make_installer()
                with self.assertRaises(preparation.DownloadError):
                    neoforge_install.installer_artifacts(self.jar, "1.21.1", "21.1.267")

    def test_conflicting_runtime_hash_is_rejected(self):
        self.descriptor["libraries"][0]["downloads"]["artifact"]["sha1"] = "0" * 40
        self.make_installer()
        with self.assertRaisesRegex(preparation.DownloadError, "противоречивые"):
            neoforge_install.installer_artifacts(self.jar, "1.21.1", "21.1.267")

    def test_symlink_library_destination_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.cache / "libraries").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks require OS permission")
        with self.assertRaisesRegex(preparation.DownloadError, "перенаправлена"):
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache):
                self.fail("symlink accepted")
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_official_mapping_source_is_rejected(self):
        self.metadata["downloads"]["client_mappings"]["url"] = "https://untrusted.invalid/mappings.txt"
        (self.cache / "versions/1.21.1/1.21.1.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(preparation.DownloadError, "client_mappings"):
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache):
                self.fail("untrusted mappings accepted")

    def test_resource_directory_is_removed_on_java_failure(self):
        resource = None
        with self.assertRaisesRegex(RuntimeError, "processor failed"):
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache) as command:
                resource = Path(command[2].split(os.pathsep)[1])
                raise RuntimeError("processor failed")
        self.assertFalse(resource.exists())
        self.assertTrue((self.cache / "libraries" / self.relative).is_file())

    def test_cancelled_preparation_never_runs_java(self):
        scope = lifecycle.CancellationScope()
        def cancel(*_args, **_kwargs):
            scope.cancel()
            lifecycle.check()
        self.downloader.side_effect = cancel
        with lifecycle.bind(scope), self.assertRaises(lifecycle.OperationCancelled):
            with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache):
                self.fail("cancelled installer executed")
        self.downloader.assert_called_once()

    def test_cancellation_before_any_worker_starts_does_not_wait_on_cancelled_future(self):
        scope = lifecycle.CancellationScope()
        outcome = []

        class QueuedExecutor:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def submit(self, *_args, **_kwargs):
                future = Future()
                future.cancel()
                scope.cancel()
                return future

            def shutdown(self, **_kwargs):
                pass

        def run():
            try:
                with lifecycle.bind(scope):
                    with neoforge_install.adapt(self.command, downloader=self.downloader, cache=self.cache):
                        outcome.append("executed")
            except lifecycle.OperationCancelled:
                outcome.append("cancelled")

        with patch.object(lifecycle, "Executor", QueuedExecutor):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "cancelled queued future blocked installer preparation")
        self.assertEqual(outcome, ["cancelled"])
        self.downloader.assert_not_called()


if __name__ == "__main__":
    unittest.main()

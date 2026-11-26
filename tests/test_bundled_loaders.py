"""Shipped loader inputs work with both Maven routes unavailable, before HTTP."""

import copy
import hashlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from launcher import bundled_loaders, lifecycle, preparation


def jar(contents):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fixture.txt", contents)
    return output.getvalue()


class BundledLoaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.app = self.root / "app"
        self.manifest = self.app / "launcher" / "neoforge_bundle.json"
        self.manifest.parent.mkdir(parents=True)
        self.objects = self.app / "bundled" / "neoforge-21.1.267" / "objects"
        self.objects.mkdir(parents=True)
        self.cache = self.root / "game-cache"
        self.messages = []
        self.progress = preparation.Progress(self.messages.append)
        self.installer_bytes = jar("publisher's original installer")
        self.library_bytes = jar("publisher's original dependency")
        self.installer = self.entry(
            "installer", "net/neoforged/neoforge/21.1.267/neoforge-21.1.267-installer.jar", self.installer_bytes)
        self.library = self.entry("library", "net/neoforged/bus/8.0.5/bus-8.0.5.jar", self.library_bytes)
        self.data = {
            "format": 1, "minecraft": "1.21.1", "loader": "neoforge", "loaderVersion": "21.1.267",
            "directory": "neoforge-21.1.267", "files": [self.installer, self.library],
        }
        self.write_manifest()
        self.enterContext(patch.object(bundled_loaders, "ROOT", self.app))
        self.enterContext(patch.object(bundled_loaders, "MANIFEST", self.manifest))
        self.enterContext(patch.dict(preparation._NEO_INSTALLER_SHA1, {
            self.installer["path"]: self.installer["sha1"],
        }))

    def entry(self, kind, path, payload):
        sha256 = hashlib.sha256(payload).hexdigest()
        (self.objects / sha256).write_bytes(payload)
        return {
            "kind": kind, "path": path, "url": preparation._NEO_ORIGIN + path,
            "sha1": hashlib.sha1(payload).hexdigest(), "sha256": sha256,
            "object": sha256, "size": len(payload),
        }

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def add_documentation(self):
        source = self.entry("source", "Fixture/archive/1234567890.zip", jar("original complete source and build scripts"))
        source["sourceProject"] = "Fixture"
        self.data["files"].append(source)
        payload = b"Original upstream copyright and license notices.\n"
        notice = self.objects.parent / "THIRD-PARTY-NOTICES.txt"
        notice.write_bytes(payload)
        self.data["documentation"] = {notice.name: {
            "sha1": hashlib.sha1(payload).hexdigest(), "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }}
        self.write_manifest()
        return source, notice

    def download(self, url, target, **kwargs):
        downloader = preparation.Download(self.progress, cache=self.cache)
        try:
            return downloader(url, target, **kwargs)
        finally:
            downloader.close()

    @contextmanager
    def no_network(self):
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError("network is unavailable")))
                     for owner, name in (
                         (preparation, "_receive"), (preparation, "_receive_requests"),
                         (preparation, "_installer_checksum"), (socket, "create_connection"),
                         (socket.socket, "connect"),
                     )]
            yield
            for mocked in mocks:
                mocked.assert_not_called()

    def test_cold_installer_uses_bundle_before_both_hosts_and_seeds_normal_persistent_cache(self):
        target = self.root / "vendor" / "neoforge-installer.jar"
        directory = self.cache / "installer-cache" / hashlib.sha256(self.installer["url"].encode()).hexdigest()
        directory.mkdir(parents=True)
        partial = directory / "installer.part"
        partial.write_bytes(b"old interrupted download")
        partial.with_suffix(".json").write_text("{}")
        with self.no_network():
            self.assertTrue(self.download(self.installer["url"], target))
            self.assertFalse(self.download(self.installer["url"], target))
        self.assertEqual(target.read_bytes(), self.installer_bytes)
        self.assertEqual((directory / "installer.jar").read_bytes(), self.installer_bytes)
        self.assertEqual(json.loads((directory / "source.json").read_bytes()), {
            "format": 1, "url": self.installer["url"], "sha1": self.installer["sha1"],
        })
        self.assertFalse(partial.exists())
        self.assertFalse(partial.with_suffix(".json").exists())
        self.assertTrue(any("комплект BlockHarbor" in message for message in self.messages))
        # A later preparation can reuse the ordinary verified cache without
        # depending on the previous vendor's temporary directory or the bundle.
        target.unlink()
        shutil.rmtree(self.app / "bundled")
        with self.no_network():
            next_target = self.root / "next-vendor" / target.name
            self.assertTrue(self.download(self.installer["url"], next_target))
        self.assertEqual(next_target.read_bytes(), self.installer_bytes)

    def test_library_and_canonicalized_official_url_use_verified_bundle_without_http(self):
        target = self.cache / "libraries" / self.library["path"]
        original = self.library["url"].replace(preparation._NEO_ORIGIN, "https://maven.neoforged.net/")
        with self.no_network():
            self.assertTrue(self.download(original, target, sha1=self.library["sha1"], minecraft_directory=self.cache))
            self.assertFalse(self.download(self.library["url"], target, sha1=self.library["sha1"]))
        self.assertEqual(target.read_bytes(), self.library_bytes)
        self.assertEqual(bundled_loaders.verified_source(self.library["url"], self.library["sha1"].upper()),
                         self.objects / self.library["object"])

    def test_unknown_version_keeps_ordinary_authoritative_network_path(self):
        url = self.installer["url"].replace("21.1.267", "21.1.999")
        target = self.root / "different-installer.jar"

        def receive(address, output, *_args, **_kwargs):
            self.assertEqual(address, url)
            output.write(self.installer_bytes)
            return {}, address

        with (patch.object(preparation, "_receive", side_effect=receive) as network,
              patch.object(preparation, "_installer_checksum", side_effect=AssertionError("explicit checksum supplied"))):
            self.assertTrue(self.download(url, target, sha1=self.installer["sha1"]))
        network.assert_called_once()
        self.assertEqual(target.read_bytes(), self.installer_bytes)
        self.assertIsNone(bundled_loaders.verified_source(url, self.installer["sha1"]))

    def test_different_expected_sha1_cannot_resolve_even_an_exact_known_url(self):
        replacement = jar("new publisher metadata supplied different bytes")
        expected = hashlib.sha1(replacement).hexdigest()
        self.assertIsNone(bundled_loaders.verified_source(self.library["url"], expected))

        def receive(address, output, *_args, **_kwargs):
            self.assertEqual(address, self.library["url"])
            output.write(replacement)
            return {}, address

        target = self.root / "new-library.jar"
        with patch.object(preparation, "_receive", side_effect=receive) as network:
            self.assertTrue(self.download(self.library["url"], target, sha1=expected))
        network.assert_called_once()
        self.assertEqual(target.read_bytes(), replacement)

    def test_bundle_never_authorizes_unhashed_or_arbitrary_url_downloads(self):
        for address, expected in (
            (self.library["url"], None), (self.library["url"], "not a checksum"),
            (self.library["url"] + "?redirect=1", self.library["sha1"]),
            (self.library["url"].replace("https:", "http:"), self.library["sha1"]),
            (self.library["url"].replace("maven.neoforged.net", "example.invalid"), self.library["sha1"]),
        ):
            with self.subTest(address=address, expected=expected):
                self.assertIsNone(bundled_loaders.verified_source(address, expected))

    def test_corrupt_installer_never_reaches_java_or_replaces_existing_cache(self):
        from minecraft_launcher_lib.mod_loader._neoforge import Neoforge

        source = self.objects / self.installer["object"]
        source.write_bytes(bytes(byte ^ 1 for byte in self.installer_bytes))  # same size, wrong bytes
        directory = self.cache / "installer-cache" / hashlib.sha256(self.installer["url"].encode()).hexdigest()
        directory.mkdir(parents=True)
        previous = jar("preserve existing cache until a verified replacement exists")
        cached = directory / "installer.jar"
        cached.write_bytes(previous)
        with (self.no_network(), patch.object(lifecycle, "run") as java,
              preparation.environment(self.progress, cache=self.cache),
              self.assertRaisesRegex(preparation.DownloadError, "Встроенный комплект.*повреждён")):
            Neoforge().install("1.21.1", str(self.cache), {}, "java", "21.1.267")
        java.assert_not_called()
        self.assertEqual(cached.read_bytes(), previous)
        self.assertEqual(list(self.root.rglob(".blockharbor-download-*")), [])

    def test_independent_sha256_is_checked_even_when_size_and_official_sha1_match(self):
        source = self.objects / self.library["object"]
        self.library["object"] = self.library["sha256"] = "0" * 64
        source.rename(self.objects / self.library["object"])
        self.write_manifest()
        target = self.root / "library.jar"
        target.write_bytes(b"previous valid target")
        with self.no_network(), self.assertRaisesRegex(preparation.DownloadError, "контрольная сумма"):
            self.download(self.library["url"], target, sha1=self.library["sha1"])
        self.assertEqual(target.read_bytes(), b"previous valid target")
        self.assertEqual(list(self.root.rglob(".blockharbor-download-*")), [])

    def test_present_but_incomplete_bundle_is_reported_without_network(self):
        (self.objects / self.library["object"]).unlink()
        target = self.root / "library.jar"
        with self.no_network(), self.assertRaisesRegex(preparation.DownloadError, "не найден bus-8.0.5.jar"):
            self.download(self.library["url"], target, sha1=self.library["sha1"])
        self.assertFalse(target.exists())

    def test_truncated_bundle_is_rejected_before_creating_download_target(self):
        (self.objects / self.library["object"]).write_bytes(self.library_bytes[:-1])
        target = self.root / "library.jar"
        with self.assertRaisesRegex(bundled_loaders.BundleError, "размер файла"):
            bundled_loaders.copy_file(self.library["url"], target, self.library["sha1"])
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.rglob(".blockharbor-download-*")), [])

    def test_source_checkout_without_binary_bundle_preserves_network_fallback(self):
        shutil.rmtree(self.app / "bundled")
        self.assertIsNone(bundled_loaders.verified_source(self.installer["url"], self.installer["sha1"]))
        self.assertEqual(bundled_loaders.distribution_sources(), [])
        self.manifest.unlink()
        self.assertIsNone(bundled_loaders.load_manifest())
        self.assertEqual(bundled_loaders.entries(), [])
        self.assertEqual(bundled_loaders.distribution_sources(), [])
        self.assertFalse(bundled_loaders.copy_file(self.library["url"], self.root / "library.jar", self.library["sha1"]))

    def test_source_distributions_and_notices_cannot_be_used_as_runtime_inputs(self):
        source = self.entry("source", "net/neoforged/bus/8.0.5/bus-8.0.5-sources.jar", jar("published source"))
        notice = {"kind": "notice", "name": "Fixture License", "text": "Fixture copyright notice."}
        self.data["files"].extend((source, notice))
        self.write_manifest()
        self.assertEqual(bundled_loaders.entries(), [self.installer, self.library])
        self.assertIsNone(bundled_loaders.verified_source(source["url"], source["sha1"]))

    def test_unrelated_mojang_assets_do_not_depend_on_bundle_manifest(self):
        self.manifest.write_text("broken bundle metadata")
        self.assertFalse(bundled_loaders.copy_file(
            "https://resources.download.minecraft.net/aa/" + "a" * 40,
            self.root / "asset", "a" * 40))
        self.assertFalse((self.root / "asset").exists())

    def test_invalid_lock_entries_fail_closed_including_object_traversal_and_host_changes(self):
        changes = [
            {"path": "../outside/library.jar"}, {"object": "../../outside.jar"},
            {"sha256": "Z" * 64}, {"sha1": "bad"}, {"size": True}, {"size": -1},
            {"size": bundled_loaders.MAX_FILE + 1}, {"kind": []},
            {"url": "https://maven.neoforged.net:bad/releases/" + self.library["path"]},
            {"url": self.library["url"].replace("maven.neoforged.net", "example.invalid")},
            {"url": self.library["url"] + "?url=other"},
        ]
        pristine = copy.deepcopy(self.data)
        for changeset in changes:
            with self.subTest(changes=changeset):
                self.data = copy.deepcopy(pristine)
                self.data["files"][1].update(changeset)
                self.write_manifest()
                with self.assertRaises(bundled_loaders.BundleError):
                    bundled_loaders.verified_source(self.installer["url"], self.installer["sha1"])
        self.assertEqual(sorted(self.objects.iterdir()),
                         sorted((self.objects / self.installer["object"], self.objects / self.library["object"])))

    def test_ambiguous_or_wrong_version_manifest_is_rejected(self):
        pristine = copy.deepcopy(self.data)
        variants = [
            {"files": [self.installer, self.library, self.library]},
            {"files": [self.library]}, {"loaderVersion": "21.1.999"},
            {"directory": "../../outside"}, {"format": True},
        ]
        for updates in variants:
            with self.subTest(updates=updates):
                self.data = copy.deepcopy(pristine)
                self.data.update(updates)
                self.write_manifest()
                with self.assertRaises(bundled_loaders.BundleError):
                    bundled_loaders.load_manifest()

    def test_linked_object_or_manifest_cannot_substitute_external_files(self):
        source = self.objects / self.library["object"]
        outside = self.root / "outside.jar"
        outside.write_bytes(self.library_bytes)
        source.unlink()
        try:
            source.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(bundled_loaders.BundleError, "перенаправлен"):
            bundled_loaders.verified_source(self.library["url"], self.library["sha1"])
        self.assertEqual(outside.read_bytes(), self.library_bytes)
        outside_manifest = self.root / "outside.json"
        self.manifest.rename(outside_manifest)
        self.manifest.symlink_to(outside_manifest)
        with self.assertRaisesRegex(bundled_loaders.BundleError, "перенаправлен"):
            bundled_loaders.load_manifest()

    def test_linked_bundle_directory_is_rejected_even_with_matching_bytes(self):
        outside = self.root / "outside-objects"
        self.objects.rename(outside)
        try:
            self.objects.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        target = self.root / "library.jar"
        with self.assertRaisesRegex(bundled_loaders.BundleError, "перенаправлен"):
            bundled_loaders.copy_file(self.library["url"], target, self.library["sha1"])
        self.assertFalse(target.exists())
        self.assertEqual((outside / self.library["object"]).read_bytes(), self.library_bytes)

    def test_source_replaced_between_lookup_and_open_cannot_be_committed(self):
        source = self.objects / self.library["object"]
        real_open = os.open

        def changed(path, *args, **kwargs):
            if Path(path) == source:
                source.write_bytes(bytes(byte ^ 1 for byte in self.library_bytes))
            return real_open(path, *args, **kwargs)

        target = self.root / "library.jar"
        target.write_bytes(b"old target")
        with patch.object(bundled_loaders.os, "open", side_effect=changed), self.assertRaises(bundled_loaders.BundleError):
            bundled_loaders.copy_file(self.library["url"], target, self.library["sha1"])
        self.assertEqual(target.read_bytes(), b"old target")
        self.assertEqual(list(self.root.rglob(".blockharbor-download-*")), [])

    def test_cancellation_after_verified_copy_prevents_publication_and_removes_temporary_file(self):
        scope = lifecycle.CancellationScope()
        consume = bundled_loaders._consume

        def cancel_after_copy(*args, **kwargs):
            consume(*args, **kwargs)
            scope.cancel()

        target = self.root / "library.jar"
        target.write_bytes(b"previous target")
        with (lifecycle.bind(scope), patch.object(bundled_loaders, "_consume", side_effect=cancel_after_copy),
              self.assertRaises(lifecycle.OperationCancelled)):
            bundled_loaders.copy_file(self.library["url"], target, self.library["sha1"])
        self.assertEqual(target.read_bytes(), b"previous target")
        self.assertEqual(list(self.root.rglob(".blockharbor-download-*")), [])

    def test_distribution_sources_checks_all_original_archives_and_locked_generated_notices_without_http(self):
        source, notice = self.add_documentation()
        with self.no_network():
            rows = bundled_loaders.distribution_sources()
        self.assertEqual([row["kind"] for row in rows], ["source", "notice"])
        self.assertEqual([row["name"] for row in rows], [
            "Fixture-" + source["sha256"][:12] + ".zip", "THIRD-PARTY-NOTICES.txt",
        ])
        self.assertEqual([row["path"] for row in rows], [self.objects / source["object"], notice])
        for row in rows:
            data = row["path"].read_bytes()
            self.assertEqual(row["size"], len(data))
            self.assertEqual(row["sha1"], hashlib.sha1(data).hexdigest())
            self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
        # A source archive with an official-looking URL remains ineligible for
        # normal library/installer resolution even after redistribution.
        self.assertIsNone(bundled_loaders.verified_source(source["url"], source["sha1"]))

    def test_distribution_sources_never_returns_an_incomplete_corrupt_or_unlocked_set(self):
        source, notice = self.add_documentation()
        source_file = self.objects / source["object"]
        source_bytes, notice_bytes = source_file.read_bytes(), notice.read_bytes()
        pristine = copy.deepcopy(self.data)
        for fault in ("missing source", "changed source", "changed notice", "missing notice", "unlocked notice"):
            with self.subTest(fault=fault):
                self.data = copy.deepcopy(pristine)
                source_file.write_bytes(source_bytes)
                notice.write_bytes(notice_bytes)
                if fault == "missing source":
                    source_file.unlink()
                elif fault == "changed source":
                    source_file.write_bytes(bytes(byte ^ 1 for byte in source_bytes))
                elif fault == "changed notice":
                    notice.write_bytes(bytes(byte ^ 1 for byte in notice_bytes))
                elif fault == "missing notice":
                    notice.unlink()
                else:
                    self.data.pop("documentation")
                self.write_manifest()
                with self.no_network(), self.assertRaises(bundled_loaders.BundleError):
                    bundled_loaders.distribution_sources()

    def test_distribution_sources_rejects_redirected_notices_and_source_object_paths(self):
        source, notice = self.add_documentation()
        outside = self.root / "outside-notices.txt"
        notice.rename(outside)
        try:
            notice.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(bundled_loaders.BundleError, "перенаправлены"):
            bundled_loaders.distribution_sources()
        source["object"] = "../../outside-notices.txt"
        self.write_manifest()
        with self.assertRaisesRegex(bundled_loaders.BundleError, "недопустимый путь"):
            bundled_loaders.distribution_sources()
        self.assertEqual(outside.read_bytes(), b"Original upstream copyright and license notices.\n")

    def test_distribution_sources_requires_source_archive_for_a_present_package(self):
        with self.assertRaisesRegex(bundled_loaders.BundleError, "отсутствуют исходники"):
            bundled_loaders.distribution_sources()

    def test_distribution_sources_cancellation_does_not_return_a_partially_verified_set(self):
        self.add_documentation()
        scope = lifecycle.CancellationScope()
        consume = bundled_loaders._consume

        def cancel_after_source(*args, **kwargs):
            consume(*args, **kwargs)
            scope.cancel()

        with (lifecycle.bind(scope), patch.object(bundled_loaders, "_consume", side_effect=cancel_after_source),
              self.assertRaises(lifecycle.OperationCancelled)):
            bundled_loaders.distribution_sources()


if __name__ == "__main__":
    unittest.main()

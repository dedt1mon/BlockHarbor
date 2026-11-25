"""NeoForge installer recovery across provider temp directories and offline starts."""

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import lifecycle, preparation


def jar(contents):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("installer.txt", contents)
    return output.getvalue()


class NeoForgeInstallerTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory).resolve()
        self.cache = self.root / "game-cache"
        self.progress = preparation.Progress(lambda _: None)
        self.url = preparation._NEO_ORIGIN + "net/neoforged/neoforge/21.1.999/neoforge-21.1.999-installer.jar"
        self.payload = jar("trusted installer")
        self.digest = hashlib.sha1(self.payload).hexdigest()
        self.calls = []

    def receive(self, url, output, limit, notify=lambda *_: None, *, deadline=None):
        self.calls.append(url)
        output.write((self.digest + "\n").encode() if url.endswith(".sha1") else self.payload)
        return {}, url

    def downloader(self):
        return preparation.Download(self.progress, cache=self.cache)

    def test_installer_survives_failed_java_and_a_new_preparation_environment(self):
        from minecraft_launcher_lib.mod_loader._neoforge import Neoforge

        self.cache.mkdir()  # game._install prepares the vanilla directory first
        provider = Neoforge()
        first_path = []

        def failed_java(command, **kwargs):
            path = Path(command[command.index("-jar") + 1])
            self.assertEqual(path.read_bytes(), self.payload)
            first_path.append(path)
            raise subprocess.CalledProcessError(1, command, stderr=b"fixture dependency outage")

        with (patch.object(preparation, "_receive", side_effect=self.receive),
              patch.object(lifecycle, "run", side_effect=failed_java),
              preparation.environment(self.progress, cache=self.cache),
              self.assertRaises(subprocess.CalledProcessError)):
            provider.install("1.21.1", str(self.cache), {}, "java", "21.1.999")
        self.assertFalse(first_path[0].exists())  # provider removed the first tempdir
        self.assertEqual(self.calls, [self.url + ".sha1", self.url])

        def successful_java(command, **kwargs):
            path = Path(command[command.index("-jar") + 1])
            self.assertNotEqual(path, first_path[0])
            self.assertEqual(path.read_bytes(), self.payload)
            return subprocess.CompletedProcess(command, 0)

        with (patch.object(preparation, "_receive", side_effect=AssertionError("network during cached retry")),
              patch.object(preparation, "_receive_requests", side_effect=AssertionError("alternate network during cached retry")),
              patch.object(lifecycle, "run", side_effect=successful_java),
              preparation.environment(self.progress, cache=self.cache)):
            provider.install("1.21.1", str(self.cache), {}, "java", "21.1.999")

    def test_reviewed_seed_recovers_cold_origin_outage_and_rejects_foreign_valid_target(self):
        relative = "net/neoforged/neoforge/21.1.267/neoforge-21.1.267-installer.jar"
        self.assertEqual(preparation._NEO_INSTALLER_SHA1[relative], "4565232f0bf231d0edfe512b949664dc5ff88fd8")
        original = preparation._NEO_ORIGIN + relative
        target = self.root / "vendor.jar"
        target.write_bytes(jar("unrelated but structurally valid ZIP"))

        def alternate(url, output, limit, notify=lambda *_: None, *, deadline=None):
            self.calls.append(url)
            if url == original:
                raise TimeoutError("origin unavailable")
            self.assertEqual(url, preparation._NEO_INSTALLER_MIRROR + relative)
            output.write(self.payload)
            return {}, url

        with (patch.dict(preparation._NEO_INSTALLER_SHA1, {relative: self.digest}),
              patch.object(preparation, "_installer_checksum", side_effect=AssertionError("seed must work without origin sidecar")),
              patch.object(preparation, "_receive", side_effect=TimeoutError("origin unavailable")),
              patch.object(preparation, "_receive_requests", side_effect=alternate),
              patch.object(lifecycle, "wait")):
            self.assertTrue(self.downloader()(original, target))
            self.assertFalse(self.downloader()(original, target))
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(self.calls, [original, preparation._NEO_INSTALLER_MIRROR + relative])

    def test_corrupt_cache_is_repaired_and_identical_vendor_filenames_do_not_cross_pins(self):
        target = self.root / "first" / "neoforge-installer.jar"
        with patch.object(preparation, "_receive", side_effect=self.receive):
            self.downloader()(self.url, target, sha1=self.digest)
        cached = list(self.cache.rglob("installer.jar"))
        self.assertEqual(len(cached), 1)
        cached[0].write_bytes(jar("corrupt cache with a valid ZIP directory"))
        self.calls.clear()
        other_target = self.root / "second" / target.name
        with patch.object(preparation, "_receive", side_effect=self.receive):
            self.assertTrue(self.downloader()(self.url, other_target))
        self.assertEqual(self.calls, [self.url])  # trusted receipt needs no new sidecar
        self.assertEqual(cached[0].read_bytes(), self.payload)
        self.assertEqual(other_target.read_bytes(), self.payload)
        next_pin = self.url.replace("21.1.999", "21.1.998")
        self.calls.clear()
        with patch.object(preparation, "_receive", side_effect=self.receive):
            self.assertTrue(self.downloader()(next_pin, self.root / "third" / target.name, sha1=self.digest))
        self.assertEqual(self.calls, [next_pin])
        self.assertEqual(len(list(self.cache.rglob("installer.jar"))), 2)

    def test_wrong_origin_receipt_cannot_authorize_cached_or_mirrored_bytes(self):
        with patch.object(preparation, "_receive", side_effect=self.receive):
            self.downloader()(self.url, self.root / "first.jar", sha1=self.digest)
        receipt = next(self.cache.rglob("source.json"))
        record = json.loads(receipt.read_text())
        record["url"] = self.url.replace("21.1.999", "21.1.998")
        receipt.write_text(json.dumps(record))
        target = self.root / "second.jar"
        with (patch.object(preparation, "_installer_checksum", side_effect=preparation.DownloadError("offline")),
              patch.object(preparation, "_receive", side_effect=TimeoutError) as primary,
              patch.object(preparation, "_receive_requests", side_effect=TimeoutError) as alternate,
              patch.object(lifecycle, "wait"),
              self.assertRaises(preparation.DownloadError)):
            self.downloader()(self.url, target)
        self.assertFalse(target.exists())
        self.assertEqual([call.args[0] for call in primary.call_args_list], [self.url, self.url])
        self.assertEqual([call.args[0] for call in alternate.call_args_list], [self.url])

    def test_bad_mirror_does_not_replace_a_target_or_enter_verified_cache(self):
        target = self.root / "vendor.jar"
        previous = jar("keep existing target")
        target.write_bytes(previous)

        def wrong(url, output, limit, notify=lambda *_: None, *, deadline=None):
            self.calls.append(url)
            if url == self.url:
                raise TimeoutError()
            output.write(jar("wrong mirror"))
            return {}, url

        with (patch.object(preparation, "_receive", side_effect=wrong),
              patch.object(preparation, "_receive_requests", side_effect=wrong),
              patch.object(lifecycle, "wait"),
              self.assertRaises(preparation.DownloadError)):
            self.downloader()(self.url, target, sha1=self.digest)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list(self.cache.rglob("installer.jar")), [])
        self.assertEqual(list(self.root.rglob(".blockharbor-download-*")), [])

    def test_redirected_checksum_and_bad_tls_never_supply_mirror_authority(self):
        def redirected(url, output, limit, notify=lambda *_: None, *, deadline=None):
            output.write(self.digest.encode())
            return {}, preparation._NEO_INSTALLER_MIRROR + "untrusted.sha1"

        with (patch.object(preparation, "_receive", side_effect=redirected),
              patch.object(preparation, "_receive_requests", side_effect=redirected),
              patch.object(lifecycle, "wait"),
              self.assertRaises(preparation.DownloadError)):
            preparation._installer_checksum(self.url, deadline=preparation.time.monotonic() + 60)
        error = preparation.URLError(preparation.ssl.SSLCertVerificationError("untrusted certificate"))
        with (patch.object(preparation, "_receive", side_effect=error) as primary,
              patch.object(preparation, "_receive_requests") as alternate,
              self.assertRaises(preparation.DownloadError)):
            preparation._installer_checksum(self.url, deadline=preparation.time.monotonic() + 60)
        primary.assert_called_once()
        alternate.assert_not_called()

    def test_redirected_cache_is_rejected_without_touching_other_files(self):
        self.cache.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep")
        try:
            (self.cache / "installer-cache").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        with (patch.object(preparation, "_receive", side_effect=AssertionError("network")),
              self.assertRaisesRegex(preparation.DownloadError, "перенаправлен")):
            self.downloader()(self.url, self.root / "vendor.jar", sha1=self.digest)
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(list(outside.iterdir()), [marker])


if __name__ == "__main__":
    unittest.main()

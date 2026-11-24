import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import lifecycle, preparation


class ProcessorDownloadsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.payload = b"official mappings bytes\n"
        self.sha = hashlib.sha1(self.payload).hexdigest()
        self.url = "https://piston-data.mojang.com/v1/objects/" + self.sha + "/client.txt"
        self.version = "1.20.1"
        self.output = self.root / "libraries/net/minecraft/client/1.20.1/client-1.20.1-mappings.txt"
        self.manifest = self.root / "versions/1.20.1/1.20.1.json"
        self.manifest.parent.mkdir(parents=True)
        self.metadata = {"id": self.version, "downloads": {"client_mappings": {
            "url": self.url, "sha1": self.sha, "size": len(self.payload)}}}
        self.write_manifest()
        self.messages = []
        self.download = preparation.Download(preparation.Progress(self.messages.append))
        self.addCleanup(self.download.close)
        self.adapter = preparation._Processors(check_default=True, downloader=self.download, cache=self.root)

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.metadata), encoding="utf-8")

    def command(self, vendor="minecraftforge"):
        return ["java", "-cp", "installertools.jar", f"net.{vendor}.installertools.ConsoleTool",
                "--task", "DOWNLOAD_MOJMAPS", "--version", self.version,
                "--side", "client", "--output", str(self.output)]

    def receive(self, url, output, limit, notify):
        self.assertEqual(url, self.url)
        output.write(self.payload)

    def test_both_publishers_raw_task_uses_sha_verified_download_and_reuses_offline(self):
        self.metadata["downloads"]["client_mappings"]["sha1"] = self.sha.upper()
        self.write_manifest()
        with (patch.object(self.download, "_receive_asset", side_effect=self.receive) as receive,
              patch.object(lifecycle, "run", side_effect=AssertionError("Java must not download raw mappings"))):
            for vendor in ("minecraftforge", "neoforged"):
                result = self.adapter.run(self.command(vendor), check=True, stdout=-1, stderr=-1)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(self.output.read_bytes(), self.payload)
            self.assertEqual(receive.call_count, 1)

    def test_corrupt_source_preserves_previous_output_and_never_reports_success(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b"previous output")
        def corrupt(_url, output, _limit, _notify):
            output.write(b"different mapping bytes")
        with (patch.object(self.download, "_receive_asset", side_effect=corrupt),
              patch.object(lifecycle, "wait"),
              patch.object(lifecycle, "run", side_effect=AssertionError("must not fall through on corrupt data")),
              self.assertRaises(preparation.DownloadError)):
            self.adapter.run(self.command())
        self.assertEqual(self.output.read_bytes(), b"previous output")
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_unknown_flags_and_processors_still_execute_original_java(self):
        commands = [self.command() + ["--sanitize"], self.command() + ["--format", "TSRG"],
                    ["java", "-jar", "installer.jar"],
                    [part.replace("DOWNLOAD_MOJMAPS", "MERGE_MAPPING") for part in self.command()],
                    [part.replace("net.minecraftforge", "unrelated.vendor") for part in self.command()]]
        with (patch.object(self.download, "_receive_asset", side_effect=AssertionError("not a raw download")),
              patch.object(lifecycle, "run", return_value=Mock(returncode=0)) as run):
            for command in commands:
                with self.subTest(command=command):
                    self.adapter.run(command, check=True)
                    self.assertEqual(run.call_args.args[0], [command[0], "-Djava.net.useSystemProxies=true", *command[1:]])
            self.assertEqual(run.call_count, len(commands))

    def test_bad_target_or_untrusted_manifest_is_rejected_before_network_or_java(self):
        with (patch.object(self.download, "_receive_asset", side_effect=AssertionError("must reject")),
              patch.object(lifecycle, "run", side_effect=AssertionError("must reject"))):
            command = self.command()
            command[-1] = str(self.root.parent / "outside-mappings.txt")
            with self.assertRaisesRegex(preparation.DownloadError, "путь"):
                self.adapter.run(command)
            original = dict(self.metadata["downloads"]["client_mappings"])
            for key, value in (("url", "https://piston-data.mojang.com.evil.test/file"),
                               ("url", "http://piston-data.mojang.com/file"),
                               ("sha1", "not-a-hash"), ("size", -1)):
                with self.subTest(key=key, value=value):
                    self.metadata["downloads"]["client_mappings"] = {**original, key: value}
                    self.write_manifest()
                    with self.assertRaisesRegex(preparation.DownloadError, "официальных"):
                        self.adapter.run(self.command())
            self.metadata["downloads"]["client_mappings"] = original
            self.metadata["id"] = "1.19.4"
            self.write_manifest()
            with self.assertRaises(preparation.DownloadError):
                self.adapter.run(self.command())

    def test_cancelled_mapping_download_does_not_publish_or_report_success(self):
        scope = lifecycle.CancellationScope()
        def cancelled(_url, stream, _limit, _notify):
            stream.write(self.payload)
            scope.cancel()
        with (lifecycle.bind(scope),
              patch.object(self.download, "_receive_asset", side_effect=cancelled),
              self.assertRaises(lifecycle.OperationCancelled)):
            self.adapter.run(self.command())
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_processor_error_explains_observed_cause_without_echoing_secrets(self):
        for captured, phrase in ((b"SSLHandshakeException PKIX path building failed", "сертификат"),
                                 (b"java.net.UnknownHostException", "DNS"),
                                 (b"java.lang.OutOfMemoryError", "памяти"),
                                 (b"patch processor failed", "остановлена")):
            with self.subTest(captured=captured):
                failure = preparation.subprocess.CalledProcessError(
                    1, ["java", "--private=secret"], output=b"https://user:secret@example.test",
                    stderr=captured)
                message = preparation.processor_diagnostic(failure)
                self.assertIn(phrase, message)
                self.assertNotIn("secret", message)
                self.assertNotIn("example.test", message)


if __name__ == "__main__":
    unittest.main()

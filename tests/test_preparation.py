import hashlib
import importlib
import io
import lzma
import os
import socket
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from launcher import lifecycle, preparation


class Source(BaseHTTPRequestHandler):
    calls: ClassVar = Counter()
    payload = b"Verified managed runtime bytes." * 4096

    def log_message(self, *_):
        pass

    def do_GET(self):
        Source.calls[self.path] += 1
        if self.path == "/refused":
            self.send_error(403)
            return
        if self.path == "/down":
            self.send_error(503)
            return
        body = (
            lzma.compress(self.payload)
            if self.path in ("/compressed", "/lzma-broken")
            else self.payload
        )
        if self.path == "/lzma-broken" and Source.calls[self.path] == 1:
            body = body[:-15]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.path == "/broken" or (
            self.path == "/resume" and self.calls[self.path] == 1
        ):
            self.wfile.write(body[:19])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.wfile.write(body)


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.messages = []
        self.progress = preparation.Progress(self.messages.append)
        self.file = self.root / "runtime.dll"
        self.sha = hashlib.sha1(Source.payload).hexdigest()
        Source.calls.clear()

    def test_dropped_download_retries_atomically_reuses_verified_bytes(self):
        self.file.write_bytes(b"previous cache")
        with patch.object(preparation.time, "sleep"):
            self.assertTrue(
                preparation.Download(self.progress)(
                    self.base + "/resume", self.file, sha1=self.sha
                )
            )
        self.assertEqual(Source.calls["/resume"], 2)
        self.assertEqual(self.file.read_bytes(), Source.payload)
        self.assertTrue(any("Повтор" in text for text in self.messages))
        self.assertTrue(any("КБ" in text for text in self.messages))
        with patch.object(
            preparation, "urlopen", side_effect=AssertionError("network on cached Play")
        ):
            self.assertFalse(
                preparation.Download(self.progress)(
                    self.base + "/resume", self.file, sha1=self.sha
                )
            )
        self.assertEqual(list(self.root.iterdir()), [self.file])

    def test_finite_retries_and_preserved_original_for_partial_checksum_and_http(self):
        self.file.write_bytes(b"original")
        for endpoint, digest, expected in [
            ("/broken", self.sha, 3),
            ("/down", self.sha, 3),
            ("/refused", self.sha, 1),
            ("/ok", "0" * 40, 3),
        ]:
            with (
                self.subTest(endpoint=endpoint),
                patch.object(preparation.time, "sleep"),
                self.assertRaises(preparation.DownloadError),
            ):
                preparation.Download(self.progress)(
                    self.base + endpoint, self.file, sha1=digest
                )
            self.assertEqual(Source.calls[endpoint], expected)
            self.assertEqual(self.file.read_bytes(), b"original")
            self.assertEqual(list(self.root.iterdir()), [self.file])

    def test_neoforge_fallback_uses_exact_coordinate_and_original_checksum(self):
        original = "https://maven.neoforged.net/releases/net/neoforged/neoform/1/neoform-1.zip"
        target = self.root / "neoform.zip"
        target.write_bytes(b"old valid bytes must not be replaced by bad mirror")
        primary = Mock(side_effect=TimeoutError("CDN timed out"))
        seen = []

        def fallback(url, output, limit, notify=lambda *_: None):
            seen.append(url)
            if url == original:
                raise ConnectionError("alternate transport failed")
            if "creeperhost" in url:
                output.write(b"wrong bytes")
            else:
                self.assertEqual(target.read_bytes(), b"old valid bytes must not be replaced by bad mirror")
                output.write(Source.payload)

        with (patch.object(preparation, "_receive", primary),
              patch.object(preparation, "_receive_requests", side_effect=fallback),
              patch.object(lifecycle, "wait")):
            self.assertTrue(preparation.Download(self.progress)(original, target, sha1=self.sha))
        self.assertEqual(seen, [
            original,
            "https://maven.creeperhost.net/net/neoforged/neoform/1/neoform-1.zip",
            "https://mirrors.nju.edu.cn/bmclapi/net/neoforged/neoform/1/neoform-1.zip",
        ])
        self.assertEqual(target.read_bytes(), Source.payload)
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_neoforge_mirrors_are_not_used_without_an_authoritative_checksum(self):
        original = "https://maven.neoforged.net/releases/net/neoforged/bus/1/bus-1.jar"
        target = self.root / "bus.jar"
        with (patch.object(preparation, "_receive", side_effect=TimeoutError) as first,
              patch.object(preparation, "_receive_requests", side_effect=ConnectionError) as alternate,
              patch.object(lifecycle, "wait"),
              self.assertRaisesRegex(preparation.DownloadError, "bus-1.jar")):
            preparation.Download(self.progress)(original, target)
        self.assertEqual(first.call_count, 2)
        self.assertEqual(alternate.call_count, 1)
        self.assertEqual(alternate.call_args.args[0], original)
        self.assertFalse(target.exists())

    def test_neoforge_installer_uses_official_checksum_for_fallback(self):
        original = "https://maven.neoforged.net/releases/net/neoforged/neoforge/21.1.999/neoforge-21.1.999-installer.jar"
        target = self.root / "installer.jar"
        seen = []

        def fallback(url, output, limit, notify=lambda *_: None, *, deadline=None):
            seen.append(url)
            self.assertIsNotNone(deadline)
            if url == original + ".sha1":
                output.write((self.sha.upper() + "\n").encode())
                return {}, url
            if url == original:
                raise ConnectionError()
            output.write(Source.payload)
            return {}, url

        with (patch.object(preparation, "_receive", side_effect=TimeoutError) as primary,
              patch.object(preparation, "_receive_requests", side_effect=fallback),
              patch.object(lifecycle, "wait")):
            preparation.Download(self.progress)(original, target)
        self.assertEqual(primary.call_args_list[0].args[0], original + ".sha1")
        self.assertNotIn("quick_catalog", primary.call_args_list[0].kwargs)
        self.assertEqual(seen, [original + ".sha1", original,
                               original.replace(preparation._NEO_ORIGIN, preparation._NEO_INSTALLER_MIRROR)])
        self.assertEqual(target.read_bytes(), Source.payload)

    def test_neoforge_bad_mirror_hashes_fail_closed(self):
        original = "https://maven.neoforged.net/releases/net/neoforged/bus/1/bus-1.jar"
        self.file.write_bytes(b"original")

        def wrong(url, output, limit, notify=lambda *_: None):
            output.write(b"wrong")

        with (patch.object(preparation, "_receive", side_effect=TimeoutError),
              patch.object(preparation, "_receive_requests", side_effect=wrong) as fallback,
              patch.object(lifecycle, "wait"),
              self.assertRaises(preparation.DownloadError)):
            preparation.Download(self.progress)(original, self.file, sha1=self.sha)
        self.assertEqual(fallback.call_count, 4)
        self.assertEqual(self.file.read_bytes(), b"original")
        self.assertEqual(list(self.root.iterdir()), [self.file])

    def test_neoforge_catalog_uses_alternate_transport_and_canonical_url(self):
        original = "https://maven.neoforged.net//net/neoforged/neoforge/maven-metadata.xml"
        canonical = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"

        def fallback(url, output, limit):
            self.assertEqual(url, canonical)
            output.write(b'{"ok":true}')
            return {}, url

        with (patch.object(preparation, "_receive", side_effect=TimeoutError) as first,
              patch.object(preparation, "_receive_requests", side_effect=fallback),
              patch.object(lifecycle, "wait")):
            self.assertEqual(preparation.metadata_response(original).json(), {"ok": True})
        self.assertEqual(first.call_args.args[0], canonical)
        self.assertEqual(preparation._neo_routes("https://other.example/releases/a/b/c/d", self.sha),
                         [("https://other.example/releases/a/b/c/d", False)] * preparation.ATTEMPTS)

    def test_neoforge_mirror_routing_rejects_noncanonical_or_unsafe_coordinates(self):
        for address in (
            "http://maven.neoforged.net/releases/net/neoforged/bus/1/a.jar",
            "https://maven.neoforged.net.evil.example/releases/net/neoforged/bus/1/a.jar",
            "https://maven.neoforged.net/releases/net/neoforged/../1/a.jar",
            "https://maven.neoforged.net/releases/net/neoforged/bus/1/a.jar?token=secret",
            "https://maven.neoforged.net/snapshots/net/neoforged/bus/1/a.jar",
        ):
            with self.subTest(address=address):
                self.assertEqual(preparation._neo_routes(address, self.sha),
                                 [(address, False)] * preparation.ATTEMPTS)

    def test_alternate_transport_validates_length_and_http_status(self):
        data = io.BytesIO()
        headers, url = preparation._receive_requests(self.base + "/ok", data, len(Source.payload))
        self.assertEqual(url, self.base + "/ok")
        self.assertEqual(data.getvalue(), Source.payload)
        with self.assertRaises((ConnectionError, preparation._Incomplete)):
            preparation._receive_requests(self.base + "/broken", io.BytesIO(), len(Source.payload))
        with self.assertRaises(HTTPError) as error:
            preparation._receive_requests(self.base + "/refused", io.BytesIO(), len(Source.payload))
        self.assertEqual(error.exception.code, 403)

    def test_compressed_java_checksum_applies_to_decompressed_file(self):
        self.assertTrue(
            preparation.Download(self.progress)(
                self.base + "/compressed",
                self.file,
                sha1=self.sha,
                lzma_compressed=True,
            )
        )
        self.assertEqual(self.file.read_bytes(), Source.payload)
        self.assertEqual(list(self.root.iterdir()), [self.file])

    def test_truncated_lzma_retries_and_installs_matching_decompressed_digest(self):
        with patch.object(preparation.time, "sleep"):
            preparation.Download(self.progress)(
                self.base + "/lzma-broken",
                self.file,
                sha1=self.sha,
                lzma_compressed=True,
            )
        self.assertEqual(Source.calls["/lzma-broken"], 2)
        self.assertEqual(self.file.read_bytes(), Source.payload)
        self.assertEqual(list(self.root.iterdir()), [self.file])

    def test_unchanged_java_inventory_reuses_index_and_truncated_dll_is_detected(self):
        component = "java-runtime-gamma"
        root = self.root / "runtime" / component / "windows-x64"
        java = root / component / "bin/java.exe"
        dll = root / component / "bin/server/jvm.dll"
        java.parent.mkdir(parents=True)
        dll.parent.mkdir(parents=True)
        java.write_bytes(b"PE executable")
        dll.write_bytes(b"vm runtime")
        inventory = "".join(
            f"{path.relative_to(root / component)!s} /#// {hashlib.sha1(path.read_bytes()).hexdigest()} 0\n"
            for path in (java, dll)
        )
        (root / (component + ".sha1")).write_text(inventory)
        self.assertIsNone(preparation.runtime_problem(self.root, component, java))
        from launcher import modfiles

        with patch.object(
            modfiles, "file_hash", side_effect=AssertionError("unnecessary hashing")
        ):
            self.assertIsNone(preparation.runtime_problem(self.root, component, java))
        # Corruption that preserves file size and mtime must invalidate the index.
        stamp = dll.stat()
        dll.write_bytes(b"x" * len(b"vm runtime"))
        os.utime(dll, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertIn("jvm.dll", preparation.runtime_problem(self.root, component, java))
        dll.write_bytes(b"")
        self.assertIn(
            "jvm.dll", preparation.runtime_problem(self.root, component, java)
        )
        dll.write_bytes(b"vm runtime")
        self.assertIsNone(preparation.runtime_problem(self.root, component, java))

    def test_interrupted_download_cleanup_preserves_published_and_unrelated_files(self):
        nested = self.root / "assets" / "objects" / "aa"
        nested.mkdir(parents=True)
        discarded = nested / ".blockharbor-download-abc_1234"
        discarded.write_bytes(b"partial")
        user_file = nested / ".blockharbor-download-user-document.txt"
        user_file.write_bytes(b"leave this file")
        verified = nested / ("a" * 40)
        verified.write_bytes(b"verified file")
        self.assertEqual(preparation.cleanup_interrupted_downloads(self.root), 1)
        self.assertFalse(discarded.exists())
        self.assertEqual(user_file.read_bytes(), b"leave this file")
        self.assertEqual(verified.read_bytes(), b"verified file")

    def test_metadata_preserves_system_proxy_environment_and_uses_finite_timeout(self):
        raw = io.BytesIO(b'{"ok":true}')
        raw.headers = {"Content-Length": "11"}
        raw.geturl = lambda: "https://metadata.example.org/catalog"
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://127.0.0.1:12345",
                "NO_PROXY": "localhost,127.0.0.1",
            },
        ):
            before = dict(os.environ)
            with patch.object(preparation, "urlopen", return_value=raw) as call:
                self.assertEqual(
                    preparation.json_document("https://metadata.example.org/catalog"),
                    {"ok": True},
                )
            self.assertEqual(call.call_args.kwargs["timeout"], 25)
            self.assertEqual(dict(os.environ), before)

    def test_legacy_system_java_reuses_only_verified_older_major_from_path(self):
        executable = self.root / "java.exe"
        executable.write_bytes(b"MZ")
        for version, expected in [("1.8.0_432", str(executable)), ("21.0.7", None)]:
            preparation._java_major.cache_clear()
            with (
                patch.object(preparation.shutil, "which", return_value=str(executable)),
                patch.object(
                    preparation.subprocess,
                    "run",
                    return_value=Mock(
                        returncode=0, stdout="", stderr=f'java version "{version}"'
                    ),
                ) as run,
            ):
                self.assertEqual(preparation.legacy_system_java(), expected)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], [str(executable), "-version"])
                self.assertEqual(run.call_args.kwargs["timeout"], 8)

    def test_aliases_and_network_methods_restore_after_failure(self):
        import minecraft_launcher_lib as mc
        import requests
        from minecraft_launcher_lib import _helper

        old = (
            mc.install.download_file,
            mc.runtime.download_file,
            _helper.download_file,
            mc.install.requests,
            requests.Session.request,
        )
        with (
            self.assertRaisesRegex(ValueError, "test error"),
            preparation.environment(self.progress),
        ):
            self.assertIsInstance(mc.install.download_file, preparation.Download)
            self.assertIs(mc.runtime.download_file, mc.install.download_file)
            self.assertIs(mc.install.requests.get, preparation.metadata_response)
            with mc.install.ThreadPoolExecutor() as workers:
                self.assertEqual(workers._max_workers, 6)
            raise ValueError("test error")
        self.assertEqual(
            (
                mc.install.download_file,
                mc.runtime.download_file,
                _helper.download_file,
                mc.install.requests,
                requests.Session.request,
            ),
            old,
        )

    def test_progress_updates_the_launcher_without_spawning_a_console(self):
        scope = lifecycle.CancellationScope()
        with lifecycle.bind(scope), patch.object(
            preparation.subprocess, "Popen", side_effect=AssertionError("progress spawned a console")
        ) as spawn:
            self.assertFalse(self.progress.started)
            self.progress.start()
            self.progress.start()
            self.progress.downloaded("runtime.dll", 1024**2, 2 * 1024**2)
            callbacks = self.progress.callbacks()
            callbacks["setStatus"]("Running fabric installer")
            callbacks["setMax"](3)
            callbacks["setProgress"](4)
            self.progress.close()
            spawn.assert_not_called()
            self.assertEqual(self.messages.count("Подготовка файлов Minecraft и Java…"), 1)
            self.assertTrue(any("1.0 МБ / 2.0 МБ" in text for text in self.messages))
            self.assertIn("Настраиваем Fabric · 4 / 4", self.messages)
            before = list(self.messages)
            scope.cancel()
            with self.assertRaises(lifecycle.OperationCancelled):
                self.progress("must not arrive after close")
            self.assertEqual(self.messages, before)

    def test_all_installer_adapters_restore_and_keep_callers_error_policy(self):
        names = ("fabric", "quilt", "forge", "runtime", "java_utils", "mod_loader._fabric", "mod_loader._quilt", "mod_loader._forge", "mod_loader._neoforge")
        modules = [importlib.import_module("minecraft_launcher_lib." + name) for name in names]
        originals = [module.subprocess for module in modules]
        command = ["/runtime/java.exe", "-jar", "installer.jar", "client"]
        completed = preparation.subprocess.CompletedProcess(command, 3, b"detail", b"failure")
        with preparation.environment(self.progress), patch.object(lifecycle, "run", return_value=completed) as run:
            for module in modules:
                with self.subTest(module=module.__name__):
                    self.assertIsInstance(module.subprocess, preparation._Processors)
                    # Legacy Fabric/Quilt inspect returncode themselves; preserve
                    # explicit check=False and captured logs for every provider.
                    result = module.subprocess.run(command, check=False, capture_output=True, timeout=12)
                    self.assertIs(result, completed)
                    self.assertEqual(run.call_args.args[0][1], "-Djava.net.useSystemProxies=true")
                    self.assertEqual(run.call_args.kwargs, {"check": False, "capture_output": True, "timeout": 12})
            modules[0].subprocess.run(command)
            self.assertFalse(run.call_args.kwargs["check"])
        self.assertEqual([module.subprocess for module in modules], originals)

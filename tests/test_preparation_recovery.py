"""Real HTTP failures at the NeoForge cache/transport boundary."""

import hashlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from launcher import lifecycle, preparation


class NeoForgeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.payload = bytes(range(256)) * 8192
        self.digest = hashlib.sha1(self.payload).hexdigest()
        self.url = preparation._NEO_ORIGIN + "net/neoforged/neoforge/21.1.999/neoforge-21.1.999-installer.jar"
        self.target = self.root / "vendor" / "installer.jar"
        self.cache = self.root / "game-cache"
        self.progress = preparation.Progress(lambda _: None)
        self.calls = []
        self.mode = "drop-first"
        self.release = threading.Event()
        owner = self

        class Source(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.calls.append((self.path, self.headers.get("Range"), self.headers.get("If-Range")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/file")
                    self.end_headers()
                    return
                if self.path == "/loop":
                    self.send_response(307)
                    self.send_header("Location", "/loop")
                    self.end_headers()
                    return
                if owner.mode == "remaining-down" and len(owner.calls) > 1:
                    self.send_error(503)
                    return
                offset = int(self.headers.get("Range", "bytes=0-").removeprefix("bytes=").split("-", 1)[0])
                first = len(owner.calls) == 1
                ignore_range = owner.mode == "ignore-range" and bool(offset)
                if ignore_range:
                    offset = 0
                length = len(owner.payload) - offset
                if owner.mode == "small-ranges" and offset:
                    length = min(length, 3000)
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(length))
                self.send_header("ETag", '"new"' if owner.mode == "changed-etag" and offset else '"stable-v1"')
                if offset:
                    start = offset + 1 if owner.mode == "bad-range" else offset
                    self.send_header("Content-Range", f"bytes {start}-{offset+length-1}/{len(owner.payload)}")
                self.end_headers()
                if first and owner.mode in {"drop-first", "remaining-down", "bad-range", "ignore-range", "changed-etag", "cancel", "stall", "small-ranges"}:
                    self.wfile.write(owner.payload[:1000 if owner.mode == "small-ranges" else 4096])
                    self.wfile.flush()
                    if owner.mode in {"cancel", "stall"}:
                        owner.release.wait(2)
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                try:
                    self.wfile.write(owner.payload[offset:offset+length])
                except (ConnectionError, OSError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @contextmanager
    def local_routes(self):
        original = preparation._receive
        alternate = preparation._receive_requests

        def receive(receiver):
            def mapped(url, output, limit, notify=lambda *_: None, **kwargs):
                return receiver(self.base + "/file", output, limit, notify, **kwargs)
            return mapped

        with (patch.object(preparation, "_receive", side_effect=receive(original)),
              patch.object(preparation, "_receive_requests", side_effect=receive(alternate)),
              patch.object(lifecycle, "wait")):
            yield

    def download(self):
        downloader = preparation.Download(self.progress, cache=self.cache)
        try:
            return downloader(self.url, self.target, sha1=self.digest)
        finally:
            downloader.close()

    def test_disconnect_resumes_in_alternate_transport_and_publishes_only_verified_file(self):
        with self.local_routes():
            self.assertTrue(self.download())
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.calls, [("/file", None, None), ("/file", "bytes=4096-", '"stable-v1"')])
        self.assertEqual(list(self.cache.rglob("*.part")), [])
        self.assertEqual(list(self.cache.rglob("installer.json")), [])

    def test_resume_survives_failed_launch_and_new_downloader(self):
        self.mode = "remaining-down"
        with self.local_routes(), self.assertRaises(preparation.DownloadError) as failure:
            self.download()
        self.assertIn("часть установщика сохранена", str(failure.exception))
        self.assertFalse(self.target.exists())
        fragment = next(self.cache.rglob("installer.part"))
        self.assertEqual(fragment.read_bytes(), self.payload[:4096])
        self.assertEqual(preparation.cleanup_interrupted_downloads(self.cache), 0)
        self.mode = "normal"
        with self.local_routes():
            self.assertTrue(self.download())
        self.assertEqual(self.calls[-1], ("/file", "bytes=4096-", '"stable-v1"'))
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_valid_short_206_ranges_accumulate_without_discarding_prior_bytes(self):
        self.mode = "small-ranges"
        self.payload = bytes(range(250)) * 40
        self.digest = hashlib.sha1(self.payload).hexdigest()
        with self.local_routes():
            self.assertTrue(self.download())
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual([call[1] for call in self.calls], [None, "bytes=1000-", "bytes=4000-", "bytes=7000-"])
        self.assertEqual(list(self.cache.rglob("*.part")), [])

    def test_ignored_range_restarts_and_bad_range_or_changed_validator_never_appends(self):
        for mode in ("ignore-range", "bad-range", "changed-etag"):
            with self.subTest(mode=mode):
                self.cache = self.root / mode
                self.target = self.root / mode / "vendor.jar"
                self.mode, self.calls = mode, []
                with self.local_routes():
                    self.assertTrue(self.download())
                self.assertEqual(self.target.read_bytes(), self.payload)
                self.assertEqual(self.calls[1][1], "bytes=4096-")
                if mode != "ignore-range":
                    self.assertEqual(self.calls[2][1], None)

    def test_partial_with_another_digest_is_discarded_before_range(self):
        self.mode = "remaining-down"
        with self.local_routes(), self.assertRaises(preparation.DownloadError):
            self.download()
        record = next(self.cache.rglob("installer.json"))
        data = json.loads(record.read_text())
        data["sha1"] = "0" * 40
        record.write_text(json.dumps(data))
        self.mode = "normal"
        with self.local_routes():
            self.assertTrue(self.download())
        self.assertEqual(self.calls[-1][1], None)
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_cancel_keeps_partial_without_publishing_and_next_launch_resumes(self):
        self.mode = "cancel"
        scope = lifecycle.CancellationScope()
        started = threading.Event()
        self.progress = preparation.Progress(lambda text: started.set() if text.startswith("Скачиваем ") else None)
        failures = []

        def work():
            try:
                with lifecycle.bind(scope):
                    self.download()
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=work, daemon=True)
        try:
            with self.local_routes():
                worker.start()
                self.assertTrue(started.wait(3), "body never reached downloader")
                scope.cancel()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), "cancel left the installer transfer blocked")
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], lifecycle.OperationCancelled)
            self.assertFalse(self.target.exists())
            self.release.set()
            self.mode = "normal"
            with self.local_routes():
                self.assertTrue(self.download())
            self.assertEqual(self.calls[-1][1], "bytes=4096-")
        finally:
            scope.cancel()
            self.release.set()
            worker.join(timeout=3)

    def test_both_transports_follow_redirect_and_reject_loop_and_https_downgrade(self):
        self.mode = "normal"
        for receiver in (preparation._receive, preparation._receive_requests):
            with self.subTest(receiver=receiver.__name__):
                output = io.BytesIO()
                _, effective = receiver(self.base + "/redirect", output, len(self.payload))
                self.assertEqual(effective, self.base + "/file")
                self.assertEqual(output.getvalue(), self.payload)
                with self.assertRaisesRegex(preparation._Incomplete, "перенаправлен"):
                    receiver(self.base + "/loop", io.BytesIO(), len(self.payload))
        for location in ("http://cdn.example/file", "file:///secret", "https://user:pass@cdn.example/file", "https://cdn.example/\nfile"):
            with self.subTest(location=location), self.assertRaises(preparation._Incomplete):
                preparation._redirect_target("https://maven.example/file", location)

    def test_stalled_socket_obeys_remaining_body_deadline_in_both_transports(self):
        self.mode = "stall"
        for receiver in (preparation._receive, preparation._receive_requests):
            with self.subTest(receiver=receiver.__name__):
                self.calls = []
                self.release = threading.Event()
                started = time.monotonic()
                try:
                    with self.assertRaises((TimeoutError, preparation._Incomplete)):
                        receiver(self.base + "/file", io.BytesIO(), len(self.payload), deadline=started + 0.25)
                    self.assertLess(time.monotonic() - started, 1.5, "socket outlived the per-attempt budget")
                finally:
                    self.release.set()

    def test_mirror_transport_retry_and_final_diagnostic_keep_actual_failure(self):
        mirror = self.url.replace(preparation._NEO_ORIGIN, preparation._NEO_INSTALLER_MIRROR)
        calls = []

        def primary(url, output, *_args, **_kwargs):
            calls.append((url, "urllib"))
            if url == self.url:
                raise TimeoutError()
            output.write(self.payload)

        def alternate(url, *_args, **_kwargs):
            calls.append((url, "requests"))
            raise HTTPError(url, 302 if url == mirror else 503, "fixture", {}, None)

        with (patch.object(preparation, "_receive", side_effect=primary),
              patch.object(preparation, "_receive_requests", side_effect=alternate),
              patch.object(lifecycle, "wait")):
            self.assertTrue(self.download())
        self.assertEqual(calls[-2:], [(mirror, "requests"), (mirror, "urllib")])
        self.target.unlink()
        for cached in self.cache.rglob("installer.jar"):
            cached.unlink()

        def unavailable(url, *_args, **_kwargs):
            if url == self.url:
                raise TimeoutError()
            raise HTTPError(url, 403, "fixture", {}, None)

        with (patch.object(preparation, "_receive", side_effect=unavailable),
              patch.object(preparation, "_receive_requests", side_effect=unavailable),
              patch.object(lifecycle, "wait"), self.assertRaises(preparation.DownloadError) as error):
            self.download()
        self.assertIn("maven.neoforged.net (urllib): тайм-аут", str(error.exception))
        self.assertIn("neoforged.forgecdn.net (urllib): HTTP 403", str(error.exception))
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(error.exception.url, mirror)

    def test_verified_working_mirror_is_preferred_for_next_dependency_only_in_this_job(self):
        calls = []
        library = preparation._NEO_ORIGIN + "net/neoforged/bus/8.0/bus-8.0.jar"

        def receive(url, output, *_args, **_kwargs):
            calls.append(url)
            if url.startswith(preparation._NEO_ORIGIN):
                raise TimeoutError()
            output.write(self.payload)

        with (patch.object(preparation, "_receive", side_effect=receive),
              patch.object(preparation, "_receive_requests", side_effect=receive),
              patch.object(lifecycle, "wait")):
            downloader = preparation.Download(self.progress, cache=self.cache)
            self.addCleanup(downloader.close)
            downloader(self.url, self.target, sha1=self.digest)
            calls.clear()
            downloader(library, self.root / "bus.jar", sha1=self.digest)
            self.assertEqual(calls, [library.replace(preparation._NEO_ORIGIN, preparation._NEO_INSTALLER_MIRROR)])
            calls.clear()
            another = preparation.Download(self.progress, cache=self.cache)
            self.addCleanup(another.close)
            another(library, self.root / "another-bus.jar", sha1=self.digest)
            self.assertEqual(calls[:2], [library, library])

    def test_slow_trickle_cannot_consume_all_route_budgets(self):
        clock = [0.0]
        budgets = []

        def receive(url, output, *_args, deadline=None, **_kwargs):
            budgets.append((url, deadline - clock[0]))
            if url.startswith(preparation._NEO_ORIGIN):
                clock[0] = deadline
                raise preparation._Incomplete("Загрузка превысила ограничение по времени.")
            output.write(self.payload)

        with (patch.object(preparation, "_receive", side_effect=receive),
              patch.object(preparation, "_receive_requests", side_effect=receive),
              patch.object(preparation.time, "monotonic", side_effect=lambda: clock[0]),
              patch.object(lifecycle, "wait")):
            self.assertTrue(self.download())
        self.assertEqual(len(budgets), 3)
        self.assertTrue(all(0 < duration <= preparation.MAVEN_ATTEMPT_SECONDS for _, duration in budgets))
        self.assertLess(clock[0], preparation.INSTALLER_SECONDS)


if __name__ == "__main__":
    unittest.main()

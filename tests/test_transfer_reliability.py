"""Storage failures, cache reuse and a real interrupted HTTP transfer."""

import errno
import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from launcher import lifecycle, preparation, transfers


class TransferReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.manager = transfers.TransferManager(self.root)
        self.addCleanup(self.manager.close)
        self.payload = b"verified bytes " * 10000
        self.destination = self.root / "content" / "artifact.jar"
        self.url = "https://cdn.modrinth.com/test.jar"
        self.options = {"hosts": {"cdn.modrinth.com"}, "hashes": {"sha256": hashlib.sha256(self.payload).hexdigest()}, "size": len(self.payload)}

    def test_storage_failures_are_actionable_and_never_network_retried(self):
        for error, text in ((OSError(errno.ENOSPC, "full"), "места"), (PermissionError(errno.EACCES, "denied"), "доступа")):
            with self.subTest(error=error), patch.object(self.manager, "_receive", side_effect=error) as receive:
                with self.assertRaisesRegex(ValueError, text):
                    self.manager.download(self.url, self.destination, **self.options)
                self.assertEqual(receive.call_count, 1)
                self.assertEqual(self.manager.snapshot()["tasks"][0]["state"], "failed")

    def test_tls_validation_failure_is_not_retried_as_connectivity(self):
        error = URLError(ssl.SSLCertVerificationError("invalid certificate"))
        with patch.object(transfers, "open_https", side_effect=error) as opened:
            with self.assertRaisesRegex(ValueError, "сертификат") as failure:
                self.manager.download(self.url, self.destination, **self.options)
        self.assertEqual(opened.call_count, 1)
        self.assertNotIsInstance(failure.exception, transfers.TransferNetworkError)

    def test_http_failure_exposes_typed_network_status_for_approved_fallback(self):
        error = HTTPError(self.url, 404, "not found", {}, None)
        with patch.object(transfers, "open_https", side_effect=error):
            with self.assertRaises(transfers.TransferNetworkError) as failure:
                self.manager.download(self.url, self.destination, **self.options)
        self.assertEqual(failure.exception.status_code, 404)

    def test_verified_existing_file_works_without_network(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(self.payload)
        with patch.object(transfers, "open_https", side_effect=AssertionError("unnecessary network")):
            result = self.manager.download(self.url, self.destination, **self.options)
        self.assertTrue(result.samefile(self.destination))
        self.assertEqual(self.manager.snapshot()["tasks"][0]["bytes"], len(self.payload))
        self.assertEqual(self.manager.snapshot()["tasks"][0]["state"], "completed")

    def test_matching_size_cannot_hide_bad_cached_hash(self):
        self.destination.parent.mkdir()
        original = b"x" * len(self.payload)
        self.destination.write_bytes(original)
        with patch.object(transfers, "open_https", side_effect=ValueError("network unavailable")) as opened:
            with self.assertRaisesRegex(ValueError, "network unavailable"):
                self.manager.download(self.url, self.destination, **self.options)
        opened.assert_called_once()
        self.assertEqual(self.destination.read_bytes(), original)

    def test_trickling_peer_hits_total_deadline_and_preserves_destination(self):
        clock = [0.0]
        response = io.BytesIO(self.payload)
        response.headers = {"Content-Length": str(len(self.payload)), "ETag": '"one"'}
        response.getcode = lambda: 200
        read = response.read
        def read1(amount):
            clock[0] += 2
            return read(min(amount, 5))
        response.read1 = read1
        with (patch.object(transfers, "open_https", return_value=response),
              patch.object(transfers, "MAX_TRANSFER_SECONDS", 3),
              patch.object(transfers.time, "monotonic", side_effect=lambda: clock[0])):
            with self.assertRaisesRegex(ValueError, "время загрузки"):
                self.manager.download(self.url, self.destination, **self.options)
        self.assertFalse(self.destination.exists())
        task = self.manager.snapshot()["tasks"][0]
        self.assertEqual(task["attempt"], 1)
        self.assertEqual(task["state"], "failed")

    def test_corrupt_queue_record_does_not_erase_other_resumable_tasks(self):
        task = self.manager._new(self.url, self.destination, "Artifact", self.options["hashes"], len(self.payload), transfers.MAX_BYTES, self.options["hosts"], "content", True, True)
        self.manager.close()
        rows = json.loads(self.manager.index.read_text())
        broken = {**rows[0], "id": "b" * 24, "config": {"destination": "x"}}
        rows.insert(0, broken)
        self.manager.index.write_text(json.dumps(rows))
        restored = transfers.TransferManager(self.root)
        self.addCleanup(restored.close)
        self.assertEqual([row["id"] for row in restored.snapshot()["tasks"]], [task])
        self.assertTrue(restored.snapshot()["tasks"][0]["retryable"])

    def test_real_http_disconnect_resumes_with_range_and_validator(self):
        calls = []
        payload = self.payload
        class Source(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                calls.append((self.headers.get("Range"), self.headers.get("If-Range")))
                start = int(self.headers.get("Range", "bytes=0-").removeprefix("bytes=").split("-", 1)[0])
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(payload) - start))
                self.send_header("ETag", '"verified-v1"')
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.end_headers()
                if len(calls) == 1:
                    self.wfile.write(payload[:4096])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                else:
                    self.wfile.write(payload[start:])
        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def open_local(_url, _hosts, headers=None):
            from urllib.request import Request
            return urlopen(Request(f"http://127.0.0.1:{server.server_port}/file", headers=headers or {}), timeout=3)
        try:
            with patch.object(transfers, "open_https", side_effect=open_local):
                result = self.manager.download(self.url, self.destination, **self.options)
            self.assertEqual(result.read_bytes(), payload)
            self.assertEqual(calls, [(None, None), ("bytes=4096-", '"verified-v1"')])
            self.assertTrue(self.manager.snapshot()["tasks"][0]["resumed"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_parent_operation_cancels_stalled_body_without_closing_manager(self):
        payload = self.payload
        started, release = threading.Event(), threading.Event()
        calls = []
        class Source(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                calls.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", '"cancel-fixture"')
                self.end_headers()
                if len(calls) == 1:
                    self.wfile.write(payload[:1])
                    self.wfile.flush()
                    release.wait(5)
                else:
                    self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        errors = []
        parent = lifecycle.CancellationScope()
        def open_local(_url, _hosts, headers=None):
            from urllib.request import Request
            return urlopen(Request(f"http://127.0.0.1:{server.server_port}/file", headers=headers or {}), timeout=3)
        def download():
            try:
                with lifecycle.bind(parent):
                    self.manager.download(self.url, self.destination, status=lambda _: started.set(), **self.options)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=download, daemon=True)
        aborts = []
        original_abort = lifecycle.abort_response
        def trace_abort(response):
            try:
                sock = response.fp.raw._sock
                aborts.append((type(sock).__name__, sock.fileno(), getattr(sock, "_closed", None)))
            except (AttributeError, OSError):
                aborts.append("no direct socket")
            return original_abort(response)
        try:
            with (patch.object(transfers, "open_https", side_effect=open_local),
                  patch.object(lifecycle, "abort_response", side_effect=trace_abort)):
                worker.start()
                self.assertTrue(started.wait(3), "body did not start")
                parent.cancel()
                worker.join(timeout=2)
                trace = ""
                if worker.is_alive():
                    frame = sys._current_frames().get(worker.ident)
                    trace = "".join(traceback.format_stack(frame)) if frame is not None else "no worker frame"
                self.assertFalse(worker.is_alive(), f"cancel left body read blocked; aborts={aborts}\n{trace}")
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], transfers.TransferCancelled)
                self.assertFalse(self.manager.scope.event.is_set())
                self.assertFalse(self.destination.exists())
                release.set()
                self.assertEqual(self.manager.download(self.url, self.destination, **self.options).read_bytes(), payload)
        finally:
            parent.cancel()
            release.set()
            worker.join(timeout=3)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)

    def test_minecraft_storage_error_preserves_specific_reason(self):
        download = preparation.Download(preparation.Progress(lambda _: None))
        with patch.object(download, "fetch", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaisesRegex(preparation.DownloadError, "места"):
                download(self.url, self.destination, sha1="0" * 40)

    @unittest.skipUnless(os.name == "nt", "Requires actual Windows short-path aliases")
    def test_native_windows_short_path_cannot_hang_destination_validation(self):
        import ctypes
        root = self.root / "BlockHarbor long cache folder"
        root.mkdir()
        short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        short_path.restype = ctypes.c_uint32
        size = short_path(str(root), None, 0)
        self.assertGreater(size, 0)
        buffer = ctypes.create_unicode_buffer(size)
        self.assertGreater(short_path(str(root), buffer, size), 0)
        code = (
            "from pathlib import Path; from launcher.transfers import TransferManager; import sys; "
            "manager=TransferManager(sys.argv[1]); "
            "actual=manager._inside(Path(sys.argv[2])/'content'/'mod.jar'); "
            "assert actual == Path(sys.argv[1]).resolve()/'content'/'mod.jar'; manager.close()"
        )
        process = subprocess.run([sys.executable, "-c", code, str(root.resolve()), buffer.value],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                 text=True, timeout=8)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()

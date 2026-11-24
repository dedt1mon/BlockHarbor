"""The asset transport reuses connections without sharing mutable worker sessions."""

import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from launcher import preparation


class AssetPoolingTests(unittest.TestCase):
    def test_two_real_asset_requests_share_one_connection(self):
        connections = []
        class Source(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_):
                pass
            def do_GET(self):
                connections.append(self.client_address)
                payload = b"verified artifact bytes"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with requests.Session() as session:
                for name in ("first", "second"):
                    target = io.BytesIO()
                    preparation._receive_requests(f"http://127.0.0.1:{server.server_port}/{name}", target, 1024, session=session)
                    self.assertEqual(target.getvalue(), b"verified artifact bytes")
            self.assertEqual(len(connections), 2)
            self.assertEqual(connections[0], connections[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_workers_have_separate_sessions_and_environment_closes_them(self):
        downloader = preparation.Download(preparation.Progress(lambda _: None))
        self.addCleanup(downloader.close)
        sessions = []
        def receive(_url, _output, _limit, _notify, *, session):
            sessions.append(session)
        def download():
            downloader._receive_asset("https://resources.download.minecraft.net/aa/a", io.BytesIO(), 1024, lambda *_: None)
        with patch.object(preparation, "_receive_requests", side_effect=receive):
            download()
            download()
            thread = threading.Thread(target=download)
            thread.start()
            thread.join(timeout=3)
        self.assertIs(sessions[0], sessions[1])
        self.assertIsNot(sessions[0], sessions[2])
        self.assertTrue(all(session.verify for session in sessions))
        with patch.object(sessions[0], "close") as first, patch.object(sessions[2], "close") as second:
            downloader.close()
            first.assert_called_once()
            second.assert_called_once()
        self.assertFalse(downloader._sessions)


if __name__ == "__main__":
    unittest.main()

import io
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from launcher import preparation


class PreparationDeadlineTests(unittest.TestCase):
    def test_checksum_attempts_share_one_budget_and_raise_download_error(self):
        url = preparation._NEO_ORIGIN + "net/neoforged/neoforge/21.1.999/neoforge-21.1.999-installer.jar"
        clock, deadlines = [0.0], []

        def timeout(url, output, limit, **kwargs):
            deadlines.append(kwargs["deadline"])
            clock[0] = kwargs["deadline"]
            raise TimeoutError()

        def wait(seconds):
            clock[0] += seconds

        with (patch.object(preparation.time, "monotonic", side_effect=lambda: clock[0]),
              patch.object(preparation, "CHECKSUM_SECONDS", 5),
              patch.object(preparation, "CHECKSUM_ATTEMPT_SECONDS", 3),
              patch.object(preparation, "_receive", side_effect=timeout),
              patch.object(preparation, "_receive_requests", side_effect=timeout),
              patch.object(preparation.lifecycle, "wait", side_effect=wait),
              self.assertRaises(preparation.DownloadError)):
            preparation._installer_checksum(url, deadline=100)
        self.assertEqual(deadlines, [3, 5])
        self.assertEqual(clock[0], 5)

    def test_installer_does_not_reset_total_budget_on_transport_or_mirror_switch(self):
        url = preparation._NEO_ORIGIN + "net/neoforged/neoforge/21.1.267/neoforge-21.1.267-installer.jar"
        clock = [0.0]

        def timeout(url, output, limit, notify=lambda *_: None, *, deadline):
            self.assertEqual(deadline, 5)
            output.write(b"partial")
            clock[0] = 5
            raise TimeoutError()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "installer.jar"
            target.write_bytes(b"previous")
            with (patch.object(preparation.time, "monotonic", side_effect=lambda: clock[0]),
                  patch.object(preparation, "INSTALLER_SECONDS", 5),
                  patch.object(preparation, "_receive", side_effect=timeout) as primary,
                  patch.object(preparation, "_receive_requests") as alternate,
                  self.assertRaises(preparation.DownloadError) as error):
                preparation.Download(preparation.Progress(lambda _: None))(url, target)
            primary.assert_called_once()
            alternate.assert_not_called()
            self.assertIn("Общее время", str(error.exception))
            self.assertNotIn("Резервная загрузка", str(error.exception))
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_both_transports_bound_an_actual_slow_trickle(self):
        class Source(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.01)
                except (ConnectionError, OSError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for receive in (preparation._receive, preparation._receive_requests):
                with self.subTest(transport=receive.__name__):
                    output = io.BytesIO()
                    with (patch.object(preparation, "MAX_RECEIVE_SECONDS", 0.04),
                          self.assertRaises(preparation._Incomplete)):
                        receive(f"http://127.0.0.1:{server.server_port}/slow", output, 100)
                    # A full-chunk read would consume all 100 bytes before ever
                    # checking the budget. Both transports stop while partial.
                    self.assertLess(len(output.getvalue()), 50)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

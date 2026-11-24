import io
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from launcher import preparation


class PreparationDeadlineTests(unittest.TestCase):
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

"""Execute the shipped cleanup runner against bounded, credential-free responses."""
import contextlib
import io
import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


class CommunityCleanupWorkflowTests(unittest.TestCase):
    def run_workflow(self, responses):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/community-cleanup.yml"
        workflow = path.read_text(encoding="utf-8")
        source = textwrap.dedent(workflow.split("python3 - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0])
        requests = []
        delays = []
        items = iter(responses)

        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, "https://blockharbor-community-catalog.ded8timon.chatgpt.site/v1/maintenance/prune")
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.data, b"")
                self.assertNotIn("Authorization", request.headers)
                self.assertEqual(timeout, 25)
                requests.append(request)
                return io.BytesIO(json.dumps(next(items)).encode())

        with patch("urllib.request.build_opener", return_value=Opener()), \
                patch("time.sleep", side_effect=delays.append), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(source, str(path), "exec"), {})
        return requests, delays

    def test_empty_registry_is_successful_without_retries(self):
        requests, delays = self.run_workflow([{"checked": 0, "limit": 16, "throttled": False, "retryAfterSeconds": 5}])
        self.assertEqual(len(requests), 1)
        self.assertEqual(delays, [])

    def test_throttle_then_remaining_batch_finishes(self):
        requests, delays = self.run_workflow([
            {"checked": 0, "limit": 16, "throttled": True, "retryAfterSeconds": 3},
            {"checked": 16, "limit": 16, "throttled": False, "retryAfterSeconds": 5},
            {"checked": 2, "limit": 16, "throttled": False, "retryAfterSeconds": 5},
        ])
        self.assertEqual(len(requests), 3)
        self.assertEqual(delays, [3, 5])

    def test_busy_registry_cannot_exceed_eight_calls(self):
        requests, delays = self.run_workflow([
            {"checked": 16, "limit": 16, "throttled": False, "retryAfterSeconds": 5}
            for _ in range(8)
        ])
        self.assertEqual(len(requests), 8)
        self.assertEqual(delays, [5] * 7)

    def test_invalid_server_contract_fails(self):
        for changes in ({"checked": 17}, {"checked": True}, {"retryAfterSeconds": 600}, {"throttled": "false"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "Unexpected cleanup contract"):
                self.run_workflow([{"checked": 0, "limit": 16, "throttled": False, "retryAfterSeconds": 5, **changes}])


if __name__ == "__main__":
    unittest.main()

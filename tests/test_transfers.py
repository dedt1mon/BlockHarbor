import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import transfers


class Response:
    def __init__(self, data, *, code=200, headers=None, fail_after=None, on_read=None):
        self.stream = io.BytesIO(data)
        self.code = code
        self.headers = headers if headers is not None else {"Content-Length": str(len(data)), "ETag": '"one"'}
        self.fail_after = fail_after
        self.reads = 0
        self.on_read = on_read
        self.closed = False

    def getcode(self):
        return self.code

    def read(self, amount=-1):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("connection lost")
        if self.on_read:
            self.on_read()
        return self.stream.read(amount)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TransfersTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manager = transfers.TransferManager(self.root)
        self.payload = b"verified data " * 1000
        self.options = {
            "hashes": {"sha256": hashlib.sha256(self.payload).hexdigest(), "sha512": hashlib.sha512(self.payload).hexdigest()},
            "size": len(self.payload), "hosts": {"cdn.modrinth.com"}, "max_size": 1024**2,
        }
        self.url = "https://cdn.modrinth.com/data/mod/version/mod.jar"
        self.destination = self.root / "files" / "mod.jar"

    def tearDown(self):
        self.directory.cleanup()

    def task(self, background=False):
        return self.manager._new(self.url, self.destination, "Mod", self.options["hashes"], self.options["size"], self.options["max_size"], self.options["hosts"], "content", True, background)

    def test_verified_publish_and_snapshot_hides_paths_and_urls(self):
        with patch.object(transfers, "open_https", return_value=Response(self.payload)):
            result = self.manager.download(self.url, self.destination, **self.options)
        self.assertEqual(result, self.destination)
        self.assertEqual(result.read_bytes(), self.payload)
        task = self.manager.snapshot()["tasks"][0]
        self.assertEqual(task["state"], "completed")
        self.assertEqual(task["bytes"], len(self.payload))
        self.assertEqual(task["total"], len(self.payload))
        self.assertNotIn("config", task)
        self.assertNotIn(str(self.root), json.dumps(task))

    def test_hash_mismatch_preserves_existing_destination(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"previous working file")
        bad = b"x" * len(self.payload)
        with patch.object(transfers, "open_https", return_value=Response(bad)):
            with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
                self.manager.download(self.url, self.destination, **self.options)
        self.assertEqual(self.destination.read_bytes(), b"previous working file")
        self.assertEqual(self.manager.snapshot()["tasks"][0]["state"], "failed")
        self.assertFalse(list(self.manager.cache.glob("*.part")))

    def test_strict_source_and_redirect_allowlist(self):
        rejected = [
            "http://cdn.modrinth.com/file", "https://cdn.modrinth.com.evil.test/file",
            "https://person@cdn.modrinth.com/file", "https://cdn.modrinth.com:444/file",
            "https://127.0.0.1/file", "https://cdn.modrinth.com/file#part",
            "https://cdn.modrinth.com\\@evil.test/file", "https://cdn.modrinth.com/file\nnext",
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ValueError):
                transfers.allowed_url(url, {"cdn.modrinth.com"})
        redirect = transfers.SafeRedirect({"cdn.modrinth.com"})
        with self.assertRaises(ValueError):
            redirect.redirect_request(None, None, 302, "", {}, "https://localhost/hidden")

    def test_destination_escape_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.download(self.url, self.root.parent / "escape.jar", **self.options)
        self.destination.parent.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager.download(self.url, linked / "escape.jar", **self.options)

    def test_actual_resume_checks_range_and_validator(self):
        task_id = self.task()
        config = self.manager.tasks[task_id]["config"]
        first = Response(self.payload[:5000], fail_after=1, headers={"Content-Length": str(len(self.payload)), "ETag": '"one"'})
        with patch.object(transfers, "open_https", return_value=first):
            with self.assertRaises(OSError):
                self.manager._receive(task_id, config, "Mod", lambda _: None)
        self.assertEqual((self.manager.cache / (task_id + ".part")).stat().st_size, 5000)
        remainder = Response(self.payload[5000:], code=206, headers={"Content-Range": f"bytes 5000-{len(self.payload)-1}/{len(self.payload)}", "ETag": '"one"'})
        with patch.object(transfers, "open_https", return_value=remainder) as opener:
            self.manager._receive(task_id, config, "Mod", lambda _: None)
        self.assertEqual(opener.call_args.kwargs["headers"], {"Range": "bytes=5000-", "If-Range": '"one"'})
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertTrue(self.manager.tasks[task_id]["resumed"])

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        task_id = self.task()
        partial = self.manager.cache / (task_id + ".part")
        partial.write_bytes(b"old prefix")
        transfers._atomic_json(self.manager.cache / (task_id + ".json"), {"url": self.url, "hashes": self.options["hashes"], "validator": '"one"', "validatorKind": "ETag"})
        with patch.object(transfers, "open_https", return_value=Response(self.payload)):
            self.manager._receive(task_id, self.manager.tasks[task_id]["config"], "Mod", lambda _: None)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertFalse(self.manager.tasks[task_id]["resumed"])

    def test_repeating_parent_operation_reuses_exact_partial_in_new_staging_directory(self):
        old_id = self.task()
        (self.manager.cache / (old_id + ".part")).write_bytes(self.payload[:5000])
        transfers._atomic_json(self.manager.cache / (old_id + ".json"), {"url": self.url, "hashes": self.options["hashes"], "validator": '"one"', "validatorKind": "ETag"})
        self.manager.tasks[old_id]["state"] = "cancelled"
        new_destination = self.root / "new-stage" / "mod.jar"
        remainder = Response(self.payload[5000:], code=206, headers={"Content-Range": f"bytes 5000-{len(self.payload)-1}/{len(self.payload)}", "ETag": '"one"'})
        with patch.object(transfers, "open_https", return_value=remainder) as opener:
            self.manager.download(self.url, new_destination, **self.options)
        self.assertEqual(opener.call_args.kwargs["headers"]["Range"], "bytes=5000-")
        self.assertEqual(new_destination.read_bytes(), self.payload)
        self.assertFalse((self.manager.cache / (old_id + ".part")).exists())

    def test_false_range_cannot_corrupt_destination(self):
        task_id = self.task()
        (self.manager.cache / (task_id + ".part")).write_bytes(self.payload[:20])
        transfers._atomic_json(self.manager.cache / (task_id + ".json"), {"url": self.url, "hashes": self.options["hashes"], "validator": '"one"', "validatorKind": "ETag"})
        response = Response(self.payload[20:], code=206, headers={"Content-Range": f"bytes 21-{len(self.payload)-1}/{len(self.payload)}", "ETag": '"one"'})
        with patch.object(transfers, "open_https", return_value=response), self.assertRaisesRegex(ValueError, "диапазон"):
            self.manager._receive(task_id, self.manager.tasks[task_id]["config"], "Mod", lambda _: None)
        self.assertFalse(self.destination.exists())

    def test_cancel_is_not_published_and_caller_retry_is_explicit(self):
        task_id = self.task()
        self.manager.cancel(task_id)
        with self.assertRaises(transfers.TransferCancelled):
            self.manager._run(task_id, lambda _: None)
        with self.assertRaisesRegex(ValueError, "исходную операцию"):
            self.manager.retry(task_id)
        self.assertFalse(self.destination.exists())

    def test_cancel_during_receive_preserves_partial_only(self):
        task_id = self.task()
        response = Response(self.payload, on_read=lambda: self.manager.cancel_flags[task_id].set())
        with patch.object(transfers, "open_https", return_value=response):
            with self.assertRaises(transfers.TransferCancelled):
                self.manager._run(task_id, lambda _: None)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.manager.tasks[task_id]["state"], "cancelled")

    def test_oversized_response_rejected_before_destination(self):
        response = Response(self.payload, headers={"Content-Length": str(2 * 1024**2)})
        with patch.object(transfers, "open_https", return_value=response), self.assertRaisesRegex(ValueError, "размер"):
            self.manager.download(self.url, self.destination, **self.options)
        self.assertFalse(self.destination.exists())

    def test_recovery_marks_interrupted_and_preserves_safe_retry(self):
        task_id = self.task(background=True)
        other = transfers.TransferManager(self.root)
        row = other.snapshot()["tasks"][0]
        self.assertEqual(row["id"], task_id)
        self.assertEqual(row["state"], "interrupted")
        self.assertTrue(row["retryable"])

    def test_duplicate_destination_is_not_downloaded_concurrently(self):
        self.task()
        with self.assertRaisesRegex(ValueError, "уже загружается"):
            self.task()

    def test_clear_keeps_active_tasks_and_never_deletes_published_files(self):
        with patch.object(transfers, "open_https", return_value=Response(self.payload)):
            self.manager.download(self.url, self.destination, **self.options)
        second = self.manager._new(self.url, self.root / "second.jar", "Second", self.options["hashes"], self.options["size"], self.options["max_size"], self.options["hosts"], "content", True, False)
        self.manager.clear()
        self.assertEqual([row["id"] for row in self.manager.snapshot()["tasks"]], [second])
        self.assertEqual(self.destination.read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()

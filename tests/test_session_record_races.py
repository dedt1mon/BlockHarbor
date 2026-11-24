"""Atomic session records survive temporary Windows sharing violations."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import sessions


class SessionRecordRaceTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state.json"

    def test_transient_windows_reader_lock_retries_same_atomic_record(self):
        sessions.atomic_json(self.path, {"profiles": {"old": {}}})
        real_replace = sessions.os.replace
        calls = []

        def replace(source, target):
            calls.append(target)
            if len(calls) < 3:
                error = PermissionError("temporary sharing violation")
                error.winerror = 5
                raise error
            return real_replace(source, target)

        with patch.object(sessions.os, "replace", side_effect=replace):
            sessions.atomic_json(self.path, {"profiles": {}})
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"profiles": {}})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_persistent_denial_stops_and_preserves_previous_record(self):
        sessions.atomic_json(self.path, {"profiles": {"old": {}}})
        error = PermissionError("denied")
        error.winerror = 5
        with patch.object(sessions.os, "replace", side_effect=error) as replace, self.assertRaises(PermissionError):
            sessions.atomic_json(self.path, {"profiles": {}})
        self.assertEqual(replace.call_count, 6)
        self.assertIn("old", sessions.read_record(self.path)["profiles"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_removal_retries_a_transient_reader_without_replacing_the_record(self):
        sessions.atomic_json(self.path, {"session": "owned"})
        real_unlink = Path.unlink
        calls = []

        def unlink(path, *args, **kwargs):
            if path == self.path:
                calls.append(path)
                if len(calls) < 3:
                    error = PermissionError("reader holds delete-denying handle")
                    error.winerror = 32
                    raise error
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=unlink):
            sessions.remove_record(self.path)
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.path.exists())

    def test_removal_preserves_record_after_persistent_windows_denial(self):
        sessions.atomic_json(self.path, {"session": "owned"})
        error = PermissionError("persistent denial")
        error.winerror = 32
        with patch.object(Path, "unlink", side_effect=error) as unlink:
            with self.assertRaises(PermissionError):
                sessions.remove_record(self.path)
        self.assertEqual(unlink.call_count, 6)
        self.assertEqual(sessions.read_record(self.path), {"session": "owned"})

    def test_real_polling_does_not_block_final_owned_record_removal(self):
        sessions.atomic_json(self.path, {"session": "owned"})
        started, stopped = threading.Event(), threading.Event()

        def read():
            while not stopped.is_set():
                sessions.read_record(self.path)
                started.set()

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            sessions.remove_record(self.path)
        finally:
            stopped.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.path.exists())

    def test_real_concurrent_polling_and_replacement_keep_records_valid(self):
        sessions.atomic_json(self.path, {"sequence": 0, "profiles": {}})
        stopped = threading.Event()
        seen = []

        def read():
            while not stopped.is_set():
                value = sessions.read_record(self.path)
                if value:
                    seen.append(value)

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        try:
            for sequence in range(1, 101):
                sessions.atomic_json(self.path, {"sequence": sequence, "profiles": {}})
        finally:
            stopped.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(seen)
        self.assertTrue(all(type(row.get("sequence")) is int and row.get("profiles") == {} for row in seen))
        self.assertEqual(sessions.read_record(self.path)["sequence"], 100)


if __name__ == "__main__":
    unittest.main()

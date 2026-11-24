"""Portable invalidation of cached checksums, including Windows creation-time traps."""

import hashlib
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import modfiles


class FileStampTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "Кэш с пробелами.jar"
        self.path.write_bytes(b"first")

    def test_actual_platform_stamp_detects_same_size_restored_mtime_edit(self):
        first = modfiles.file_stamp(self.path)
        original = self.path.stat()
        self.assertEqual(modfiles.file_hash(self.path, first, "sha256"), hashlib.sha256(b"first").hexdigest())
        self.path.write_bytes(b"other")
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        changed = modfiles.file_stamp(self.path)
        self.assertEqual(first[:2], changed[:2])
        self.assertNotEqual(first[2], changed[2])
        self.assertEqual(modfiles.file_hash(self.path, changed, "sha256"), hashlib.sha256(b"other").hexdigest())
        self.assertEqual(modfiles.file_stamp(self.path), changed)
        if os.name == "nt":
            print("Windows file-stamp strategy:", str(changed[2]).split(":", 1)[0])

    def test_journal_records_require_known_version_and_positive_sequence(self):
        for major, reference_size, length in ((2, 8, 60), (3, 16, 76)):
            with self.subTest(major=major):
                raw = bytearray(length)
                struct.pack_into("<IHH", raw, 0, length, major, 0)
                raw[8:8 + reference_size] = b"x" * reference_size
                struct.pack_into("<q", raw, 8 + 2 * reference_size, 123)
                self.assertEqual(modfiles._usn_stamp(raw), f"usn:{major}:" + (b"x" * reference_size).hex() + ":123")
                self.assertIsNone(modfiles._usn_stamp(raw[:-1]))
                struct.pack_into("<q", raw, 8 + 2 * reference_size, 0)
                self.assertIsNone(modfiles._usn_stamp(raw))
                struct.pack_into("<H", raw, 4, 4)
                self.assertIsNone(modfiles._usn_stamp(raw))

    def test_unsupported_windows_query_uses_real_content_and_remains_stable(self):
        with patch.object(modfiles, "_windows_file_api", side_effect=OSError("unsupported filesystem")):
            before = modfiles._windows_change_token(self.path)
            self.assertEqual(before, modfiles._windows_change_token(self.path))
            self.assertEqual(before, "sha256:" + hashlib.sha256(b"first").hexdigest())
            self.path.write_bytes(b"other")
            self.assertNotEqual(before, modfiles._windows_change_token(self.path))


if __name__ == "__main__":
    unittest.main()

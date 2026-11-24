"""Portable invalidation of cached checksums, including Windows creation-time traps."""

import hashlib
import os
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

    def test_unsupported_windows_query_uses_real_content_and_remains_stable(self):
        with patch.object(modfiles, "_windows_file_api", side_effect=OSError("unsupported filesystem")):
            before = modfiles._windows_change_token(self.path)
            self.assertEqual(before, modfiles._windows_change_token(self.path))
            self.assertEqual(before, "sha256:" + hashlib.sha256(b"first").hexdigest())
            self.path.write_bytes(b"other")
            self.assertNotEqual(before, modfiles._windows_change_token(self.path))


if __name__ == "__main__":
    unittest.main()

"""Malformed game archives must not crash or exhaust local advisory readers."""

import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import compatibility
from launcher.modfiles import file_stamp, metadata_zip, mod_info


def make_jar(metadata, nested=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fabric.mod.json", json.dumps(metadata))
        if nested is not None:
            archive.writestr("lib/nested.jar", nested)
    return output.getvalue()


def corrupt_deflate(raw, name):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        offset = archive.getinfo(name).header_offset
    name_len, extra_len = struct.unpack_from("<HH", raw, offset + 26)
    compressed = offset + 30 + name_len + extra_len
    result = bytearray(raw)
    result[compressed] = 7  # Reserved DEFLATE block type, not merely a CRC mismatch.
    return bytes(result)


class MetadataBounds(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / "test.jar"

    def test_invalid_deflate_descriptor_is_an_unknown_mod_not_exception(self):
        self.path.write_bytes(corrupt_deflate(make_jar({"id": "test", "version": "1"}), "fabric.mod.json"))
        stamp = file_stamp(self.path)
        self.assertEqual((frozenset(), ""), mod_info(self.path, stamp))
        report = compatibility.local_report({"version": "1.20.1", "loader": "fabric"}, {}, {"test.jar": self.path})
        self.assertFalse(report["blocked"])
        self.assertIn("нет читаемого описания", report["warnings"][0])

    def test_invalid_nested_deflate_keeps_parent_metadata(self):
        nested = make_jar({"id": "library", "version": "1"})
        data = make_jar({"id": "parent", "version": "1", "jars": [{"file": "lib/nested.jar"}]}, nested)
        self.path.write_bytes(corrupt_deflate(data, "lib/nested.jar"))
        info = compatibility._metadata(self.path, file_stamp(self.path), "fabric")
        self.assertIn("parent", info.provided)
        self.assertNotIn("library", info.provided)

    def test_forged_directory_count_rejected_before_zipinfo_allocation(self):
        data = bytearray(make_jar({"id": "parent", "version": "1"}, make_jar({"id": "nested", "version": "1"})))
        offset = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, offset + 8, 1, 1)
        self.path.write_bytes(data)
        with patch("launcher.modfiles.zipfile.ZipFile") as parser:
            with self.assertRaises(zipfile.BadZipFile):
                with metadata_zip(self.path):
                    pass
            parser.assert_not_called()

    def test_oversized_directory_rejected_before_zipinfo_allocation(self):
        data = bytearray(make_jar({"id": "test", "version": "1"}))
        offset = data.rfind(b"PK\x05\x06")
        struct.pack_into("<I", data, offset + 12, 1024**3)
        self.path.write_bytes(data)
        with patch("launcher.modfiles.zipfile.ZipFile") as parser:
            self.assertEqual((frozenset(), ""), mod_info(self.path, file_stamp(self.path)))
            parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""The setup builder must refuse incomplete or corrupt single-file releases."""

import hashlib
import importlib.util
import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools/build_installer.py"
SPEC = importlib.util.spec_from_file_location("blockharbor_installer_build", MODULE)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class InstallerPackageTests(unittest.TestCase):
    def package(self):
        binary = bytearray(256)
        binary[:2] = b"MZ"
        struct.pack_into("<I", binary, 0x3C, 0x80)
        binary[0x80:0x86] = b"PE\0\0\x64\x86"
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as z:
            z.writestr("main.py", "print('BlockHarbor')\n")
        data = payload.getvalue()
        return (
            bytes(binary)
            + data
            + struct.pack(
                "<Q32s16s",
                len(data),
                hashlib.sha256(data).digest(),
                b"BlockHarborPKG18",
            )
        )

    def test_complete_release_and_damaged_copy(self):
        original = self.package()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BlockHarbor.exe"
            path.write_bytes(original)
            self.assertEqual(builder.verify_launcher(path), len(original))
            damaged = bytearray(original)
            damaged[280] ^= 1
            path.write_bytes(damaged)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                builder.verify_launcher(path)
            path.write_bytes(original[:-7])
            with self.assertRaisesRegex(ValueError, "complete embedded package"):
                builder.verify_launcher(path)
            path.write_bytes(original[:256])
            with self.assertRaisesRegex(ValueError, "complete embedded package"):
                builder.verify_launcher(path)

    def test_wrong_architecture_and_nsis_path_literals(self):
        wrong = bytearray(self.package())
        wrong[0x84:0x86] = b"\x4c\x01"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BlockHarbor.exe"
            path.write_bytes(wrong)
            with self.assertRaisesRegex(ValueError, "x64"):
                builder.verify_launcher(path)
        self.assertEqual(
            builder.nsis_literal("C:\\$User\\Block Harbor.exe"),
            "C:\\$User\\Block Harbor.exe",
        )
        self.assertEqual(
            builder.nsis_literal("https://example.com/$asset", runtime=True),
            "https://example.com/$$asset",
        )
        with self.assertRaisesRegex(ValueError, "single-line"):
            builder.nsis_literal("invalid\npath")

"""The setup builder must refuse incomplete or corrupt single-file releases."""

import hashlib
import importlib.util
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "tools/build_installer.py"
SPEC = importlib.util.spec_from_file_location("blockharbor_installer_build", MODULE)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class InstallerPackageTests(unittest.TestCase):
    @staticmethod
    def native_binary(architecture=b"\x64\x86"):
        binary = bytearray(256)
        binary[:2] = b"MZ"
        struct.pack_into("<I", binary, 0x3C, 0x80)
        binary[0x80:0x86] = b"PE\0\0" + architecture
        return bytes(binary)

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

    def test_cleanup_is_standalone_windows_gui_with_local_toolchain(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "BlockHarborDataCleanup.exe"
            calls = []

            def compile_helper(command, **kwargs):
                calls.append((command, kwargs))
                output.write_bytes(self.native_binary())

            with mock.patch.object(builder.subprocess, "run", side_effect=compile_helper):
                builder.build_cleanup("/tools/go", output)
            command, kwargs = calls[0]
            self.assertEqual(command[:2], ["/tools/go", "build"])
            self.assertIn("-ldflags=-H=windowsgui -s -w -buildid=", command)
            self.assertEqual(kwargs["cwd"], builder.TOOLS / "data_cleanup")
            self.assertEqual(
                {key: kwargs["env"][key] for key in ("GOOS", "GOARCH", "CGO_ENABLED", "GOTOOLCHAIN", "GOWORK", "GOFLAGS")},
                dict(GOOS="windows", GOARCH="amd64", CGO_ENABLED="0", GOTOOLCHAIN="local", GOWORK="off", GOFLAGS=""),
            )
            self.assertTrue(kwargs["check"])

    def test_cleanup_rejects_wrong_or_missing_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "helper.exe"
            for binary in (b"not an executable", self.native_binary(b"\x4c\x01")):
                with mock.patch.object(builder.subprocess, "run", side_effect=lambda *a, **k: output.write_bytes(binary)):
                    with self.assertRaisesRegex(ValueError, "Windows"):
                        builder.build_cleanup("go", output)

    def test_nsis_source_is_explicit_utf8_bom_even_on_non_russian_windows(self):
        source_text = 'Unicode true\nName "Установка BlockHarbor"\n'
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.nsi"
            target = Path(temp) / "compiled.nsi"
            for encoding in ("utf-8", "utf-8-sig"):
                with self.subTest(encoding=encoding):
                    source.write_bytes(source_text.encode(encoding))
                    self.assertEqual(builder.stage_unicode_script(source, target), target)
                    self.assertEqual(target.read_bytes(), b"\xef\xbb\xbf" + source_text.encode("utf-8"))
                    self.assertEqual(target.read_text(encoding="utf-8-sig"), source_text)
            source.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                builder.stage_unicode_script(source, target)

    def test_setup_build_embeds_the_verified_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "BlockHarbor.exe"
            launcher.write_bytes(self.package())
            output = Path(tmp) / "Setup.exe"
            calls = []

            def compile_step(command, **kwargs):
                calls.append(command)
                if command[1] == "build":
                    target = Path(command[command.index("-o") + 1])
                else:
                    self.assertIn("UTF8", command)
                    self.assertTrue(any(arg.endswith("INPUTCHARSET") for arg in command))
                    source = Path(command[-1])
                    self.assertTrue(source.read_bytes().startswith(b"\xef\xbb\xbf"))
                    self.assertIn("Установка BlockHarbor", source.read_text(encoding="utf-8-sig"))
                    definitions = dict(arg[2:].split("=", 1) for arg in command if arg.startswith(("-D", "/D")))
                    helper = Path(definitions["CLEANUP_EXE"])
                    self.assertEqual(helper.read_bytes(), self.native_binary())
                    self.assertEqual(helper.parent, Path(definitions["OUT_FILE"]).parent)
                    target = Path(definitions["OUT_FILE"])
                target.write_bytes(self.native_binary())

            argv = ["build_installer.py", "--launcher", str(launcher), "--output", str(output), "--go", "/tools/go", "--makensis", "/tools/makensis"]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(builder.subprocess, "run", side_effect=compile_step), mock.patch("builtins.print"):
                builder.main()
            self.assertEqual(len(calls), 2)
            self.assertEqual(output.read_bytes(), self.native_binary())
            self.assertEqual(launcher.read_bytes(), self.package())

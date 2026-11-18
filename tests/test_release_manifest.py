import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "release_manifest_tool", Path(__file__).resolve().parents[1] / "tools/release_update_manifest.py"
)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class ReleaseManifestTests(unittest.TestCase):
    def test_legacy_compatibility_fields_remain_unchanged(self):
        self.assertEqual(tool.manifest("2.4.0"), {
            "format": 1, "version": "2.4.0", "dataSchema": 1,
            "minDataSchema": 1, "maxDataSchema": 1, "rollbackSafe": True,
        })

    def test_public_metadata_matches_exact_installer_bytes(self):
        payload = b"verified release fixture\x00\xff"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "BlockHarbor-Setup-2.4.0.exe"
            path.write_bytes(payload)
            value = tool.manifest("2.4.0", installer=path)
            self.assertEqual(value["installer"], {
                "name": path.name, "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
            self.assertEqual(value["dataSchema"], 1)

    def test_wrong_version_and_empty_or_oversized_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "BlockHarbor-Setup-2.3.0.exe"
            path.write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "2.4.0"):
                tool.manifest("2.4.0", installer=path)
            path = Path(folder) / "BlockHarbor-Setup-2.4.0.exe"
            path.touch()
            with self.assertRaisesRegex(ValueError, "size"):
                tool.manifest("2.4.0", installer=path)
            with path.open("wb") as stream:
                stream.truncate(tool.MAX_INSTALLER + 1)
            with self.assertRaisesRegex(ValueError, "size"):
                tool.manifest("2.4.0", installer=path)

    def test_version_and_schema_rules_match_updater_contract(self):
        for version in ("02.4.0", "2.4", "v2.4.0", "2.4.0-rc.01", "2.4.0/other"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                tool.manifest(version)
        for kwargs in ({"minimum": 0}, {"maximum": 0}, {"data_schema": True}, {"minimum": 2, "maximum": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                tool.manifest("2.4.0", **kwargs)
        self.assertFalse(tool.manifest("2.4.0-rc.1", rollback_safe=False)["rollbackSafe"])

    def test_cli_cannot_overwrite_installer_with_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "BlockHarbor-Setup-2.4.0.exe"
            path.write_bytes(b"keep this executable")
            args = ["manifest", "--version", "2.4.0", "--installer", str(path), "--output", str(path)]
            with patch.object(sys, "argv", args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tool.main()
            self.assertEqual(path.read_bytes(), b"keep this executable")

    def test_cli_writes_compatible_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "BlockHarbor-Setup-2.4.0.exe"
            path.write_bytes(b"fixture")
            target = Path(folder) / "release" / "BlockHarbor-update.json"
            args = ["manifest", "--version", "2.4.0", "--installer", str(path), "--output", str(target)]
            with patch.object(sys, "argv", args):
                tool.main()
            self.assertEqual(json.loads(target.read_bytes()), tool.manifest("2.4.0", installer=path))

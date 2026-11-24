import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import prism


class PrismTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # launch() supplies canonical paths to its internal instance builder.
        # Windows TemporaryDirectory can otherwise use the same directory's 8.3 alias.
        self.root = Path(tmp.name).resolve(strict=True)
        self.folder = self.root / "old BlockHarbor instance"
        self.folder.mkdir()
        self.data = self.root / "engine data"
        self.data.mkdir()
        self.rows = {
            "net.minecraft": [{"version": "1.20.1"}],
            "net.fabricmc.intermediary": [{"version": "1.20.1"}],
            prism.LOADER["fabric"]: [{"version": "0.20.1", "recommended": True}],
            prism.LOADER["quilt"]: [
                {"version": "0.40-beta", "type": "beta"},
                {"version": "0.30.2", "type": "release"},
            ],
            prism.LOADER["forge"]: [
                {
                    "version": "53",
                    "recommended": True,
                    "requires": [{"uid": "net.minecraft", "equals": "1.21.1"}],
                },
                {
                    "version": "47.4.29",
                    "requires": [{"uid": "net.minecraft", "equals": "1.20.1"}],
                },
                {
                    "version": "47.4.28",
                    "recommended": True,
                    "requires": [{"uid": "net.minecraft", "equals": "1.20.1"}],
                },
            ],
            prism.LOADER["neoforge"]: [
                {
                    "version": "47.1.106",
                    "type": "release",
                    "requires": [{"uid": "net.minecraft", "equals": "1.20.1"}],
                }
            ],
        }
        self.mock_rows = self.enterContext(
            patch.object(prism, "_rows", side_effect=self.rows.__getitem__)
        )

    def test_actual_instance_layout_for_every_loader(self):
        expected = {
            "forge": "47.4.28",
            "quilt": "0.30.2",
            "neoforge": "47.1.106",
            "fabric": "0.20.1",
            "vanilla": "1.20.1",
        }
        (self.folder / "mods").mkdir()
        (self.folder / "mods" / "existing.jar").write_bytes(b"original")
        for loader, pinned in expected.items():
            with self.subTest(loader=loader):
                ident = prism._instance(
                    self.data, self.folder, "Остров", "1.20.1", loader, 6144
                )
                base = self.data / "instances" / ident
                pack = json.loads((base / "mmc-pack.json").read_text(encoding="utf-8"))
                self.assertEqual(pack["components"][-1]["version"], pinned)
                self.assertEqual(pack["components"][0]["uid"], "net.minecraft")
                self.assertEqual(pack["formatVersion"], 1)
                if loader in ("fabric", "quilt"):
                    self.assertEqual(
                        pack["components"][1]["uid"], "net.fabricmc.intermediary"
                    )
                self.assertEqual(
                    (base / "minecraft" / "mods" / "existing.jar").read_bytes(),
                    b"original",
                )
                self.assertIn(
                    "MaxMemAlloc=6144", (base / "instance.cfg").read_text("utf-8")
                )

    def test_relaunch_preserves_prism_fields_and_works_offline(self):
        args = (self.data, self.folder, "Test", "1.20.1", "fabric", 4096)
        key = prism._instance(*args)
        p = self.data / "instances" / key / "instance.cfg"
        p.write_text(p.read_text(encoding="utf-8") + "lastLaunchTime=12345\n", encoding="utf-8")
        self.mock_rows.reset_mock()
        self.assertEqual(prism._instance(*args), key)
        self.assertIn("lastLaunchTime=12345", p.read_text(encoding="utf-8"))
        self.mock_rows.assert_not_called()

    def test_pin_from_direct_profiles_preserved(self):
        stamp = {
            "version": "1.20.1",
            "loader": "forge",
            "loader_version": "1.20.1-47.4.29",
        }
        (self.folder / prism.game.STAMP).write_text(json.dumps(stamp))
        self.assertEqual(
            prism._components(self.folder, "1.20.1", "forge")[-1]["version"], "47.4.29"
        )

    def test_wrong_version_rejected_without_partial_instance(self):
        with self.assertRaises(ValueError):
            prism._instance(self.data, self.folder, "Test", "1.19.2", "fabric", 4096)
        self.assertEqual(list((self.data / "instances").iterdir()), [])

    def test_archive_checksum_checked_before_any_execution(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            for entry in prism.REQUIRED:
                z.writestr(entry, b"placeholder")
        raw = archive.getvalue()
        with (
            patch.object(prism, "urlopen", return_value=io.BytesIO(raw)),
            patch.object(prism, "SIZE", len(raw)),
        ):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                prism._engine(self.root, lambda _: None)
        self.assertFalse(list(self.root.glob("engine-*")))
        with (
            patch.object(prism, "urlopen", return_value=io.BytesIO(raw)),
            patch.object(prism, "SIZE", len(raw)),
            patch.object(prism, "SHA256", hashlib.sha256(raw).hexdigest()),
        ):
            exe = prism._engine(self.root, lambda _: None)
            self.assertTrue(exe.is_file())
        with patch.object(prism, "urlopen", side_effect=AssertionError):
            self.assertEqual(prism._engine(self.root, lambda _: None), exe)

    def test_launch_sends_instance_directly_and_does_not_open_a_selection_screen(self):
        exe = self.root / "prismlauncher.exe"
        with (
            patch.object(prism.sys, "platform", "win32"),
            patch.object(prism, "_engine", return_value=exe),
            patch.object(prism.subprocess, "Popen") as popen,
        ):
            child = Mock()
            child.wait.side_effect = subprocess.TimeoutExpired("prism", 1.5)
            popen.return_value = child
            cmd = prism.launch(
                self.root, self.folder, "Остров", "1.20.1", "fabric", 4096
            )
            self.assertIs(cmd, child)
            argv = popen.call_args.args[0]
            self.assertEqual((argv[0], argv[1], argv[3]), (str(exe), "--dir", "--launch"))
            self.assertTrue(Path(argv[2]).samefile(self.root / "prism" / "data"))
            self.assertTrue(
                (Path(argv[2]) / "instances" / argv[4] / "minecraft").samefile(
                    self.folder
                )
            )
            self.assertNotIn("--show-window", argv)
            (Path(argv[2]) / "prismlauncher.cfg").write_text("previous settings")
            child.wait.side_effect = None
            child.wait.return_value = 7
            with self.assertRaisesRegex(RuntimeError, "7"):
                prism.launch(self.root, self.folder, "Остров", "1.20.1", "fabric", 4096)
            self.assertEqual(
                (Path(argv[2]) / "prismlauncher.cfg").read_text(), "previous settings"
            )

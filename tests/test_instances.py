import tempfile
import unittest
import zipfile
from pathlib import Path

from launcher.store import Store


class InstanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.first = self.store.create("Первая сборка", "1.20.1", "fabric")
        self.second = self.store.create("Друзья", "1.20.1", "fabric")

    def make_pack(self, name, contents):
        incoming = self.root / name
        with zipfile.ZipFile(incoming, "w") as archive:
            for file, data in contents.items():
                archive.writestr(file, data)
        return incoming

    def test_mod_isolated_after_switch_and_restart(self):
        source = self.make_pack("map.jar", {"fabric.mod.json": '{"id":"map"}'})
        self.store.import_file(self.first, "mods", source.name, source)
        self.assertFalse(self.store.content(self.second)["mods"])
        restarted = Store(self.root)
        self.assertEqual(restarted.get("active"), self.second)
        self.assertEqual(restarted.content(self.first)["mods"][0]["name"], "map.jar")
        with self.assertRaises(ValueError):
            restarted.remove(self.second, "mods", source.name)
        self.assertTrue((self.store.folder(self.first) / "mods" / source.name).exists())

    def test_duplicate_rejected_before_modification(self):
        a = self.make_pack("a.zip", {"pack.mcmeta": "first"})
        b = self.make_pack("b.zip", {"pack.mcmeta": "second"})
        self.store.import_file(self.first, "resourcepacks", "pack.zip", a)
        with self.assertRaises(ValueError):
            self.store.import_file(self.first, "resourcepacks", "pack.zip", b)
        self.assertEqual(
            (self.store.folder(self.first) / "resourcepacks" / "pack.zip").read_bytes(),
            a.read_bytes(),
        )

    def test_bad_or_incompatible_addons_do_not_leave_files(self):
        resource = self.make_pack("r.zip", {"not-a-pack.txt": ""})
        with self.assertRaises(ValueError):
            self.store.import_file(self.first, "resourcepacks", "r.zip", resource)
        with self.assertRaises(ValueError):
            self.store.import_file(self.first, "shaderpacks", "s.zip", resource)
        forge = self.store.create("Test Forge", "1.20.1", "forge")
        fabric = self.make_pack(
            "fabric.jar",
            {"fabric.mod.json": "{}", "META-INF/MANIFEST.MF": "Manifest-Version: 1.0"},
        )
        with self.assertRaises(ValueError):
            self.store.import_file(forge, "mods", "fabric.jar", fabric)
        wrong = self.root / "bad.zip"
        wrong.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            self.store.import_file(self.first, "shaderpacks", "s.zip", wrong)
        self.assertFalse(any(self.store.content(self.first).values()))

    def test_windows_reserved_names_and_parent_components_are_rejected(self):
        pack = self.make_pack("s.zip", {"shaders/final.fsh": "void main(){}"})
        for name in ["../s.zip", "CON.zip", "other:s.zip", "test.zip.", "hidden/s.zip"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.import_file(self.first, "shaderpacks", name, pack)

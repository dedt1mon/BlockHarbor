"""JAR loader markers: keep strict checks while supporting documented FML libraries."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from launcher.store import PackLoaderMismatch, verify_pack


class ModArtifactTests(unittest.TestCase):
    def archive(self, entries):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(tmp) / "addon.jar"
        with zipfile.ZipFile(path, "w") as out:
            for name, content in entries.items():
                out.writestr(name, content)
        return path

    def test_fml_libraries_and_language_providers_without_mods_toml(self):
        for kind in ("LIBRARY", "GAMELIBRARY", "LANGPROVIDER"):
            with self.subTest(kind=kind):
                path = self.archive({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\nFMLModType: " + kind + "\r\n\r\n"})
                verify_pack(path, "mods", "forge")
                verify_pack(path, "mods", "neoforge")
                with self.assertRaises(PackLoaderMismatch):
                    verify_pack(path, "mods", "fabric")

    def test_core_plugin_and_folded_manifest_attribute(self):
        path = self.archive({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\nFMLCorePlugin: example.\r\n Plugin\r\n\r\n"})
        verify_pack(path, "mods", "forge")
        with self.assertRaises(PackLoaderMismatch):
            verify_pack(path, "mods", "neoforge")

    def test_arbitrary_jars_and_wrong_loader_markers_are_not_allowed(self):
        for manifest in ("Manifest-Version: 1.0\r\n",
                         "Manifest-Version: 1.0\r\nFMLModType: MOD\r\n\r\n",
                         "Manifest-Version: 1.0\r\n\r\nName: example.class\r\nFMLModType: LIBRARY\r\n",
                         "Manifest-Version: 1.0\r\nFMLModType: MOD\r\nFMLModType: LIBRARY\r\n"):
            path = self.archive({"META-INF/MANIFEST.MF": manifest, "example.class": b"test"})
            with self.assertRaises(PackLoaderMismatch):
                verify_pack(path, "mods", "forge")
        path = self.archive({"fabric.mod.json": '{"id":"fabric_only"}',
                             "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\nFMLModType: LIBRARY\r\n"})
        with self.assertRaisesRegex(PackLoaderMismatch, "Fabric"):
            verify_pack(path, "mods", "forge")
        verify_pack(path, "mods", "fabric")
        verify_pack(path, "mods", "quilt")

    def test_corrupt_archive_is_not_a_loader_mismatch(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(tmp) / "invalid.jar"
        path.write_bytes(b"invalid")
        with self.assertRaisesRegex(ValueError, "ZIP/JAR") as error:
            verify_pack(path, "mods", "forge")
        self.assertNotIsInstance(error.exception, PackLoaderMismatch)


if __name__ == "__main__":
    unittest.main()

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from launcher import modrinth
from launcher.store import Store


def fabric(metadata, nested=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as jar:
        jar.writestr("fabric.mod.json", json.dumps(metadata))
        if nested:
            jar.writestr("lib/io.jar", nested)
    return data.getvalue()


class ManualChecks(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(tmp))
        self.pid = self.store.create("Fabric", "1.20.1", "fabric")
        self.folder = self.store.content_folder(self.pid, "mods")
        self.upload = Path(tmp) / "upload"

    def check(self, payload, pid=None):
        self.upload.write_bytes(payload)
        return modrinth.manual_check(
            self.store, pid or self.pid, "mods", "new.jar", self.upload
        )

    def test_required_recommended_optional_and_bundled_names_are_reported_offline(self):
        bundled = fabric({"id": "lib", "version": "2", "name": "Library"})
        (self.folder / "installed.jar").write_bytes(
            fabric(
                {"id": "base", "version": "4", "jars": [{"file": "lib/io.jar"}]},
                bundled,
            )
        )
        result = self.check(
            fabric(
                {
                    "id": "incoming",
                    "version": "1",
                    "depends": {"absent": "*", "lib": ">=2"},
                    "recommends": {"helper": "*"},
                    "suggests": {"optional": "*"},
                }
            )
        )
        self.assertFalse(result["blocked"])
        self.assertEqual(
            [(x["name"], x["installed"], x["filename"]) for x in result["required"]],
            [("absent", False, ""), ("Library", True, "installed.jar")],
        )
        self.assertEqual(
            [(x["name"], x["recommended"]) for x in result["optional"]],
            [("helper", True), ("optional", False)],
        )
        self.assertEqual([p.name for p in self.folder.iterdir()], ["installed.jar"])

    def test_exact_and_reverse_hard_conflicts_and_soft_cautions(self):
        (self.folder / "installed.jar").write_bytes(
            fabric(
                {
                    "id": "base",
                    "version": "2.1",
                    "breaks": {"next": "<2"},
                    "conflicts": {"soft": "*"},
                }
            )
        )
        self.assertTrue(self.check(fabric({"id": "next", "version": "1.5"}))["blocked"])
        self.assertFalse(
            self.check(fabric({"id": "next", "version": "2.0"}))["blocked"]
        )
        out = self.check(fabric({"id": "soft", "version": "1"}))
        self.assertFalse(out["blocked"])
        self.assertIn("installed.jar", out["warnings"][0])
        out = self.check(
            fabric(
                {
                    "id": "newmod",
                    "version": "1",
                    "breaks": {"base": ["1.x", ">=2.0 <3"]},
                }
            )
        )
        self.assertTrue(out["blocked"])
        out = self.check(
            fabric({"id": "newmod", "version": "1", "breaks": {"base": "otherbuild"}})
        )
        self.assertFalse(out["blocked"])

    def test_neoforge_dependencies_honor_sides_and_maven_ranges(self):
        pid = self.store.create("NeoForge", "1.20.1", "neoforge")

        def forge(id, dependencies=""):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as jar:
                jar.writestr(
                    "META-INF/neoforge.mods.toml",
                    f'[[mods]]\nmodId="{id}"\nversion="2.0"\n' + dependencies,
                )
            return data.getvalue()

        self.store.content_folder(pid, "mods").joinpath("existing.jar").write_bytes(
            forge("other")
        )
        relation = '[[dependencies.newmod]]\nmodId="other"\ntype="incompatible"\nversionRange="[2.0,3.0)"\n'
        self.assertTrue(self.check(forge("newmod", relation), pid)["blocked"])
        self.assertFalse(
            self.check(forge("newmod", relation + 'side="SERVER"\n'), pid)["blocked"]
        )
        self.assertFalse(
            self.check(forge("newmod", relation.replace("[2.0", "(2.0")), pid)[
                "blocked"
            ]
        )

    def test_nested_requirements_use_fabric_caret_and_tilde(self):
        (self.folder / "lib.jar").write_bytes(fabric({"id": "lib", "version": "0.4.2"}))
        nested = fabric({"id": "bridge", "version": "1", "depends": {"lib": "^0.3", "other": "~2.1"}})
        result = self.check(fabric({"id": "outer", "version": "1", "jars": [{"file": "lib/io.jar"}]}, nested))
        self.assertEqual({row["id"]: row["installed"] for row in result["required"]}, {"lib": True, "other": False})
        (self.folder / "other.jar").write_bytes(fabric({"id": "other", "version": "2.2"}))
        result = self.check(fabric({"id": "outer", "version": "1", "jars": [{"file": "lib/io.jar"}]}, nested))
        self.assertFalse(next(row["installed"] for row in result["required"] if row["id"] == "other"))

    def test_forge_manifest_version_used_instead_of_placeholder(self):
        pid = self.store.create("Forge", "1.20.1", "forge")
        dep = self.store.content_folder(pid, "mods") / "library.jar"
        with zipfile.ZipFile(dep, "w") as jar:
            jar.writestr("META-INF/mods.toml", '[[mods]]\nmodId="library"\nversion="${file.jarVersion}"\n')
            jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\nImplementation-Version: 1.4\r\n\r\n")
        incoming = io.BytesIO()
        with zipfile.ZipFile(incoming, "w") as jar:
            jar.writestr("META-INF/mods.toml", '[[mods]]\nmodId="incoming"\nversion="1"\n'
                         '[[dependencies.incoming]]\nmodId="library"\nmandatory=true\nversionRange="[2,)"\n')
        result = self.check(incoming.getvalue(), pid)
        self.assertFalse(result["required"][0]["installed"])
        self.assertIn("другая версия", result["warnings"][0])

    def test_large_descriptor_and_duplicate_root_ids(self):
        (self.folder / "renamed.jar").write_bytes(
            fabric({"id": "existing", "version": "1"})
        )
        out = self.check(fabric({"id": "existing", "version": "2"}))
        self.assertIn("renamed.jar", out["conflicts"][0])
        out = self.check(fabric({"id": "large", "junk": "x" * (1024 * 1024)}))
        self.assertFalse(out["blocked"])
        self.assertTrue(out["warnings"])

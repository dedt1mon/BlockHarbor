"""Shared ZIPs resolve Quick Skin at launch and never carry a player's identity."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import aliases, build_archive, game_wardrobe
from launcher.experience import Experience
from launcher.store import Store


def mod_jar(mod_id, version):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as jar:
        jar.writestr("META-INF/neoforge.mods.toml", f'''modLoader="javafml"
loaderVersion="[4,)"
[[mods]]
modId="{mod_id}"
version="{version}"
[[dependencies.{mod_id}]]
modId="minecraft"
type="required"
versionRange="[1.21.1,1.21.2)"
''')
    return data.getvalue()


class WardrobeArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.author = Store(self.root / "author")
        self.friend = Store(self.root / "friend")
        self.assets = {}
        for attribute, mod_id, version in (("_QUICKSKIN", "quickskin", "5.0.2"),
                                            ("_ARCHITECTURY", "architectury", "13.0.12")):
            actual = getattr(game_wardrobe, attribute)
            data = mod_jar(mod_id, version)
            release = aliases.ModRelease(actual.title, actual.mod_ids, actual.url,
                                         len(data), hashlib.sha512(data).hexdigest())
            self.enterContext(patch.object(game_wardrobe, attribute, release))
            self.assets[release] = data
        self.download = self.enterContext(patch.object(aliases, "_download", side_effect=lambda release: self.assets[release]))

    def test_zip_and_publication_roundtrip_excludes_quickskin_and_restores_official_dependency(self):
        pid = self.author.create("Для друзей", "1.21.1", "neoforge")
        source = self.author.folder(pid)
        game_wardrobe.sync(self.author.root, source, "1.21.1", "neoforge", [], install=True)
        # Identify by metadata, even if the user renamed the original JAR.
        (source / "mods" / game_wardrobe._QUICKSKIN.filename).rename(source / "mods/renamed-client.jar")
        public_config = b'{"graphics":"high"}'
        private = {
            "config/quickskin-client.json": b'{"accountNicknames":{"private-account":"PrivatePlayer"},"activeSkinHash":"private-choice"}',
            "config/skin-preferences.json": b'{"preferences":{"private-choice":{"modelType":"slim"}}}',
            "quickskin/uploads/skins/private.png": b"private skin",
            "saves/private-world/level.dat": b"private world",
        }
        for name, data in {"config/renderer.json": public_config, **private}.items():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        source_jar = (source / "mods/renamed-client.jar").read_bytes()
        for public in (False, True):
            with self.subTest(public=public):
                self.download.reset_mock()
                output = io.BytesIO()
                options = {"catalog_name": "Для друзей", "include_configs": True} if public else {}
                build_archive.export_build(self.author, pid, output, worlds=False, **options)
                self.download.assert_not_called()
                with zipfile.ZipFile(output) as archive:
                    self.assertNotIn("mods/renamed-client.jar", archive.namelist())
                    self.assertIn("mods/" + game_wardrobe._ARCHITECTURY.filename, archive.namelist())
                    self.assertEqual(archive.read("config/renderer.json"), public_config)
                    self.assertFalse(set(private).intersection(archive.namelist()))
                    self.assertNotIn(b"PrivatePlayer", archive.read(build_archive.MANIFEST))
                listing = {"name": "Для друзей", "minecraft": "1.21.1", "loader": "neoforge"} if public else None
                imported = build_archive.import_build(self.friend, output, catalog_listing=listing)
                folder = self.friend.folder(imported["id"])
                self.download.assert_not_called()
                self.assertFalse((folder / "mods" / game_wardrobe._QUICKSKIN.filename).exists())
                self.assertEqual((folder / "config/renderer.json").read_bytes(), public_config)
                prepared = Experience(self.friend).prepare_game_wardrobe(imported["id"])
                self.assertTrue(prepared["ready"])
                self.download.assert_called_once_with(game_wardrobe._QUICKSKIN)
                self.assertEqual((folder / "mods" / game_wardrobe._QUICKSKIN.filename).read_bytes(), source_jar)
                for name, data in private.items():
                    self.assertEqual((source / name).read_bytes(), data)
                    self.assertFalse((folder / name).exists())
                self.assertEqual((source / "mods/renamed-client.jar").read_bytes(), source_jar)

    def test_unrestorable_or_disabled_quickskin_is_not_silently_exported(self):
        cases = [
            ("1.20.1", "fabric", "renamed.jar", self.assets[game_wardrobe._QUICKSKIN]),
            ("1.21.1", "neoforge", "renamed.jar", mod_jar("quickskin", "5.0.3")),
            ("1.21.1", "neoforge", "renamed.jar.disabled", self.assets[game_wardrobe._QUICKSKIN]),
        ]
        for version, loader, filename, data in cases:
            with self.subTest(version=version, loader=loader, filename=filename):
                pid = self.author.create("Сборка", version, loader)
                path = self.author.folder(pid) / "mods" / filename
                path.write_bytes(data)
                for public in (False, True):
                    options = {"catalog_name": "Сборка"} if public else {}
                    with self.assertRaisesRegex(ValueError, "Quick Skin нельзя включать в ZIP"):
                        build_archive.export_build(self.author, pid, io.BytesIO(), **options)
                self.assertEqual(path.read_bytes(), data)
        self.download.assert_not_called()

    def test_import_rejects_personal_quickskin_settings_without_creating_a_build(self):
        for filename in ("quickskin-client.json", "skin-preferences.json"):
            with self.subTest(filename=filename):
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, "w") as archive:
                    archive.writestr(build_archive.MANIFEST, json.dumps({"format": "BlockHarbor", "formatVersion": 1,
                        "name": "Чужая сборка", "minecraft": "1.21.1", "loader": "neoforge"}))
                    archive.writestr("config/" + filename, "{}")
                with self.assertRaisesRegex(ValueError, "вне состава сборки"):
                    build_archive.import_build(self.friend, payload)
                self.assertEqual(self.friend.all(), [])


if __name__ == "__main__":
    unittest.main()

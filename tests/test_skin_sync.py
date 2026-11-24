import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from launcher import skin_sync
from PIL import Image


class SkinSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.instance = Path(temporary.name)
        self.config = self.instance / "config/skinss/settings.json"

    def png(self, legacy=False):
        image = Image.new("RGBA", (64, 32 if legacy else 64), (70, 100, 200, 255))
        image.putpixel((44, 20), (0, 255, 0, 255))
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    def test_complete_catalog_installs_pinned_receiver_for_observers(self):
        for key, asset in skin_sync._CATALOG.items():
            loader, version = key.split("/")
            folder = self.instance / key
            with self.subTest(release=key):
                jar = skin_sync.prepare(folder, version, loader)
                self.assertTrue(
                    skin_sync.managed_file(folder, version, loader, jar.name)
                )
                self.assertFalse((folder / "config/skinss/settings.json").exists())
                self.assertEqual(skin_sync.prepare(folder, version, loader), jar)
                self.assertEqual(jar.stat().st_size, asset["size"])
                mods = list((folder / "mods").glob("*.jar"))
                self.assertEqual(len(mods), 1)
                self.assertTrue(mods[0].samefile(jar))
        self.assertIsNone(skin_sync.prepare(self.instance, "1.20.1", "vanilla"))
        self.assertFalse((self.instance / "mods").exists())

    def test_selected_png_and_models_match_mod_configuration(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"selectedCape": "user.gif", "more": True}))
        for game, model, field in [
            ("1.20.1", "slim", {"skinModel": "slim"}),
            ("1.20.1", "classic", {"skinModel": "classic"}),
            ("1.21.8", "slim", {"slim": True}),
        ]:
            with self.subTest(game=game, model=model):
                # Use the same world profile settings, but replace the game version's mod.
                for jar in (self.instance / "mods").glob("*.jar"):
                    jar.unlink()
                skin_sync.prepare(self.instance, game, "fabric", self.png(), model)
                cfg = json.loads(self.config.read_text())
                self.assertEqual({k: cfg[k] for k in field}, field)
                self.assertEqual(cfg["selectedCape"], "user.gif")
                self.assertTrue(cfg["more"])
                skin = self.config.parent / "skin" / cfg["selectedSkin"]
                with Image.open(skin) as image:
                    self.assertEqual(image.size, (64, 64))
                    self.assertEqual(image.getpixel((44, 20)), (0, 255, 0, 255))
                mtime = self.config.stat().st_mtime_ns
                skin_sync.prepare(self.instance, game, "fabric", self.png(), model)
                self.assertEqual(self.config.stat().st_mtime_ns, mtime)

    def test_legacy_left_limbs_are_mirrored(self):
        skin_sync.prepare(self.instance, "1.20.1", "forge", self.png(True), "classic")
        cfg = json.loads(self.config.read_text())
        with Image.open(self.config.parent / "skin" / cfg["selectedSkin"]) as image:
            self.assertEqual(image.size, (64, 64))
            self.assertEqual(image.getpixel((39, 52)), (0, 255, 0, 255))
            self.assertEqual(image.getpixel((32, 0))[3], 0)
        with self.assertRaisesRegex(ValueError, "Classic"):
            skin_sync.prepare(self.instance, "1.20.1", "forge", self.png(True), "slim")
        self.assertEqual(json.loads(self.config.read_text()), cfg)

    def test_renamed_duplicate_and_corrupted_jar_are_preserved(self):
        target = skin_sync.prepare(self.instance, "1.20.1", "forge")
        renamed = target.with_name("renamed.jar")
        target.rename(renamed)
        self.assertEqual(skin_sync.prepare(self.instance, "1.20.1", "forge"), renamed)
        with zipfile.ZipFile(target, "w") as duplicate:
            duplicate.writestr("META-INF/mods.toml", '[[mods]]\nmodId="skinss"')
        with self.assertRaisesRegex(ValueError, "другая копия"):
            skin_sync.prepare(self.instance, "1.20.1", "forge")
        self.assertTrue(target.is_file() and renamed.is_file())
        renamed.unlink()
        with self.assertRaisesRegex(ValueError, "другая копия"):
            skin_sync.prepare(self.instance, "1.20.1", "forge")
        self.assertFalse(
            skin_sync.managed_file(self.instance, "1.20.1", "forge", target.name)
        )

    def test_bad_or_redirected_settings_are_left_untouched(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text("broken")
        with self.assertRaisesRegex(ValueError, "настройки"):
            skin_sync.prepare(self.instance, "1.20.1", "fabric", self.png())
        self.assertEqual(self.config.read_text(), "broken")
        real = self.config.parent / "original.json"
        self.config.rename(real)
        self.config.symlink_to(real)
        with self.assertRaisesRegex(ValueError, "перенаправлены"):
            skin_sync.prepare(self.instance, "1.20.1", "fabric", self.png())
        self.assertEqual(real.read_text(), "broken")

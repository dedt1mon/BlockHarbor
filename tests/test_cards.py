import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import cards
from PIL import Image
from PIL.PngImagePlugin import PngInfo


class CardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.card = self.base / "player.bhplayer"
        self.skin = self.base / "skin.png"
        metadata = PngInfo()
        metadata.add_text("creationNote", "private-data")
        Image.new("RGBA", (64, 64), "red").save(self.skin, pnginfo=metadata)

    def test_round_trip_and_strips_metadata(self):
        self.card.write_bytes(cards.build_card("CraftPlayer", "DedTimon", self.skin))
        player, alias, png = cards.read_card(self.card)
        self.assertEqual((player, alias), ("CraftPlayer", "DedTimon"))
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertNotIn(b"private-data", png)
        self.assertNotIn(str(self.base), self.card.read_text())

    def test_mapping_without_skin(self):
        self.card.write_bytes(cards.build_card("CraftPlayer", "DedTimon", None))
        with (
            patch.object(cards.aliases, "apply_alias") as install,
            patch.object(cards.aliases, "ensure_skin_support") as skin,
        ):
            self.assertIn(
                "DedTimon",
                cards.apply_card(self.card, self.base, own_name="LocalPlayer"),
            )
        install.assert_called_once_with(self.base, "CraftPlayer", "DedTimon")
        skin.assert_not_called()

    def test_rejects_own_identity_before_installing_anything(self):
        self.card.write_bytes(cards.build_card("CraftPlayer", "DedTimon", self.skin))
        with (
            patch.object(cards.aliases, "ensure_skin_support") as install,
            self.assertRaisesRegex(ValueError, "собственный"),
        ):
            cards.apply_card(self.card, self.base, own_name="craftplayer")
        install.assert_not_called()

    def test_rejects_unknown_fields_paths_bad_png_and_oversized_card(self):
        good = json.loads(cards.build_card("CraftPlayer", "DedTimon", None))
        for bad in (
            dict(good, mods=["https://example.org/test.jar"]),
            dict(good, profileName="../Bad"),
            dict(good, skinPNG=base64.b64encode(b"not png").decode()),
            dict(good, skinPNG="https://example.org/test.png"),
            dict(good, version=True),
        ):
            self.card.write_text(json.dumps(bad))
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cards.read_card(self.card)
        self.card.write_bytes(b" " * 100_000)
        with self.assertRaisesRegex(ValueError, "64"):
            cards.read_card(self.card)

    def test_applies_skin_to_real_name_without_changing_local_profile(self):
        self.card.write_bytes(cards.build_card("CraftPlayer", "DedTimon", self.skin))
        with (
            patch.object(cards.aliases, "apply_alias") as install,
            patch.object(cards.aliases, "ensure_skin_support"),
        ):
            reply = cards.apply_card(self.card, self.base, own_name="AnotherAccount")
        install.assert_called_once_with(self.base, "CraftPlayer", "DedTimon")
        self.assertIn("скин", reply)
        self.assertTrue(
            (self.base / "CustomSkinLoader/LauncherSkins/skin-CraftPlayer.png").exists()
        )

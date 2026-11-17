import io
import json
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from launcher import skins
from PIL import Image
from PIL.PngImagePlugin import PngInfo


class SkinTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.file = self.base / "input.png"
        self.make_image((64, 64))

    def make_image(self, size):
        info = PngInfo()
        info.add_text("Editor", "private-personal-metadata")
        Image.new("RGBA", size, (213, 90, 19, 255)).save(self.file, pnginfo=info)

    def test_preview_renders_both_layouts(self):
        for size in [(64, 32), (64, 64)]:
            with self.subTest(size=size):
                self.make_image(size)
                image = skins.skin_preview(self.file, "classic", 3)
                self.assertEqual(skins.validate_skin(self.file), size)
                self.assertEqual(image.size, (48, 96))
                self.assertEqual(image.getpixel((23, 12)), (213, 90, 19, 255))
                self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_rejects_bad_format_dimension_and_truncation(self):
        self.make_image((512, 512))
        with self.assertRaises(skins.SkinError):
            skins.validate_skin(self.file)
        self.make_image((64, 64))
        self.file.write_bytes(self.file.read_bytes()[:46])
        with self.assertRaises(skins.SkinError):
            skins.validate_skin(self.file)
        self.file.write_text("not an image")
        with self.assertRaises(skins.SkinError):
            skins.validate_skin(self.file)

    def test_upload_sends_checked_pixels_and_selected_model(self):
        with patch("launcher.skins._profile_request") as request:
            skins.upload_skin(self.file, "slim", "token")
        route, token, verb, body, media = request.call_args.args
        self.assertEqual((route, token, verb), ("/skins", "token", "POST"))
        form = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + media + "\r\n\r\n").encode() + body
        )
        model, image = list(form.iter_parts())
        self.assertEqual(model.get_payload(), "slim")
        self.assertEqual(image.get_filename(), "skin.png")
        with Image.open(io.BytesIO(image.get_payload(decode=True))) as decoded:
            self.assertEqual(decoded.size, (64, 64))
            self.assertNotIn("Editor", decoded.info)

    def test_legacy_slim_is_rejected_before_upload(self):
        self.make_image((64, 32))
        with (
            patch("launcher.skins._profile_request") as request,
            self.assertRaises(skins.SkinError),
        ):
            skins.upload_skin(self.file, "slim", "token")
        request.assert_not_called()

    def test_local_lookup_and_players_are_distinct(self):
        game_dir = self.base / "instance"
        first = skins.install_local_skin(self.file, game_dir, "Player_A")
        second = skins.install_local_skin(self.file, game_dir, "Player_B")
        root = game_dir / "CustomSkinLoader"
        listing = json.loads((root / "CustomSkinLoader.json").read_text())["loadlist"][
            0
        ]
        self.assertEqual(
            root / listing["skin"].replace("{USERNAME}", "Player_A"), first
        )
        self.assertTrue(second.is_file())
        self.assertNotEqual(first, second)
        with Image.open(first) as data:
            self.assertNotIn("Editor", data.info)
        with self.assertRaises(skins.SkinError):
            skins.install_local_skin(self.file, game_dir, "../Bad")

    def test_rename_only_accepts_supported_names(self):
        with patch("launcher.skins._profile_request") as request:
            skins.change_name(" DedTimon ", "token")
            request.assert_called_once_with("/name/DedTimon", "token", "PUT", b"")
            with self.assertRaises(skins.SkinError):
                skins.change_name("John/Smith", "token")
            self.assertEqual(request.call_count, 1)

    def test_expired_account_error_hides_token(self):
        error = HTTPError(
            "https://api.minecraftservices.com", 401, "Unauthorized", {}, io.BytesIO()
        )
        with (
            patch("launcher.skins.build_opener") as opener,
            self.assertRaises(skins.SkinError) as result,
        ):
            opener.return_value.open.side_effect = error
            skins.upload_skin(self.file, "classic", "test-personal-token")
        self.assertIn("Войдите", str(result.exception))
        self.assertNotIn("test-personal-token", str(result.exception))

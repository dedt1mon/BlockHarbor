import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from launcher import skin_network, skins
from launcher.store import Store


class SkinNetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.instance = self.root / "game"
        self.instance.mkdir()
        image = io.BytesIO()
        Image.new("RGBA", (64, 64), (32, 48, 64, 255)).save(image, format="PNG")
        self.png = image.getvalue()

    def test_observer_without_png_gets_receiver_and_preserves_official_sources(self):
        with patch.object(skin_network.aliases, "ensure_skin_support") as install:
            result = skin_network.prepare_player(self.store, self.instance, "1.21.1",
                                                 "neoforge", "Friend")
        install.assert_called_once_with(self.instance, "1.21.1", "neoforge")
        self.assertTrue(result["supported"])
        data = json.loads((self.instance / "CustomSkinLoader/CustomSkinLoader.json").read_text())
        self.assertEqual(data["loadlist"][0]["type"], "CustomSkinAPI")
        self.assertEqual(data["loadlist"][0]["root"], skin_network.ROOT)
        self.assertEqual(data["loadlist"][1]["type"], "MojangAPI")
        skin_network.configure_receiver(self.instance, "Friend", local_profile=False)
        data = json.loads((self.instance / "CustomSkinLoader/CustomSkinLoader.json").read_text())
        self.assertEqual(data["loadlist"][0]["type"], "MojangAPI")

    def test_changed_name_does_not_leave_old_local_texture_over_friend(self):
        skins.install_local_skin(self.png, self.instance, "OldPlayer", "classic")
        skins.install_local_skin(self.png, self.instance, "NewPlayer", "slim")
        config = skin_network.configure_receiver(self.instance, "NewPlayer", has_skin=True)
        data = json.loads(config.read_text())
        self.assertEqual(data["loadlist"][0]["name"], "BlockHarbor newplayer")
        self.assertEqual(data["loadlist"][0]["model"], "slim")
        self.assertFalse(any(site["name"] == "BlockHarbor oldplayer" for site in data["loadlist"]))
        before = config.stat().st_mtime_ns
        skin_network.configure_receiver(self.instance, "NewPlayer", has_skin=True)
        self.assertEqual(before, config.stat().st_mtime_ns)

    def test_publication_refresh_change_parallel_names_and_remove_keep_owner_private(self):
        class API:
            origin = "https://skin-test.invalid"
            calls = []

            def request(self, method, name, token, pixels=None, model="classic"):
                self.calls.append((method, name, token, pixels, model))
                return ({"removed": True} if method == "DELETE" else
                        {"username": name, "sha256": hashlib.sha256(pixels).hexdigest(), "model": model})

        api = API()
        first = skin_network.synchronize(self.store, "Player", self.png, "slim", api=api, now=1000)
        self.assertTrue(first["shared"])
        cached = skin_network.synchronize(self.store, "Player", self.png, "slim", api=api, now=1001)
        self.assertTrue(cached["cached"])
        skin_network.synchronize(self.store, "Player", self.png, "classic", api=api, now=1002)
        skin_network.synchronize(self.store, "Renamed", self.png, "classic", api=api, now=1003)
        still_playing = skin_network.synchronize(self.store, "Player", self.png, "classic", api=api, now=1003)
        self.assertTrue(still_playing["shared"] and still_playing["cached"])
        skin_network.synchronize(self.store, "Renamed", api=api, now=1004)
        self.assertEqual([call[:2] for call in api.calls],
                         [("PUT", "Player"), ("PUT", "Player"), ("PUT", "Renamed"), ("DELETE", "Renamed")])
        self.assertEqual(api.calls[0][2], api.calls[1][2])
        self.assertEqual(api.calls[2][2], api.calls[3][2])
        self.assertNotEqual(api.calls[0][2], api.calls[2][2])
        self.assertRegex(api.calls[0][2], r"^[a-f0-9]{64}$")
        self.assertNotIn(api.calls[0][2], json.dumps(first))

    def test_failed_publication_does_not_claim_success_or_prevent_launch(self):
        notices = []
        with (patch.object(skin_network.aliases, "ensure_skin_support"),
              patch.object(skin_network.SkinAPI, "request", side_effect=skin_network.SkinNetworkError("Сервис недоступен")) as request):
            result = skin_network.prepare_player(self.store, self.instance, "1.21.1", "neoforge",
                                                 "Player", self.png, notify=lambda *a, **k: notices.append((a, k)))
            self.assertFalse(result["shared"])
            self.assertEqual(result["warning"], "Сервис недоступен")
            retry = skin_network.prepare_player(self.store, self.instance, "1.21.1", "neoforge", "Player", self.png)
            self.assertTrue(retry["deferred"])
            request.assert_called_once()
        self.assertEqual(len(notices), 1)
        config = json.loads((self.instance / "CustomSkinLoader/CustomSkinLoader.json").read_text())
        self.assertEqual(config["loadlist"][0]["name"], "BlockHarbor player")
        self.assertEqual(config["loadlist"][1]["type"], "CustomSkinAPI")

    def test_vanilla_and_authenticated_names_are_not_published_as_offline_claims(self):
        with (patch.object(skin_network.aliases, "ensure_skin_support") as install,
              patch.object(skin_network, "synchronize") as publish):
            result = skin_network.prepare_player(self.store, self.instance, "1.21.1", "vanilla", "Player", self.png)
            self.assertFalse(result["supported"])
            install.assert_not_called()
            skin_network.prepare_player(self.store, self.instance, "1.21.1", "neoforge",
                                        "Player", self.png, mode="direct")
            publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()

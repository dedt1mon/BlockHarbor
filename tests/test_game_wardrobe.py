import hashlib
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from launcher import aliases, game_wardrobe, skin_sync, skins
from launcher.experience import Experience
from launcher.store import Store


def jar_blob(mod_id, version):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as jar:
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
    return stream.getvalue()


class GameWardrobeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root)
        self.pid = self.store.create("KingdomWars", "1.21.1", "neoforge")
        self.folder = self.store.folder(self.pid)
        self.experience = Experience(self.store)
        self.player = str(uuid.UUID("11111111-2222-3333-8444-555555555555"))
        self.releases = {}
        for attribute, mod_id, version in (("_QUICKSKIN", "quickskin", "5.0.2"),
                                            ("_ARCHITECTURY", "architectury", "13.0.12")):
            blob = jar_blob(mod_id, version)
            release = aliases.ModRelease(mod_id, frozenset({mod_id}), f"https://cdn.modrinth.com/data/test/{mod_id}.jar",
                                         len(blob), hashlib.sha512(blob).hexdigest())
            self.enterContext(patch.object(game_wardrobe, attribute, release))
            self.releases[release] = blob
        self.download = self.enterContext(patch.object(aliases, "_download", side_effect=lambda release: self.releases[release]))
        self.first = self.entry(1, "Рыцарь", "classic")
        self.second = self.entry(2, "Странник", "slim")
        self.entries = [self.first, self.second]

    def entry(self, number, name, model):
        stream = io.BytesIO()
        Image.new("RGBA", (64, 64), (number * 30, 80, 120, 255)).save(stream, "PNG")
        digest = skins.save_texture(self.root, stream.getvalue())
        return {"id": f"{number:032x}", "name": name, "digest": digest, "model": model}

    def sync(self, entries=None, **options):
        return game_wardrobe.sync(self.root, self.folder, "1.21.1", "neoforge",
                                  self.entries if entries is None else entries, **options)

    def read(self, relative):
        return json.loads((self.folder / relative).read_text("utf-8"))

    def native_hash(self, entry):
        pixels = game_wardrobe._texture(self.root / "skins" / (entry["digest"] + ".png"), entry["model"], "quickskin")
        return "sha256-" + hashlib.sha256(pixels).hexdigest()

    def test_prepares_pinned_dependencies_and_all_choices_without_touching_worlds(self):
        world = self.folder / "saves/world/level.dat"
        world.parent.mkdir(parents=True)
        world.write_bytes(b"world bytes")
        result = self.sync(install=True)
        self.assertTrue(result["ready"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.download.call_count, 2)
        images = list((self.folder / "quickskin/uploads/skins").glob("*.png"))
        self.assertEqual(len(images), 2)
        self.assertTrue(any("Рыцарь" in image.name for image in images))
        self.assertTrue(any("slim" in image.name for image in images))
        self.assertEqual(world.read_bytes(), b"world bytes")
        models = self.read("config/skin-preferences.json")["preferences"]
        self.assertEqual({row["modelType"] for row in models.values()}, {"classic", "slim"})
        self.assertTrue(game_wardrobe.describe(self.folder, "1.21.1", "neoforge")["ready"])
        self.sync(install=True)
        self.assertEqual(self.download.call_count, 2, "an offline repeat reuses verified files")

    def test_failed_dependency_download_never_publishes_half_a_mod_set(self):
        def fail(release):
            if release == game_wardrobe._QUICKSKIN:
                raise skins.SkinError("offline")
            return self.releases[release]
        self.download.side_effect = fail
        with self.assertRaisesRegex(ValueError, "offline"):
            self.sync(install=True)
        self.assertEqual(list((self.folder / "mods").glob("*.jar")), [])
        self.assertFalse((self.folder / "quickskin/uploads/skins").exists())

    def test_download_checksum_mismatch_is_rejected_before_publication(self):
        self.download.return_value = b"corrupted"
        self.download.side_effect = None
        with self.assertRaisesRegex(ValueError, "проверку"):
            self.sync(install=True)
        self.assertEqual(list((self.folder / "mods").glob("*.jar")), [])

    def test_existing_compatible_architectury_is_preserved(self):
        path = self.folder / "mods/already-installed.jar"
        path.write_bytes(jar_blob("architectury", "13.0.8"))
        before = path.read_bytes()
        self.sync(install=True)
        self.assertEqual(path.read_bytes(), before)
        self.download.assert_called_once_with(game_wardrobe._QUICKSKIN)
        self.assertEqual(len(list(path.parent.glob("*.jar"))), 2)
        self.assertTrue(game_wardrobe.describe(self.folder, "1.21.1", "neoforge")["ready"])

    def test_incompatible_or_duplicate_dependency_is_never_replaced(self):
        path = self.folder / "mods/existing.jar"
        for version in ("12.1.0", "13.0.5"):
            raw = jar_blob("architectury", version)
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "существующий файл"):
                self.sync(install=True)
            self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(self.releases[game_wardrobe._ARCHITECTURY])
        other = path.with_name("second.jar")
        other.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ValueError, "несколько версий"):
            self.sync(install=True)
        self.download.assert_not_called()

    def test_disabled_receiver_or_dependency_is_not_silently_enabled(self):
        for release in (game_wardrobe._QUICKSKIN, game_wardrobe._ARCHITECTURY):
            with self.subTest(mod=release.title):
                path = self.folder / "mods" / (release.filename + ".disabled")
                original = self.releases[release]
                path.write_bytes(original)
                result = self.experience.prepare_game_wardrobe(self.pid)
                self.assertFalse(result["ready"])
                self.assertIn("отключён", result["warning"])
                self.assertIn("Включи", result["warning"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob("*.jar")), [])
                self.download.assert_not_called()
                path.unlink()

    def test_event_updates_only_png_and_preserves_live_config_and_identity(self):
        self.sync(install=True, current=self.first, player_name="DedTimon", player_id=self.player)
        config = self.folder / "config/quickskin-client.json"
        changed = self.read("config/quickskin-client.json")
        changed.update(activeSkinHash=self.native_hash(self.second), accountNicknames={"custom": "kept"}, activeCapeHash="cape")
        changed["accountSkins"]["skins"][self.player + ":DedTimon"] = changed["activeSkinHash"]
        config.write_text(json.dumps(changed))
        before = config.read_bytes()
        third = self.entry(3, "Новый", "slim")
        self.sync(self.entries + [third], current=self.first, running=True)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.download.call_count, 2)
        self.assertEqual(len(list((self.folder / "quickskin/uploads/skins").glob("*.png"))), 3)
        # A skin chosen in-game survives a restart if the launcher choice stayed.
        self.sync(self.entries + [third], current=self.first, install=True,
                  player_name="DedTimon", player_id=self.player)
        self.assertEqual(self.read("config/quickskin-client.json")["activeSkinHash"], changed["activeSkinHash"])
        # A NEW explicit launcher choice is reflected at the next launch only.
        self.sync(self.entries + [third], current=third, running=True)
        self.sync(self.entries + [third], current=third, install=True,
                  player_name="DedTimon", player_id=self.player)
        updated = self.read("config/quickskin-client.json")
        self.assertEqual(updated["activeSkinHash"], self.native_hash(third))
        self.assertEqual(updated["accountSkins"]["skins"][self.player + ":DedTimon"], updated["activeSkinHash"])
        self.assertEqual(updated["accountNicknames"], {"custom": "kept"})
        self.assertEqual(updated["activeCapeHash"], "cape")

    def test_live_event_without_receiver_never_installs_anything(self):
        self.assertFalse(self.sync(running=True)["ready"])
        self.download.assert_not_called()
        self.assertFalse((self.folder / "quickskin").exists())
        with self.assertRaisesRegex(ValueError, "следующем запуске"):
            self.sync(running=True, install=True)

    def test_cleanup_only_removes_unchanged_owned_inactive_copies(self):
        self.sync(install=True)
        directory = self.folder / "quickskin/uploads/skins"
        foreign = directory / "my-skin.png"
        foreign.write_bytes(b"my file")
        originals = {path.name: path.read_bytes() for path in directory.glob("BH*.png")}
        self.sync([], running=True)
        self.assertEqual(len(list(directory.glob("BH*.png"))), 2)
        own_modified = next(directory.glob("BH*.png"))
        own_modified.write_bytes(b"edited by owner")
        self.sync([])
        self.assertEqual(foreign.read_bytes(), b"my file")
        self.assertEqual(own_modified.read_bytes(), b"edited by owner")
        self.assertEqual(len(list(directory.glob("BH*.png"))), 1)
        self.assertEqual(len(originals), 2)

    def test_cleanup_keeps_a_native_selected_skin(self):
        self.sync(install=True, current=self.second, player_name="DedTimon", player_id=self.player)
        self.sync([])
        files = list((self.folder / "quickskin/uploads/skins").glob("BH*.png"))
        self.assertEqual(len(files), 1)
        self.assertIn("Странник", files[0].name)

    def test_redirected_paths_and_changed_source_fail_without_writing(self):
        destination = self.folder / "quickskin"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        destination.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "перенаправлена"):
            self.sync(install=True)
        self.assertEqual(list(elsewhere.iterdir()), [])
        destination.unlink()
        source = self.root / "skins" / (self.second["digest"] + ".png")
        source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "изменён"):
            self.sync(install=True)
        self.assertFalse((self.folder / "quickskin").exists())

    def test_skinss_menu_receives_all_skins_and_preserves_native_selection(self):
        target = self.root / "skinss-instance"
        chosen = self.root / "skins" / (self.first["digest"] + ".png")
        skin_sync.prepare(target, "1.20.1", "fabric", chosen, "classic")
        config = target / "config/skinss/settings.json"
        before = config.read_bytes()
        result = game_wardrobe.sync(self.root, target, "1.20.1", "fabric", self.entries, running=True)
        self.assertTrue(result["ready"])
        self.assertEqual(len(list((target / "config/skinss/skin").glob("BH*.png"))), 2)
        self.assertEqual(config.read_bytes(), before)
        self.assertIn("/skinss", game_wardrobe.describe(target, "1.20.1", "fabric")["hint"])
        self.download.assert_not_called()

    def test_skinss_launch_preserves_native_choice_until_launcher_choice_changes(self):
        pid = self.store.create("Fabric", "1.20.1", "fabric")
        target = self.store.folder(pid)
        self.store.set_many({"skinDigest": self.first["digest"], "model": "classic"})
        self.assertTrue(self.experience.prepare_game_wardrobe(pid)["ready"])
        config = target / "config/skinss/settings.json"
        choice = json.loads(config.read_text())
        choice.update(selectedSkin="my-own-skin.png", skinModel="slim")
        config.write_text(json.dumps(choice))
        self.assertTrue(self.experience.prepare_game_wardrobe(pid)["ready"])
        self.assertEqual(json.loads(config.read_text()), choice)
        self.store.set_many({"skinDigest": self.second["digest"], "model": "slim"})
        self.assertTrue(self.experience.prepare_game_wardrobe(pid)["ready"])
        selected = json.loads(config.read_text())
        self.assertTrue(selected["selectedSkin"].startswith("blockharbor-"))
        self.assertEqual(selected["skinModel"], "slim")

    def test_same_pixels_with_two_models_remain_distinct_native_choices(self):
        slim = {**self.first, "id": "3" * 32, "name": "Рыцарь Slim", "model": "slim"}
        self.sync([self.first, slim], install=True, current=slim, player_name="DedTimon", player_id=self.player)
        self.assertNotEqual(self.native_hash(self.first), self.native_hash(slim))
        models = self.read("config/skin-preferences.json")["preferences"]
        self.assertEqual(models[self.native_hash(self.first)]["modelType"], "classic")
        self.assertEqual(models[self.native_hash(slim)]["modelType"], "slim")
        self.assertEqual(self.read("config/quickskin-client.json")["activeSkinHash"], self.native_hash(slim))
        pngs = list((self.folder / "quickskin/uploads/skins").glob("*.png"))
        with Image.open(pngs[0]) as a, Image.open(pngs[1]) as b:
            self.assertEqual(a.tobytes(), b.tobytes())

    def test_deep_corrupt_json_does_not_block_a_launch_or_wardrobe_page(self):
        self.sync(install=True)
        corrupted = "[" * 2000 + "0" + "]" * 2000
        marker = self.folder / game_wardrobe._MARKER
        valid_marker = marker.read_bytes()
        marker.write_text(corrupted)
        self.assertFalse(game_wardrobe.describe(self.folder, "1.21.1", "neoforge")["ready"])
        self.assertIn("warning", self.experience.prepare_game_wardrobe(self.pid))
        marker.write_bytes(valid_marker)
        config = self.folder / "config/quickskin-client.json"
        config.write_text(corrupted)
        self.assertIn("warning", self.experience.prepare_game_wardrobe(self.pid))
        self.assertEqual(config.read_text(), corrupted)
        self.sync([])
        self.assertEqual(len(list((self.folder / "quickskin/uploads/skins").glob("*.png"))), 2)

    def test_unsupported_game_loader_does_not_get_a_foreign_mod(self):
        self.assertFalse(game_wardrobe.sync(self.root, self.folder, "1.20.1", "neoforge", self.entries, install=True)["supported"])
        self.download.assert_not_called()
        self.assertEqual(list((self.folder / "mods").iterdir()), [])

    def test_experience_events_copy_wardrobe_but_state_polling_does_not(self):
        self.store.set_many({"skinDigest": self.first["digest"], "model": "classic", "nickname": "DedTimon"})
        self.experience.prepare_game_wardrobe(self.pid)
        experience = Experience(self.store, running=lambda _pid: True)
        experience.handle("experienceSaveSkin", {"name": "Рыцарь"})
        state = experience.handle("experienceState", {"id": self.pid})
        self.assertTrue(state["gameWardrobe"]["running"])
        self.assertTrue(state["gameWardrobe"]["ready"])
        choice = experience.handle("experienceUseSkin", {"skin": state["wardrobe"][0]["id"]})
        self.assertIn("игровом меню", choice["message"])
        self.assertEqual(self.store.get("nickname"), "DedTimon")
        with patch.object(game_wardrobe, "sync", side_effect=AssertionError("state must not sync")), patch.object(game_wardrobe, "describe", side_effect=AssertionError("polling must not scan")):
            experience.state(self.pid)
        self.assertEqual(self.download.call_count, 2)


if __name__ == "__main__":
    unittest.main()

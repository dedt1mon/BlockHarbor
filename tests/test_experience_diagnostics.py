import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from launcher.build_care import BuildCare
from launcher.diagnostics import MAX_LOG, analyze, capture_stderr, redact, report_text
from launcher.experience import Experience
from launcher.performance_profiles import PerformanceProfiles
from launcher.skins import save_texture
from launcher.store import Store


class ExperienceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.pid = self.store.create("Мир", "1.20.1", "fabric")
        self.folder = self.store.folder(self.pid)
        self.experience = Experience(self.store)

    @staticmethod
    def png(size=(64, 64)):
        out = io.BytesIO()
        Image.new("RGBA", size, (50, 160, 80, 255)).save(out, "PNG")
        return out.getvalue()

    def test_existing_database_and_empty_install_are_preserved(self):
        reopened = Store(self.root)
        self.assertEqual(reopened.profile(self.pid)["name"], "Мир")
        with tempfile.TemporaryDirectory() as tmp:
            clean = Store(Path(tmp))
            Experience(clean)
            BuildCare(clean)
            self.assertEqual(clean.all(), [])

    def test_metadata_changes_only_selected_build(self):
        other = self.store.create("Другой", "1.20.1", "vanilla")
        self.experience.handle("experienceSaveBuild", {"id":self.pid,"name":"С друзьями","description":"Проверка","group":"Общие","favorite":True,"color":"blue"})
        self.assertEqual(self.store.profile(other)["name"], "Другой")
        self.assertEqual(self.store.profile(self.pid)["name"], "С друзьями")
        self.assertEqual(self.experience.metadata()[self.pid]["favorite"], 1)

    def test_cover_is_normalized_and_addressed_by_content_hash(self):
        result = self.experience.set_cover(self.pid, self.png((500,300)))
        path = self.experience.media(result["cover"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result["cover"])
        with Image.open(path) as image:
            self.assertEqual(image.size, (960,540))
        with self.assertRaises(ValueError):
            self.experience.media("../../settings.sqlite3")
        with self.assertRaises(ValueError):
            self.experience.set_cover(self.pid, b"not png")

    def test_wardrobe_roundtrip_preserves_other_preferences(self):
        digest = save_texture(self.root, self.png())
        self.store.set_many({"skinDigest":digest,"model":"slim","nickname":"DedTimon","memory":"5120"})
        self.experience.handle("experienceSaveSkin", {"name":"Зелёный"})
        row = self.experience.handle("experienceState", {})["wardrobe"][0]
        self.assertTrue(self.experience.media(row["preview"]).is_file())
        self.store.set("model", "classic")
        self.experience.handle("experienceUseSkin", {"skin":row["id"]})
        self.assertEqual(self.store.get("model"), "slim")
        self.assertEqual(self.store.get("memory"), "5120")
        self.experience.handle("experienceDeleteSkin", {"skin":row["id"]})
        self.assertEqual(self.experience.state()["wardrobe"], [])
        self.assertTrue((self.root / "skins" / (self.store.get("skinDigest") + ".png")).is_file())

    def test_gallery_ignores_links_and_caches_thumbnail(self):
        folder = self.folder / "screenshots"
        folder.mkdir()
        (folder / "scene.png").write_bytes(self.png((800,600)))
        (folder / "invalid.png").write_bytes(b"broken")
        (folder / "linked.png").symlink_to(folder / "scene.png")
        first = self.experience.gallery(self.pid)
        self.assertEqual(len(first["images"]), 1)
        with patch.object(self.experience, "_image", side_effect=AssertionError("unchanged thumbnail must be reused")):
            self.assertEqual(self.experience.gallery(self.pid), first)

    def test_media_budget_evicts_unreferenced_images_but_keeps_cover(self):
        import launcher.experience as experience_module
        old = self.experience._image(io.BytesIO(self.png((200,100))))
        cover = self.experience.set_cover(self.pid,self.png((500,300)))["cover"]
        with patch.object(experience_module, "CACHE_BUDGET", 1):
            self.experience._prune_media()
        self.assertFalse(self.experience.media(old).exists())
        self.assertTrue(self.experience.media(cover).exists())

    def test_notifications_redact_secrets_and_deduplicate(self):
        self.experience.notify("accessToken=secret123 email test@example.com", "error")
        self.experience.notify("accessToken=secret123 email test@example.com", "error")
        items = self.experience.handle("experienceNotifications", {})["items"]
        self.assertEqual(len(items), 1)
        self.assertNotIn("secret123", items[0]["message"])
        self.assertNotIn("test@example.com", items[0]["message"])

    def test_report_explains_oom_missing_dependency_and_redacts(self):
        (self.folder / "logs").mkdir()
        (self.folder / "logs/latest.log").write_text('java.lang.OutOfMemoryError: Java heap space\nAuthorization: Bearer abcSECRET\nC:\\Users\\PrivateName\\app\n192.168.1.5:25565\n',encoding="utf-8")
        with zipfile.ZipFile(self.folder / "mods/example.jar", "w") as jar:
            jar.writestr("fabric.mod.json", json.dumps({"id":"example","version":"1.0","depends":{"missinglib":"*"}}))
        data = analyze(self.store, self.pid, 1)
        self.assertIn("memory", {x["id"] for x in data["findings"]})
        self.assertIn("missing-missinglib", {x["id"] for x in data["findings"]})
        report = report_text(self.store, data)
        for private in ("abcSECRET", "PrivateName", "192.168.1.5"):
            self.assertNotIn(private, report)

    def test_external_log_symlink_is_not_read(self):
        outside = self.root / "private.txt"
        outside.write_text("OutOfMemoryError secret")
        (self.folder / "logs").mkdir()
        (self.folder / "logs/latest.log").symlink_to(outside)
        self.assertTrue(analyze(self.store, self.pid)["empty"])

    def test_stderr_capture_drains_and_keeps_a_bounded_tail(self):
        data = b"a" * (MAX_LOG + 1024) + b"OutOfMemoryError"
        thread = capture_stderr(io.BytesIO(data), self.folder)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual((self.folder / "logs/launcher-stderr.log").read_bytes(), data[-MAX_LOG:])

    def test_stderr_capture_never_follows_external_log_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.folder / "logs").symlink_to(outside, target_is_directory=True)
        thread = capture_stderr(io.BytesIO(b"private"), self.folder)
        thread.join(timeout=3)
        self.assertEqual(list(outside.iterdir()), [])

    def test_report_redaction_handles_command_line_json_and_urls(self):
        sample = '"refresh_token": "PRIVATE", --accessToken TOKEN --username Player Authorization: Bearer AUTH https://example.com?token=TOKEN test@example.com 76561191234567890'
        clean = redact(sample)
        for private in ("PRIVATE", "TOKEN", "Player", "AUTH", "example.com", "76561191234567890"):
            self.assertNotIn(private, clean)
        for text in ('{"Authorization": "Bearer opaque-secret"}', "{'Authorization': 'Basic opaque-secret'}", r'{\"Authorization\": \"Bearer opaque-secret\"}'):
            self.assertNotIn("opaque-secret", redact(text))

    def test_performance_preview_is_stale_safe_and_restore_reverses_it(self):
        care = BuildCare(self.store)
        presets = PerformanceProfiles(self.store, care.before_change)
        options = self.folder / "options.txt"
        original = b"renderDistance:24\nsimulationDistance:12\nlang:ru_ru\n"
        options.write_bytes(original)
        plan = presets.preview(self.pid,"economy")
        options.write_bytes(original + b"otherSetting:true\n")
        with self.assertRaises(ValueError):
            presets.apply(plan, lambda _:None)
        options.write_bytes(original)
        plan = presets.preview(self.pid,"economy")
        presets.apply(plan, lambda _:None)
        self.assertIn(b"lang:ru_ru", options.read_bytes())
        self.assertIn(b"renderDistance:6", options.read_bytes())
        self.assertEqual(self.store.get("performancePreset:"+self.pid), "economy")
        snapshot = care.state(self.pid)["snapshots"][0]["id"]
        care.restore(self.pid, snapshot, lambda _:None)
        self.assertEqual(options.read_bytes(), original)
        self.assertEqual(self.store.get("performancePreset:"+self.pid), "")

    def test_preferences_are_validated_and_old_versions_have_no_preset(self):
        with self.assertRaises(ValueError):
            self.experience.handle("experiencePreferences", {"scale":500,"reducedMotion":False})
        self.experience.handle("experiencePreferences", {"scale":110,"reducedMotion":True})
        self.assertEqual(self.experience.preferences(), {"scale":110,"reducedMotion":True,"checkUpdatesOnStart":True})
        old = self.store.create("Old", "1.7.10", "forge")
        with self.assertRaises(ValueError):
            PerformanceProfiles(self.store).preview(old,"economy")


if __name__ == "__main__":
    unittest.main()

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
from launcher import build_errors
from launcher.diagnostics import MAX_LOG, analyze, capture_stderr, read_log_chunk, redact, report_text
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

    def test_favorite_persists_without_changing_other_build_metadata(self):
        self.experience.handle("experienceSaveBuild", {"id":self.pid,"name":"Любимая сборка","description":"Описание","group":"Друзья","favorite":False,"color":"purple"})
        cover = self.experience.set_cover(self.pid, self.png((500, 300)))["cover"]
        cover_bytes = self.experience.media(cover).read_bytes()
        other = self.store.create("Другая сборка", "1.21.1", "vanilla")
        self.experience.handle("experienceSaveBuild", {"id":other,"favorite":True})
        before, profiles, settings = self.experience.metadata(), self.store.all(), self.store.settings()

        for _ in range(2):  # Repeating a delivered request must not toggle it back.
            result = self.experience.handle("experienceFavorite", {"id":self.pid,"favorite":True})
            self.assertEqual(result, {"ok":True,"id":self.pid,"favorite":True,"message":"Сборка добавлена в избранное."})
        expected = {**before, self.pid: {**before[self.pid], "favorite":1}}
        self.assertEqual(self.experience.metadata(), expected)
        self.assertEqual(self.store.all(), profiles)
        self.assertEqual(self.store.settings(), settings)
        self.assertEqual(self.experience.media(cover).read_bytes(), cover_bytes)

        reopened = Experience(Store(self.root))
        self.assertEqual(reopened.state()["details"], expected)
        result = reopened.handle("experienceFavorite", {"id":self.pid,"favorite":False})
        self.assertEqual(result, {"ok":True,"id":self.pid,"favorite":False,"message":"Сборка убрана из избранного."})
        self.assertEqual(Experience(Store(self.root)).metadata(), before)

    def test_unfavorite_new_build_is_idempotent_and_keeps_defaults(self):
        self.assertNotIn(self.pid, self.experience.metadata())
        for _ in range(2):
            self.experience.handle("experienceFavorite", {"id":self.pid,"favorite":False})
        self.assertEqual(self.experience.metadata()[self.pid], {
            "profile":self.pid,"description":"","group_name":"","favorite":0,"color":"green","cover":"",
        })
        self.assertEqual(self.store.profile(self.pid)["name"], "Мир")

    def test_favorite_rejects_non_boolean_values_without_writing_metadata(self):
        for value in (None, 0, 1, "true", "false", "", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.experience.handle("experienceFavorite", {"id":self.pid,"favorite":value})
                self.assertEqual(self.experience.metadata(), {})
        with self.assertRaises(ValueError):
            self.experience.handle("experienceFavorite", {"id":self.pid})
        self.assertEqual(self.experience.metadata(), {})

    def test_favorite_rejects_missing_invalid_and_unknown_build_ids(self):
        self.experience.handle("experienceFavorite", {"id":self.pid,"favorite":True})
        before = self.experience.metadata()
        for value in (None, 1, False, [], {}, "", "../" + self.pid, "x" * 32, "0" * 32):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.experience.handle("experienceFavorite", {"id":value,"favorite":False})
                self.assertEqual(self.experience.metadata(), before)
        with self.assertRaises(ValueError):
            self.experience.handle("experienceFavorite", {"favorite":False})
        self.assertEqual(self.experience.metadata(), before)

    def test_favorite_rejects_deleted_build_and_preserves_its_metadata(self):
        self.experience.handle("experienceFavorite", {"id":self.pid,"favorite":True})
        before = self.experience.metadata()
        self.store.move_profile(self.pid)
        with self.assertRaisesRegex(ValueError, "Сборка не найдена"):
            self.experience.handle("experienceFavorite", {"id":self.pid,"favorite":False})
        self.assertEqual(self.experience.metadata(), before)
        self.assertEqual(self.store.all(), [])
        self.assertEqual(self.store.deleted()[0]["id"], self.pid)

    def test_favorite_action_during_game_or_download_never_touches_game_files(self):
        from main import App

        app = App(self.root)
        self.addCleanup(app.close)
        app.job["busy"] = True
        with (
            patch.object(app, "profile_running", return_value=True),
            patch.object(app, "require_profile_idle", side_effect=AssertionError("favorite does not need an idle game")),
            patch.object(app.coordinator, "operation", side_effect=AssertionError("favorite does not need a game-files lock")),
            patch.object(app.store, "folder", side_effect=AssertionError("favorite must not access game files")),
            patch.object(app, "start_job", side_effect=AssertionError("favorite must not install anything")),
        ):
            result = app.action("experienceFavorite", {"id":self.pid,"favorite":True})
            self.assertIs(result["favorite"], True)
            with self.assertRaisesRegex(ValueError, "требует запроса на изменение"):
                app.feature_action("experienceFavorite", {"id":self.pid,"favorite":False}, read=True)
        self.assertEqual(app.experience.metadata()[self.pid]["favorite"], 1)
        app.job["busy"] = False
        profile = next(p for p in app.state()["profiles"] if p["id"] == self.pid)
        self.assertEqual(profile["favorite"], 1)
        with patch.object(app.updates, "pending", {"stage":"preparing"}):
            with self.assertRaisesRegex(ValueError, "Подготавливается обновление"):
                app.action("experienceFavorite", {"id":self.pid,"favorite":False})
        self.assertEqual(app.experience.metadata()[self.pid]["favorite"], 1)

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

    def test_windows_logs_remain_readable_in_report_and_stored_failure(self):
        log = self.folder / "logs/latest.log"
        log.parent.mkdir()
        expected = "OutOfMemoryError: Не удалось загрузить библиотеку для игры. Ёжик"
        for encoding in ("utf-8-sig", "utf-16", "cp1251", "cp866"):
            with self.subTest(encoding=encoding):
                log.write_bytes(expected.encode(encoding))
                result = analyze(self.store, self.pid)
                self.assertIn(expected, report_text(self.store, result))
                build_errors.record_exit(self.store, self.pid, 1, log.stat().st_mtime)
                event = build_errors.history(self.store, self.pid)["groups"][0]["events"][0]
                self.assertIn(expected, event["evidence"])

    def test_mixed_utf8_and_windows_lines_and_bounded_unicode_reads(self):
        first = "Строка игры"
        second = "Ошибка инициализации сборки"
        for encoding in ("cp1251", "cp866"):
            raw = (first + "\n").encode() + second.encode(encoding)
            self.assertEqual(read_log_chunk(io.BytesIO(raw), MAX_LOG), first + "\n" + second)
        self.assertEqual(read_log_chunk(io.BytesIO("ЯЁж".encode()), 5), "Ёж")
        self.assertEqual(read_log_chunk(io.BytesIO("ЯЁж".encode()), 3, tail=False), "Я")
        self.assertEqual(read_log_chunk(io.BytesIO("ЯЁж".encode("utf-16")), 5), "Ёж")

    def test_report_distinguishes_unrecoverable_input_from_normal_punctuation(self):
        log = self.folder / "logs/latest.log"
        log.parent.mkdir()
        log.write_text("Почему? Это вопрос!", encoding="utf-8")
        self.assertFalse(analyze(self.store, self.pid)["warnings"])
        log.write_text("OutOfMemoryError: ?????? ??????", encoding="ascii")
        result = analyze(self.store, self.pid)
        self.assertIn("?????? ??????", result["logs"][0]["excerpt"])
        self.assertIn("в исходном журнале", result["warnings"][0])

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

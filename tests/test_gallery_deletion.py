import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from launcher.experience import Experience, SCREENSHOT_TRASH
from launcher.store import Store


class GalleryDeletionTests(unittest.TestCase):
    def test_windows_path_and_descriptor_stamp_compare_birthtime_consistently(self):
        common = {"st_dev": 1, "st_ino": 2, "st_size": 4, "st_mtime_ns": 30, "st_birthtime_ns": 10}
        by_path = SimpleNamespace(**common, st_ctime_ns=10)
        by_handle = SimpleNamespace(**common, st_ctime_ns=20)
        with patch("launcher.experience.os.name", "nt"):
            self.assertEqual(Experience._stamp(by_path), Experience._stamp(by_handle))
            by_handle.st_ino = 3
            self.assertNotEqual(Experience._stamp(by_path), Experience._stamp(by_handle))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.pid = self.store.create("Скриншоты", "1.20.1", "fabric")
        self.folder = self.store.folder(self.pid) / "screenshots"
        self.folder.mkdir()
        self.experience = Experience(self.store)

    @staticmethod
    def png(color=(30, 100, 60)):
        out = io.BytesIO()
        Image.new("RGB", (100, 80), color).save(out, "PNG")
        return out.getvalue()

    def screenshot(self, name, color=(30, 100, 60)):
        path = self.folder / name
        path.write_bytes(self.png(color))
        return path

    def selection(self, *names):
        data = self.experience.gallery(self.pid)
        return [{"name": image["name"], "token": image["token"]} for image in data["images"] if not names or image["name"] in names]

    def trash(self, batch):
        return self.folder / SCREENSHOT_TRASH / batch

    def test_multiple_screenshots_move_and_survive_service_restart_then_restore(self):
        originals = {name: self.screenshot(name, color).read_bytes() for name, color in [("one.png", (10, 20, 30)), ("two.png", (40, 50, 60)), ("keep.png", (70, 80, 90))]}
        selected = self.selection("one.png", "two.png")
        result = self.experience.handle("experienceDeleteScreenshots", {"id": self.pid, "images": selected})
        self.assertEqual(result["deleted"], 2)
        self.assertFalse((self.folder / "one.png").exists())
        self.assertEqual((self.folder / "keep.png").read_bytes(), originals["keep.png"])
        other = Experience(self.store)
        data = other.gallery(self.pid)
        self.assertEqual([image["name"] for image in data["images"]], ["keep.png"])
        self.assertEqual(data["trash"][0]["count"], 2)
        restored = other.handle("experienceRestoreScreenshots", {"id": self.pid, "batch": result["batch"]})
        self.assertEqual(restored["restored"], 2)
        for name, content in originals.items():
            self.assertEqual((self.folder / name).read_bytes(), content)
        self.assertEqual(other.gallery(self.pid)["trash"], [])

    def test_stale_selection_rejects_the_whole_batch_before_any_file_moves(self):
        first = self.screenshot("one.png")
        second = self.screenshot("two.png")
        selected = self.selection()
        second.write_bytes(self.png((180, 20, 30)))
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.experience.delete_screenshots(self.pid, selected)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse((self.folder / SCREENSHOT_TRASH).exists())

    def test_content_checksum_is_checked_even_when_stat_fingerprint_is_forced_same(self):
        shot = self.screenshot("one.png")
        selected = self.selection()
        original_key = self.experience._shot_key(self.pid, shot, shot.stat())
        shot.write_bytes(self.png((200, 20, 40)))
        with patch.object(self.experience, "_shot_key", return_value=original_key):
            with self.assertRaisesRegex(ValueError, "изменился"):
                self.experience.delete_screenshots(self.pid, selected)
        self.assertTrue(shot.exists())

    def test_token_cannot_be_reused_for_another_profile(self):
        self.screenshot("one.png")
        selected = self.selection()
        other_id = self.store.create("Другой", "1.20.1", "fabric")
        other = self.store.folder(other_id) / "screenshots"
        other.mkdir()
        (other / "one.png").write_bytes(self.png())
        with self.assertRaises(ValueError):
            self.experience.delete_screenshots(other_id, selected)
        self.assertTrue((other / "one.png").exists())
        self.assertTrue((self.folder / "one.png").exists())

    def test_traversal_links_and_duplicate_selection_are_rejected(self):
        shot = self.screenshot("one.png")
        selected = self.selection()
        for name in ("../one.png", "sub/one.png", "sub\\one.png", "/tmp/one.png", "one.png:stream"):
            with self.assertRaises(ValueError):
                self.experience.delete_screenshots(self.pid, [{"name": name, "token": selected[0]["token"]}])
        with self.assertRaises(ValueError):
            self.experience.delete_screenshots(self.pid, selected * 2)
        (self.folder / "linked.png").symlink_to(shot)
        with self.assertRaises(ValueError):
            self.experience.delete_screenshots(self.pid, [{"name": "linked.png", "token": selected[0]["token"]}])
        self.assertTrue(shot.exists())

    def test_redirected_screenshots_directory_and_profile_are_rejected(self):
        self.screenshot("one.png")
        selected = self.selection()
        old = self.folder.with_name("old-screenshots")
        self.folder.rename(old)
        self.folder.symlink_to(old, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.experience.delete_screenshots(self.pid, selected)
        self.assertTrue((old / "one.png").exists())
        self.folder.unlink()
        old.rename(self.folder)
        profile = self.folder.parent
        moved = profile.with_name("old-profile")
        profile.rename(moved)
        profile.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.experience.delete_screenshots(self.pid, selected)
        self.assertTrue((moved / "screenshots/one.png").exists())

    def test_restore_name_collision_preserves_new_file_and_entire_batch(self):
        self.screenshot("one.png")
        self.screenshot("two.png")
        result = self.experience.delete_screenshots(self.pid, self.selection())
        replacement = self.screenshot("one.png", (240, 20, 80)).read_bytes()
        with self.assertRaisesRegex(ValueError, "уже есть"):
            self.experience.restore_screenshots(self.pid, result["batch"])
        self.assertEqual((self.folder / "one.png").read_bytes(), replacement)
        self.assertFalse((self.folder / "two.png").exists())
        self.assertTrue((self.trash(result["batch"]) / "two.png").exists())

    def test_restore_rejects_changed_trash_bytes_and_tampered_manifest(self):
        self.screenshot("one.png")
        result = self.experience.delete_screenshots(self.pid, self.selection())
        directory = self.trash(result["batch"])
        (directory / "one.png").write_bytes(self.png((240, 50, 10)))
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.experience.restore_screenshots(self.pid, result["batch"])
        self.assertFalse((self.folder / "one.png").exists())
        record = json.loads((directory / "manifest.json").read_text())
        record["images"][0]["name"] = "../../outside.png"
        (directory / "manifest.json").write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            self.experience.restore_screenshots(self.pid, result["batch"])

    def test_partial_move_failure_remains_visible_and_restorable(self):
        self.screenshot("one.png")
        self.screenshot("two.png")
        selected = sorted(self.selection(), key=lambda row: row["name"])
        rename = os.rename
        def interrupted(source, target):
            if Path(source).name == "two.png":
                raise PermissionError("busy")
            return rename(source, target)
        with patch("launcher.experience.os.rename", side_effect=interrupted):
            result = self.experience.delete_screenshots(self.pid, selected)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["skipped"], ["two.png"])
        self.assertEqual(self.experience.gallery(self.pid)["trash"][0]["count"], 1)
        self.experience.restore_screenshots(self.pid, result["batch"])
        self.assertTrue((self.folder / "one.png").exists())
        self.assertTrue((self.folder / "two.png").exists())

    def test_restore_write_failure_keeps_trash_and_removes_partial_new_file(self):
        content = self.screenshot("one.png").read_bytes()
        result = self.experience.delete_screenshots(self.pid, self.selection())
        with patch("launcher.experience.os.fsync", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.experience.restore_screenshots(self.pid, result["batch"])
        self.assertFalse((self.folder / "one.png").exists())
        self.assertEqual((self.trash(result["batch"]) / "one.png").read_bytes(), content)
        self.experience.restore_screenshots(self.pid, result["batch"])
        self.assertEqual((self.folder / "one.png").read_bytes(), content)

    def test_deleted_item_cache_is_removed_without_evicting_other_thumbnail(self):
        self.screenshot("one.png", (20, 30, 40))
        self.screenshot("two.png", (60, 70, 80))
        selected = self.selection("one.png")
        self.experience.delete_screenshots(self.pid, selected)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM gallery_cache").fetchone()[0], 1)
        with patch.object(self.experience, "_image", side_effect=AssertionError("unchanged thumbnail should be cached")):
            self.assertEqual(len(self.experience.gallery(self.pid)["images"]), 1)

    def test_unknown_batches_and_oversized_selections_do_not_mutate_files(self):
        shot = self.screenshot("one.png")
        selection = self.selection()
        for requested in ([], selection * 121, "one.png"):
            with self.assertRaises(ValueError):
                self.experience.delete_screenshots(self.pid, requested)
        for batch in ("../screenshots", "", None, "0" * 32):
            with self.assertRaises(ValueError):
                self.experience.restore_screenshots(self.pid, batch)
        self.assertTrue(shot.exists())


if __name__ == "__main__":
    unittest.main()

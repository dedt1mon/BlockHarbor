import ctypes
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from launcher import game, memory, skin_sync
from main import App


class PerformanceTests(unittest.TestCase):
    def test_windows_reads_physical_and_available_ram(self):
        def fill(pointer):
            status = ctypes.cast(pointer, ctypes.POINTER(memory.MemoryStatus)).contents
            self.assertEqual(status.length, 64)
            status.total, status.available = 16 << 30, 9 << 30
            return 1

        kernel = SimpleNamespace(GlobalMemoryStatusEx=Mock(side_effect=fill))
        with (
            patch.object(memory, "sys", SimpleNamespace(platform="win32")),
            patch.object(
                ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True
            ),
        ):
            self.assertEqual(memory.physical_memory(), (16384, 9216))
            kernel.GlobalMemoryStatusEx.side_effect = lambda _: 0
            self.assertIsNone(memory.physical_memory())
            self.assertEqual(memory.automatic_heap(), 2048)

    def test_auto_heap_scales_down_under_pressure_and_caps_large_systems(self):
        for available, expected in (
            ((4096, 1800), 1024),
            ((8192, 3700), 2048),
            ((8192, 7200), 4096),
            ((32768, 21000), 6144),
        ):
            with (
                self.subTest(available=available),
                patch.object(memory, "physical_memory", return_value=available),
            ):
                self.assertEqual(memory.automatic_heap(), expected)

    def test_auto_and_existing_manual_setting_reach_game_launch(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("main.automatic_heap", return_value=3072),
        ):
            app = App(Path(tmp))
            pid = app.store.create("Vanilla", "1.20.1", "vanilla")
            child = Mock()
            child.poll.return_value = 0
            for configured, expected in ((None, 3072), ("5120", 5120), ("auto", 3072)):
                with self.subTest(configured=configured):
                    if configured is not None:
                        app.action("settings", {"mode": "local", "memory": configured})
                    # Re-opening an existing installation must preserve manual limits.
                    reopened = App(Path(tmp))
                    snapshot = reopened.state()
                    self.assertEqual(
                        snapshot["autoMemory"], configured in (None, "auto")
                    )
                    self.assertEqual(snapshot["memory"], expected)
                    # Exercise the Play action; run its job synchronously in this test.
                    with (
                        patch.object(
                            reopened,
                            "start_job",
                            side_effect=lambda fn, **kwargs: fn(lambda _: None),
                        ),
                        patch.object(game, "install"),
                        patch.object(
                            game, "launch_local", return_value=child
                        ) as launch,
                    ):
                        reopened.action("play", {"id": pid})
                    self.assertEqual(launch.call_args.kwargs["memory_mb"], expected)
                    reopened.close()

    def test_state_reads_db_independently_of_mod_count_and_sees_external_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp))
            pid = app.store.create("Fabric", "1.20.1", "fabric")
            folder = app.store.folder(pid)
            skin_sync.prepare(folder, "1.20.1", "fabric")
            with patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
                base = app.state()
                initial_queries = connect.call_count
                for n in range(60):
                    (folder / "mods" / f"local-{n}.jar").write_bytes(b"my file")
                connect.reset_mock()
                current = app.state()
            self.assertEqual(initial_queries, connect.call_count)
            items = current["contents"]["mods"]
            self.assertEqual(len(items), len(base["contents"]["mods"]) + 60)
            self.assertEqual(
                [i["name"] for i in items if i["managed"]],
                [base["contents"]["mods"][0]["name"]],
            )
            file = folder / "mods" / "local-2.jar"
            file.write_bytes(b"updated")
            file.rename(folder / "mods" / "user-file.jar")
            changed = app.state()["contents"]["mods"]
            self.assertFalse(any(row["name"] == file.name for row in changed))
            self.assertTrue(
                any(
                    row["name"] == "user-file.jar" and row["size"] == 7
                    for row in changed
                )
            )


class DistanceSettingsTests(unittest.TestCase):
    def setUp(self):
        from launcher import distances

        self.distances = distances
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.file = self.root / "options.txt"

    def test_reduction_changes_only_high_distances_and_restore_keeps_later_options(
        self,
    ):
        original = b"\xef\xbb\xbfversion:3465\r\nrenderDistance:24\r\nsimulationDistance:4\r\nkey_key.attack:key.mouse.left\r\n"
        self.file.write_bytes(original)
        plan = self.distances.preview(self.root, "1.20.1")
        self.assertEqual(plan["changes"], {"renderDistance": 8})
        self.distances.apply(self.root, "1.20.1", plan)
        reduced = self.file.read_bytes()
        self.assertEqual(
            reduced, original.replace(b"renderDistance:24", b"renderDistance:8")
        )
        self.file.write_bytes(reduced + b"lastServer:example.org\r\n")
        restore = self.distances.preview(self.root, "1.20.1")
        self.assertEqual(restore["changes"], {"renderDistance": 24})
        self.distances.apply(self.root, "1.20.1", restore)
        self.assertEqual(
            self.file.read_bytes(), original + b"lastServer:example.org\r\n"
        )
        self.assertFalse((self.root / ".blockharbor-distances.json").exists())

    def test_preview_detects_edit_and_preserves_modified_file(self):
        self.file.write_bytes(b"renderDistance:18\nsimulationDistance:12\n")
        plan = self.distances.preview(self.root, "26.1")
        self.assertEqual(
            plan["changes"], {"renderDistance": 8, "simulationDistance": 5}
        )
        self.file.write_bytes(b"renderDistance:2\nsimulationDistance:2\n")
        with self.assertRaisesRegex(ValueError, "Параметры изменились"):
            self.distances.apply(self.root, "26.1", plan)
        self.assertEqual(
            self.file.read_bytes(), b"renderDistance:2\nsimulationDistance:2\n"
        )
        self.assertEqual(self.distances.preview(self.root, "1.18")["changes"], {})

    def test_failure_after_creating_backup_leaves_no_false_restore_state(self):
        self.file.write_text("renderDistance:16\nsimulationDistance:8\n")
        plan = self.distances.preview(self.root, "1.20.1")
        original_replace = self.distances._replace

        def fail_options(path, data):
            if path.name == "options.txt":
                raise PermissionError("options.txt is locked")
            original_replace(path, data)

        with patch.object(self.distances, "_replace", side_effect=fail_options):
            with self.assertRaises(PermissionError):
                self.distances.apply(self.root, "1.20.1", plan)
        self.assertFalse((self.root / ".blockharbor-distances.json").exists())
        self.assertEqual(self.distances.preview(self.root, "1.20.1"), plan)

        original_read = self.distances._read
        calls = 0

        def concurrent_editor(folder, name, limit):
            nonlocal calls
            if name == "options.txt":
                calls += 1
                if calls == 2:
                    self.file.write_text("renderDistance:4\nsimulationDistance:4\n")
            return original_read(folder, name, limit)

        with patch.object(self.distances, "_read", side_effect=concurrent_editor):
            with self.assertRaisesRegex(ValueError, "Параметры игры изменились"):
                self.distances.apply(self.root, "1.20.1", plan)
        self.assertEqual(
            self.file.read_text(), "renderDistance:4\nsimulationDistance:4\n"
        )
        self.assertFalse((self.root / ".blockharbor-distances.json").exists())
        self.assertEqual(self.distances.preview(self.root, "1.20.1")["mode"], "reduce")

    def test_missing_legacy_and_external_files_are_never_modified(self):
        self.assertEqual(
            self.distances.preview(self.root, "1.18")["mode"], "unavailable"
        )
        self.assertFalse(self.file.exists())
        self.assertFalse(self.distances.supported("1.17.1"))
        self.assertFalse(self.distances.supported("1.18-pre1"))
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "options.txt"
            external.write_text("renderDistance:24")
            self.file.symlink_to(external)
            with self.assertRaises(ValueError):
                self.distances.preview(self.root, "1.20.1")
            self.assertEqual(external.read_text(), "renderDistance:24")

    def test_playing_profile_cannot_apply_and_all_edits_use_normal_action(self):
        app = App(self.root)
        pid = app.store.create("Distance test", "1.20.1", "vanilla")
        options = app.store.folder(pid) / "options.txt"
        options.write_text("renderDistance:12\nsimulationDistance:8\n")
        plan = app.action("gameDistances", {"id": pid})
        with (
            patch.object(app, "profile_running", return_value=True),
            self.assertRaisesRegex(ValueError, "Закрой Minecraft"),
        ):
            app.action("gameDistances", {"id": pid, "expected": plan})
        self.assertIn("renderDistance:12", options.read_text())
        result = app.action("gameDistances", {"id": pid, "expected": plan})
        self.assertEqual(result["mode"], "restore")
        self.assertIn("renderDistance:8", options.read_text())

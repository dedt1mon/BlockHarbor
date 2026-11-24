"""Real staged JAR transactions with independent failure/review/cancel handling."""

import copy
from contextlib import closing
import json
import os
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import catalog_batch, lifecycle, modrinth
from launcher.build_care import BuildCare
from launcher.store import Store
import test_modrinth as fixtures


class CatalogBatchTests(unittest.TestCase):
    setUp = fixtures.ModrinthTests.setUp
    add = fixtures.ModrinthTests.add
    get = fixtures.ModrinthTests.get

    def items(self, *projects):
        return [{"kind": "mods", "project": project} for project in projects]

    def run_batch(self, items, **options):
        return catalog_batch.install(self.store, self.pid, items, lambda _message: None, **options)

    def test_independent_download_failure_does_not_publish_its_dependencies_or_stop_others(self):
        self.root["files"][0]["hashes"]["sha512"] = "0" * 128
        self.add("independent", "other1", "other.jar")
        updates = []
        result = self.run_batch(self.items("root", "independent"), on_progress=updates.append)
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["installed"], 1)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["summary"]["remaining"], 0)
        self.assertEqual({path.name for path in self.mods.iterdir()}, {"other.jar"})
        root = next(row for row in result["items"] if row["project"] == "root")
        self.assertIn("Контрольная", root["error"])
        self.assertEqual(updates[0]["items"][0]["status"], "queued")
        self.assertEqual(updates[-1]["summary"]["progress"], 100)
        seen = {row["status"] for snapshot in updates for row in snapshot["items"]}
        self.assertTrue({"queued", "checking", "dependencies", "downloading", "installing", "installed", "error"} <= seen)

    def test_shared_dependency_keeps_roots_in_one_atomic_group(self):
        self.add("another", "next1", "next.jar", deps=[("required", "dep", "dep1")])
        self.root["files"][0]["hashes"]["sha512"] = "0" * 128
        result = self.run_batch(self.items("root", "another"))
        self.assertEqual(result["summary"]["failed"], 2)
        self.assertEqual(list(self.mods.iterdir()), [])

    def test_conflicting_shared_pins_do_not_hide_an_independent_feasible_root(self):
        self.root["dependencies"][0]["version_id"] = "dep1"
        self.add("dep", "dep2", "dep2.jar")
        self.add("other", "other1", "other.jar", deps=[("required", "dep", "dep2")])
        self.add("good", "good1", "good.jar")
        items = self.items("root", "other", "good")
        preview = catalog_batch.check_selection(self.store, self.pid, items)
        self.assertFalse(preview["blocked"])
        self.assertTrue(preview["partial"])
        self.assertEqual([row["project"] for row in preview["items"]], ["good"])
        self.assertEqual(len(preview["itemErrors"]), 2)
        self.request.assert_not_called()
        result = self.run_batch(items)
        self.assertEqual(result["summary"]["incompatible"], 2)
        self.assertEqual(result["summary"]["installed"], 1)
        self.assertEqual({path.name for path in self.mods.iterdir()}, {"good.jar"})

    def test_no_compatible_version_is_individual_and_a_fully_invalid_basket_is_blocked(self):
        self.root["loaders"] = ["forge"]
        self.add("good", "good1", "good.jar")
        result = self.run_batch(self.items("root", "good"))
        self.assertEqual(result["summary"]["incompatible"], 1)
        self.assertEqual(result["summary"]["installed"], 1)
        preview = catalog_batch.check_selection(self.store, self.pid, self.items("root"))
        self.assertTrue(preview["blocked"])
        self.assertFalse(preview["items"])

    def test_retry_recognizes_verified_renamed_installed_artifacts(self):
        first = self.run_batch(self.items("root"))
        self.assertTrue(first["ok"])
        (self.mods / "root.jar").rename(self.mods / "my-renamed-mod.jar")
        self.request.reset_mock()
        result = self.run_batch(self.items("root"))
        self.assertEqual(result["summary"]["alreadyInstalled"], 1)
        self.assertEqual(result["summary"]["installed"], 0)
        self.request.assert_not_called()

    def test_explicit_review_is_required_and_never_approved_by_a_token_for_other_roots(self):
        self.root["dependencies"].append({"dependency_type": "incompatible", "project_id": "dep"})
        self.add("good", "good1", "good.jar")
        first = self.run_batch(self.items("root", "good"), acknowledgement="unrelated-token")
        self.assertEqual(first["summary"]["needsReview"], 1)
        self.assertEqual(first["summary"]["installed"], 1)
        self.assertEqual({path.name for path in self.mods.iterdir()}, {"good.jar"})
        review = first["reviews"][0]
        # Unrelated successful publication changes the inventory, invalidating
        # the earlier token. The next response must request fresh consent.
        retry = self.run_batch(review["request"]["items"], acknowledgement=review["token"])
        self.assertEqual(retry["summary"]["needsReview"], 1)
        newest = retry["reviews"][0]
        accepted = self.run_batch(newest["request"]["items"], acknowledgement=newest["token"])
        self.assertTrue(accepted["ok"])
        self.assertEqual({path.name for path in self.mods.iterdir()}, {"good.jar", "root.jar", "dep.jar"})

    def test_cancellation_keeps_completed_groups_and_cancels_only_unpublished_items(self):
        self.add("good", "good1", "good.jar")
        scope = lifecycle.CancellationScope()
        updates = []

        def progress(snapshot):
            updates.append(snapshot)
            if snapshot["summary"]["installed"]:
                scope.cancel()

        with lifecycle.bind(scope), self.assertRaises(lifecycle.OperationCancelled):
            self.run_batch(self.items("good", "root"), on_progress=progress)
        self.assertEqual({path.name for path in self.mods.iterdir()}, {"good.jar"})
        self.assertEqual(updates[-1]["summary"]["installed"], 1)
        self.assertEqual(updates[-1]["summary"]["cancelled"], 1)
        self.assertEqual(updates[-1]["summary"]["remaining"], 0)

    def test_callback_snapshots_cannot_change_internal_rows(self):
        def mutate(snapshot):
            snapshot["items"].clear()
            snapshot["summary"]["selected"] = 900
        result = self.run_batch(self.items("root"), on_progress=mutate)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["summary"]["selected"], 1)

    def test_durable_publisher_preserves_existing_files_and_records_one_backup(self):
        self.add("good", "good1", "good.jar")
        care = BuildCare(self.store)
        backups = []
        result = self.run_batch(self.items("root", "good"), publisher=care.publish_addons,
                                before_publish=lambda: backups.append("snapshot"))
        self.assertTrue(result["ok"])
        self.assertEqual(backups, ["snapshot"])
        self.assertFalse(list(care.root.glob("transaction-*")))
        self.assertFalse(list(care.root.glob("addon-stage-*")))

    def test_optional_post_install_metadata_failure_cannot_report_verified_files_as_failed(self):
        def fail_metadata(_releases):
            raise sqlite3.OperationalError("metadata database unavailable")
        result = self.run_batch(self.items("root"), after_publish=fail_metadata)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["installed"], 1)
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertTrue(result["warnings"])
        self.assertEqual({path.name for path in self.mods.iterdir()}, {"root.jar", "dep.jar"})

    def test_selected_root_installed_as_dependency_after_temporary_metadata_error_is_counted(self):
        self.add("parent", "parent1", "parent.jar", deps=[("required", "root", "root1")])
        original = modrinth._plan_items
        failed = False

        def plan(store, profile, items):
            nonlocal failed
            if len(items) == 1 and items[0]["project"] == "root" and not failed:
                failed = True
                raise modrinth.CatalogError("temporary metadata failure")
            return original(store, profile, items)

        with patch.object(modrinth, "_plan_items", side_effect=plan):
            result = self.run_batch(self.items("root", "parent"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["installed"], 2)
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertEqual({path.name for path in self.mods.iterdir()}, {"root.jar", "dep.jar", "parent.jar"})

    def test_dependency_does_not_satisfy_a_different_explicitly_selected_version(self):
        self.add("parent", "parent1", "parent.jar", deps=[("required", "root", "root1")])
        items = [{"kind": "mods", "project": "root", "version": "missingversion"}, *self.items("parent")]
        result = self.run_batch(items)
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["installed"], 1)
        self.assertEqual(result["summary"]["notInstalled"], 1)
        self.assertEqual(result["items"][0]["version"], "missingversion")


class CatalogCrashRecoveryTests(unittest.TestCase):
    def test_process_exit_between_file_publications_rolls_back_only_its_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Данные с пробелами"
            store = Store(root)
            pid = store.create("Проверка", "1.20.1", "fabric")
            keep = store.content_folder(pid, "mods") / "existing.jar"
            keep.write_bytes(b"unrelated user content")
            # Exercise a real abrupt exit, not an exception caught by rollback.
            script = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from launcher.store import Store
from launcher.build_care import BuildCare
from launcher import build_care
root=Path(sys.argv[1]); pid=sys.argv[2]; store=Store(root); care=BuildCare(store)
staging=root/'staged'; staging.mkdir()
pending=[]
for name in ('one.jar','two.jar'):
    source=staging/name; source.write_bytes(name.encode())
    pending.append((SimpleNamespace(kind='mods',filename=name), source, store.content_folder(pid,'mods')/name))
original=build_care.publish_new
def die(source,target):
    original(source,target)
    os._exit(87)
build_care.publish_new=die
care.publish_addons(pid,pending)
'''
            completed = subprocess.run([sys.executable, "-c", script, str(root), pid], cwd=Path(__file__).parents[1], timeout=15)
            self.assertEqual(completed.returncode, 87)
            self.assertTrue((keep.parent / "one.jar").exists())
            care = BuildCare(store)
            self.assertFalse(care.recovery_errors)
            self.assertEqual([p.name for p in keep.parent.iterdir()], ["existing.jar"])
            self.assertEqual(keep.read_bytes(), b"unrelated user content")
            self.assertFalse(list(care.root.glob("transaction-*")))


class CatalogJobTests(unittest.TestCase):
    def test_cancel_while_waiting_for_operation_lock_finishes_every_queued_row(self):
        from main import App
        with tempfile.TemporaryDirectory() as temporary, closing(App(Path(temporary))) as app:
            calls = []
            initial = catalog_batch.initial_snapshot("queued-profile", [
                {"kind": "mods", "project": "one"}, {"kind": "mods", "project": "two"}])
            with app.coordinator.operation():
                jid = app.start_job(lambda status: calls.append(True), job_data=initial, cancellable=True)["job"]
                app.action("cancelCatalogInstall", {"job": jid})
                deadline = time.monotonic() + 3
                while app.job_snapshot()["busy"] and time.monotonic() < deadline:
                    time.sleep(.01)
                state = app.job_snapshot()
                self.assertFalse(state["busy"])
                self.assertEqual(state["summary"]["cancelled"], 2)
                self.assertEqual(state["summary"]["remaining"], 0)
                self.assertEqual([row["status"] for row in state["items"]], ["cancelled", "cancelled"])
                self.assertFalse(state["cancellable"])
                self.assertEqual(calls, [])
                self.assertFalse(app.scope.event.is_set())

    def test_user_cancel_releases_just_the_job_and_read_notifications_stay_responsive(self):
        from main import App
        with tempfile.TemporaryDirectory() as temporary, closing(App(Path(temporary))) as app:
            started = threading.Event()

            def task(status):
                started.set()
                lifecycle.wait(20)

            job = app.start_job(task, job_data={"kind": "catalog-install"}, cancellable=True)["job"]
            self.assertTrue(started.wait(2))
            app.experience.notify("Новое событие")
            self.assertEqual(app.feature_action("experienceReadNotifications", {"throughId": 1})["unread"], 0)
            with self.assertRaises(ValueError):
                app.action("cancelCatalogInstall", {"job": "different-job"})
            app.action("cancelCatalogInstall", {"job": job})
            for _ in range(200):
                state = app.job_snapshot()
                if not state["busy"]:
                    break
                time.sleep(.01)
            self.assertFalse(state["busy"])
            self.assertTrue(state["cancelled"])
            self.assertFalse(app.closing.is_set())
            self.assertIsNone(app.catalog_scope)
            self.assertIsInstance(app.state(), dict)


if __name__ == "__main__":
    unittest.main()

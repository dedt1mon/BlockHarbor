"""A project facet match alone is not a compatible version of a mod."""
import threading
import time
import unittest
import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from http.client import IncompleteRead
from unittest.mock import patch

from launcher import catalog_search, lifecycle, modrinth


class VerifiedSearchTests(unittest.TestCase):
    def setUp(self):
        catalog_search._confirmed.cache_clear()
        self.addCleanup(catalog_search._confirmed.cache_clear)
        self.profile = {"version": "1.20.1", "loader": "fabric"}
        self.page = {"hits": [{"id": key, "title": key, "source": "community"} for key in ("wrong", "good", "good")],
                     "offset": 0, "limit": 12, "total": 30}
        self.enterContext(patch.object(modrinth, "search", return_value=self.page))

    def test_exact_release_tuple_removes_false_facet_match_and_deduplicates(self):
        calls = []
        def releases(profile, kind, project, source):
            calls.append((profile, kind, project, source))
            return [] if project == "wrong" else [SimpleNamespace(version="alpha", channel="alpha"), SimpleNamespace(version="stable", channel="release")]
        with patch.object(modrinth, "_releases", side_effect=releases):
            result = catalog_search.search(self.profile, "mods", "", 0)
            again = catalog_search.search(self.profile, "mods", "", 0)
        self.assertEqual([hit["id"] for hit in result["hits"]], ["good"])
        self.assertEqual(result["hits"][0]["recommendedVersion"], "stable")
        self.assertEqual(result["hits"][0]["compatibility"], "release-metadata")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0] == self.profile for call in calls))
        self.assertEqual(result, again)
        self.assertTrue(result["totalIsEstimate"])
        self.assertEqual(result["total"], 30)

    def test_network_failure_never_presents_unchecked_hit_as_compatible(self):
        def releases(profile, kind, project, source):
            if project == "wrong":
                raise modrinth.CatalogError("temporary offline")
            return [SimpleNamespace(version="stable", channel="release")]
        with patch.object(modrinth, "_releases", side_effect=releases):
            result = catalog_search.search(self.profile, "mods", "", 0)
        self.assertEqual([hit["id"] for hit in result["hits"]], ["good"])
        self.assertTrue(result["partial"])
        self.assertIn("не удалось проверить", result["message"])

    def test_installed_cards_match_bytes_including_renamed_and_disabled_addons(self):
        payload = b"verified addon contents"
        release = SimpleNamespace(version="stable", channel="release", alternatives=(),
                                  size=len(payload), sha512=hashlib.sha512(payload).hexdigest())
        with tempfile.TemporaryDirectory() as directory, patch.object(modrinth, "_releases", return_value=[release]):
            service = catalog_search.SearchService()
            self.addCleanup(service.close)
            for kind, extension in (("mods", ".jar"), ("shaderpacks", ".zip"), ("resourcepacks", ".zip")):
                with self.subTest(kind=kind):
                    folder = Path(directory) / kind
                    folder.mkdir()
                    file = folder / ("renamed" + extension + ".disabled")
                    file.write_bytes(payload)
                    files = modrinth._files(folder, kind, include_disabled=True)
                    result = service.search(self.profile, kind, "", 0, installed_files=files)
                    self.assertTrue(all(hit["installed"] and hit["installedVersion"] == "stable" for hit in result["hits"]))
                    file.write_bytes(b"x" * len(payload))
                    changed = service.search(self.profile, kind, "", 0, installed_files=files)
                    self.assertFalse(any(hit["installed"] for hit in changed["hits"]))

    def test_total_deadline_cancels_pending_checks_without_cancelling_launcher(self):
        owner = lifecycle.CancellationScope()
        stopped = threading.Event()
        def releases(*args, **kwargs):
            try:
                lifecycle.wait(10)
            finally:
                stopped.set()
        start = time.monotonic()
        with lifecycle.bind(owner), patch.object(catalog_search, "VERIFY_BUDGET", .1), patch.object(modrinth, "_releases", side_effect=releases):
            result = catalog_search.search(self.profile, "mods", "", 0)
            self.assertTrue(stopped.wait(1))
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(owner.event.is_set())
        self.assertFalse(result["hits"])
        self.assertTrue(result["partial"])

    def test_interrupted_metadata_body_keeps_other_confirmed_hits(self):
        def releases(profile, kind, project, source):
            if project == "wrong":
                raise IncompleteRead(b"partial", 99)
            return [SimpleNamespace(version="stable", channel="release")]
        with patch.object(modrinth, "_releases", side_effect=releases):
            result = catalog_search.search(self.profile, "mods", "", 0)
        self.assertEqual([hit["id"] for hit in result["hits"]], ["good"])
        self.assertTrue(result["partial"])

    def test_parent_close_propagates_instead_of_returning_a_successful_empty_page(self):
        owner = lifecycle.CancellationScope()
        def releases(*args, **kwargs):
            owner.cancel()
            lifecycle.check()
        with lifecycle.bind(owner), patch.object(modrinth, "_releases", side_effect=releases):
            with self.assertRaises(lifecycle.OperationCancelled):
                catalog_search.search(self.profile, "mods", "", 0)

    def test_one_window_bounds_network_workers_during_superseded_queries(self):
        service = catalog_search.SearchService()
        gate, entered = threading.Event(), threading.Event()
        lock = threading.Lock()
        active, maximum = [0], [0]
        errors, results = [], []

        def slow_project_search(*args):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
                entered.set()
            try:
                gate.wait(3)  # Simulate an OS DNS call that cannot be aborted.
                lifecycle.check()
                return {"hits": [], "offset": 0, "limit": 12, "total": 0}
            finally:
                with lock:
                    active[0] -= 1

        def request(number):
            try:
                results.append(service.search(self.profile, "mods", str(number), 0))
            except (lifecycle.OperationCancelled, modrinth.CatalogError) as exc:
                errors.append(exc)

        with patch.object(modrinth, "search", side_effect=slow_project_search):
            threads = []
            try:
                for number in range(24):
                    thread = threading.Thread(target=request, args=(number,), daemon=True)
                    threads.append(thread)
                    thread.start()
                    time.sleep(.005)
                self.assertTrue(entered.wait(1))
                self.assertLessEqual(maximum[0], 4)
                self.assertLessEqual(service.pool._queue.qsize(), catalog_search.MAX_SEARCH_TASKS)
            finally:
                gate.set()
                for thread in threads:
                    thread.join(3)
                service.close()
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(errors) + len(results), 24)
        self.assertTrue(errors)

    def test_initial_project_search_shares_the_same_total_deadline(self):
        def slow(*args):
            lifecycle.wait(3)
        start = time.monotonic()
        with patch.object(modrinth, "search", side_effect=slow), patch.object(catalog_search, "VERIFY_BUDGET", .1):
            with self.assertRaisesRegex(modrinth.CatalogError, "отведённое время"):
                catalog_search.search(self.profile, "mods", "", 0)
        self.assertLess(time.monotonic() - start, 1)


if __name__ == "__main__":
    unittest.main()

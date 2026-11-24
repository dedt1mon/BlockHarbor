"""A project facet match alone is not a compatible version of a mod."""
import threading
import time
import unittest
from types import SimpleNamespace
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

    def test_parent_close_propagates_instead_of_returning_a_successful_empty_page(self):
        owner = lifecycle.CancellationScope()
        def releases(*args, **kwargs):
            owner.cancel()
            lifecycle.check()
        with lifecycle.bind(owner), patch.object(modrinth, "_releases", side_effect=releases):
            with self.assertRaises(lifecycle.OperationCancelled):
                catalog_search.search(self.profile, "mods", "", 0)


if __name__ == "__main__":
    unittest.main()

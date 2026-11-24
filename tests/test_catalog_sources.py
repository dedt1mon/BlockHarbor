"""Search stays fast without claiming unintegrated websites are installable."""

import hashlib
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from launcher import catalog_search, catalog_sources, modrinth


class CatalogSourcesTests(unittest.TestCase):
    profile = {"version": "1.20.1", "loader": "fabric"}

    def setUp(self):
        catalog_search._confirmed.cache_clear()
        self.addCleanup(catalog_search._confirmed.cache_clear)

    def test_result_metadata_avoids_project_fetches_but_still_checks_exact_releases(self):
        for kind, project_type, extension in (("mods", "mod", ".jar"),
                                               ("shaderpacks", "shader", ".zip"),
                                               ("resourcepacks", "resourcepack", ".zip")):
            with self.subTest(kind=kind):
                requests = []

                def get(path, **params):
                    requests.append(path)
                    if path == "/search":
                        return {"hits": [{"project_id": name, "project_type": project_type,
                                          "title": name} for name in ("Good", "Wrong", "BadHash")],
                                "total_hits": 3}
                    if not path.endswith("/version"):
                        raise AssertionError("Search fetched redundant project metadata: " + path)
                    project = path.split("/")[2]
                    row = {"id": project + "V", "project_id": project, "version_type": "release",
                           "version_number": "1", "name": "1", "date_published": "2026-01-01",
                           "game_versions": ["1.20.4" if project == "Wrong" else "1.20.1"],
                           "loaders": ["fabric"], "files": [{"filename": project + extension,
                           "url": "https://cdn.modrinth.com/data/" + project + "/file" + extension,
                           "size": 100, "hashes": {"sha512": "bad" if project == "BadHash" else hashlib.sha512(b"file").hexdigest()}}]}
                    return [row]

                with patch.object(modrinth, "_get", side_effect=get):
                    result = catalog_search.search(self.profile, kind, "", 0)
                self.assertEqual([h["id"] for h in result["hits"]], ["Good"])
                self.assertEqual(result["hits"][0]["sourceTitle"], "Modrinth")
                self.assertEqual(len(requests), 4)  # Search + one version request per project, not seven.
                self.assertTrue(result["verified"])

    def test_reused_project_metadata_cannot_turn_a_wrong_loader_release_into_a_match(self):
        def get(path, **params):
            if path == "/search":
                return {"hits": [{"project_id": "WrongLoader", "project_type": "mod", "title": "Wrong"}], "total_hits": 1}
            return [{"id": "WrongV", "project_id": "WrongLoader", "game_versions": ["1.20.1"], "loaders": ["forge"]}]
        with patch.object(modrinth, "_get", side_effect=get):
            result = catalog_search.search(self.profile, "mods", "", 0)
        self.assertEqual(result["hits"], [])

    def test_primary_outage_keeps_reviewed_github_results_and_external_links(self):
        with patch.object(modrinth, "_get", side_effect=modrinth.CatalogError("offline")) as get:
            result = catalog_search.search(self.profile, "shaderpacks", "bliss", 0, source="all")
        self.assertTrue(result["partial"])
        self.assertTrue(result["hits"])
        self.assertTrue(all(hit["source"] == "github" for hit in result["hits"]))
        self.assertEqual(get.call_count, 1)
        statuses = {item["id"]: item for item in result["sourceStatus"]}
        self.assertEqual(statuses["modrinth"]["status"], "unavailable")
        self.assertEqual(statuses["github"]["status"], "available")
        self.assertEqual(statuses["curseforge"]["mode"], "website")
        self.assertEqual(len(result["externalSources"]), 2)

    def test_external_links_keep_category_and_literal_query_without_network(self):
        query = "a & search=other # /?привет"
        with patch.object(modrinth, "_open", side_effect=AssertionError("No network for website links")):
            for kind, cf, pmc in (("mods", "mc-mods", "/mods/"),
                                   ("shaderpacks", "shaders", "/texture-packs/tag/shaders/"),
                                   ("resourcepacks", "texture-packs", "/texture-packs/")):
                with self.subTest(kind=kind):
                    result = catalog_sources.describe(self.profile, kind, query)
                    curseforge, planet = [urlsplit(row["url"]) for row in result["externalSources"]]
                    self.assertEqual((curseforge.scheme, curseforge.netloc), ("https", "www.curseforge.com"))
                    self.assertEqual(parse_qs(curseforge.query)["class"], [cf])
                    self.assertEqual(parse_qs(curseforge.query)["search"], [query])
                    self.assertEqual(parse_qs(curseforge.query)["version"], [self.profile["version"]])
                    self.assertEqual((planet.scheme, planet.netloc, planet.path), ("https", "www.planetminecraft.com", pmc))
                    self.assertEqual(parse_qs(planet.query)["keywords"], [query])
                    self.assertFalse(curseforge.fragment or planet.fragment)
        with self.assertRaises(ValueError):
            catalog_sources.describe(self.profile, "https://untrusted.example/", query)


if __name__ == "__main__":
    unittest.main()

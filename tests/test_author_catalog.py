import json
import tempfile
import zipfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import compatibility, modrinth, trusted_catalog
from launcher.modfiles import file_stamp, mod_info


class AuthorCatalogTests(unittest.TestCase):
    fabric = {"version": "1.20.1", "loader": "fabric"}
    legacy = {"version": "1.7.10", "loader": "forge"}

    def test_publisher_search_and_version_compatibility_are_available_offline(self):
        for profile, kind, term, filters in (
            (self.fabric, "mods", "lithium", {"category": ["optimization"]}),
            (self.fabric, "resourcepacks", "faithful", {"resolution": ["32x"]}),
            (self.fabric, "shaderpacks", "bliss", {"renderer": ["iris"]}),
            (self.legacy, "mods", "mouse", {"environment": ["client"]}),
        ):
            with self.subTest(kind=kind, term=term), patch.object(modrinth, "_open", side_effect=AssertionError):
                result = modrinth.search(profile, kind, term, 0, filters, source="github")
                self.assertEqual(result["total"], 1)
                project = result["hits"][0]["id"]
                releases = modrinth._releases(profile, kind, project, source="github")
                self.assertTrue(releases)
                for release in releases:
                    meta = modrinth._get("/version/" + release.version)
                    self.assertIn(profile["version"], meta["game_versions"])
                    self.assertEqual(modrinth._get("/project/" + project)["id"], project)
                    self.assertEqual(trusted_catalog.by_hash(release.sha512), meta)
                    self.assertEqual(trusted_catalog.canonical_version(release.version),
                                     meta["community_version"] or release.version)
        self.assertEqual(modrinth.search(self.fabric, "mods", "GTNH", 0, source="github")["total"], 0)

    def test_legacy_forge_descriptors_in_both_formats(self):
        required = [{"modid": "Mouse", "name": "Mouse", "version": "2",
                     "useDependencyInformation": True,
                     "requiredMods": ["Library@[1,2)", "Other"], "dependencies": ["OrderOnly"]}]
        with tempfile.TemporaryDirectory() as temp:
            for index, shape in enumerate((required, {"modListVersion": 2, "modList": required})):
                path = Path(temp) / f"legacy-{index}.jar"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("mcmod.info", json.dumps(shape))
                self.assertEqual(mod_info(path, file_stamp(path)), ({"mouse"}, "2"))
                report = compatibility.local_report(self.legacy, {}, {path.name: path})
                self.assertEqual([(row["id"], row["version"]) for row in report["required"]],
                                 [("library", "[1,2)"), ("other", "*")])
                self.assertFalse(report["warnings"])

    def test_only_exact_reviewed_assets_use_the_github_transfer(self):
        [release] = modrinth._releases(self.legacy, "mods", "BHghPMouseTweaksGTNH", "github")
        transfer = Mock()
        modrinth._download(release, Path("staged.jar"), transfer)
        kwargs = transfer.download.call_args.kwargs
        self.assertEqual(kwargs["hashes"], {"sha512": release.sha512})
        self.assertEqual(kwargs["hosts"], trusted_catalog.RELEASE_HOSTS)
        for replacement in ({"version": "unreviewed"}, {"project": "other"}, {"size": 12},
                            {"url": release.url.replace("GTNewHorizons/", "unrelated/")},
                            {"sha512": "a" * 128}):
            with self.subTest(replacement=replacement), self.assertRaises(modrinth.CatalogError):
                modrinth._download(replace(release, **replacement), Path("staged.jar"), transfer)
        transfer.download.assert_called_once()

    def test_quick_add_keeps_author_source_when_another_mod_pins_the_same_binary(self):
        project = "P7dR8mSH"  # Fabric API
        profile = {**self.fabric, "id": "test"}
        [api] = modrinth._releases(profile, "mods", project, "github")
        pin = trusted_catalog.canonical_version(api.version)
        self.assertNotEqual(pin, api.version)
        dependency = {"project_id": project, "version_id": pin, "dependency_type": "required"}
        other = replace(api, project="other", version="otherV", deps=(dependency,))
        items = [{"project": project, "version": None, "kind": "mods", "source": "github"},
                 {"project": other.project, "version": other.version, "kind": "mods", "source": "community"}]

        def available(_profile, _kind, identifier, source="all"):
            if identifier == project:
                self.assertEqual(source, "github")
                return [api]
            return [other]

        with patch.object(modrinth, "_get", side_effect=AssertionError("unexpected primary lookup")), \
                patch.object(modrinth, "_releases", side_effect=available), \
                patch.object(modrinth, "_inventory", return_value={}):
            plan = modrinth._plan_items(None, profile, items)
            self.assertEqual([release.version for release in plan], [api.version, other.version])
            self.assertTrue(plan[0].url.startswith("https://github.com/FabricMC/"))

    def test_mixed_results_keep_total_and_order_across_page_boundaries(self):
        remote = [
            {"project_id": f"community{i}", "title": f"Remote {i}", "downloads": 2000 - 5 * i}
            for i in range(40)
        ]
        publishers = modrinth.search(self.fabric, "mods", "", 0, source="github")["hits"]
        # A matching title alone must not hide a different author's project.
        remote[0]["title"] = publishers[0]["title"]
        # A shared project has a second listing on the primary service.
        duplicate = {"project_id": publishers[0]["id"], "title": "Cross-listed", "downloads": 3000}

        def search(_path, **params):
            excluded = {facet[0][12:] for facet in json.loads(params["facets"]) if facet[0].startswith("project_id!=")}
            hits = [h for h in [duplicate, *remote] if h["project_id"] not in excluded]
            return {"total_hits": len(hits), "hits": hits[params["offset"]:params["offset"] + params["limit"]]}

        with patch.object(modrinth, "_get", side_effect=search):
            expected = {h["id"]: h["downloads"] for h in publishers}
            expected.update({h["project_id"]: h["downloads"] for h in remote})
            all_hits = []
            for offset in range(0, len(expected), modrinth._PAGE):
                result = modrinth.search(self.fabric, "mods", "", offset, sort="downloads", source="all")
                self.assertEqual(result["total"], len(expected))
                all_hits.extend(h["id"] for h in result["hits"])
            self.assertEqual(all_hits, sorted(expected, key=expected.get, reverse=True))

    def test_secondary_source_remains_usable_when_community_is_unavailable(self):
        with patch.object(modrinth, "_get", side_effect=modrinth.CatalogError("offline")):
            result = modrinth.search(self.fabric, "shaderpacks", "bliss", 0, source="all")
            self.assertTrue(result["partial"])
            self.assertTrue(result["message"])
            project = result["hits"][0]["id"]
            self.assertTrue(modrinth._releases(self.fabric, "shaderpacks", project))
            with self.assertRaises(modrinth.CatalogError):
                modrinth.search(self.fabric, "shaderpacks", "bliss", 0, source="community")

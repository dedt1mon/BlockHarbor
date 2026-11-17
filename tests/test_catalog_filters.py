import json
import unittest
from unittest.mock import patch

from launcher import catalog_filters, modrinth


class CatalogFiltersTest(unittest.TestCase):
    profile = {"version": "1.20.1", "loader": "fabric"}

    def test_mods_combine_categories_license_environment_and_exclusions(self):
        filters = {
            "category": ["magic", "!library"],
            "environment": ["client", "server"],
            "license": ["open_source"],
            "exclude": ["telemetry", "plugin"],
        }
        with patch.object(
            modrinth, "_get", return_value={"hits": [], "total_hits": 0}
        ) as api:
            modrinth.search(self.profile, "mods", "chest", 12, filters, "updated")
        params = api.call_args.kwargs
        self.assertEqual(params["index"], "updated")
        self.assertEqual(params["offset"], 12)
        facets = json.loads(params["facets"])
        for value in (
            "project_type:mod",
            "versions:1.20.1",
            "categories:fabric",
            "categories:magic",
            "categories!=library",
            "open_source:true",
            "disclosure_types!=telemetry",
            "all_project_types!=plugin",
        ):
            self.assertIn([value], facets)
        environments = next(
            group for group in facets if group[0].startswith("environment:")
        )
        self.assertIn("environment:client_and_server", environments)
        self.assertNotIn("environment:client_only", environments)

    def test_website_groups_and_negative_options(self):
        shader = catalog_filters.facets(
            "shaderpacks",
            {
                "renderer": ["iris", "canvas"],
                "feature": ["reflections", "shadows"],
                "performance": ["!high"],
            },
            "downloads",
        )
        self.assertIn(["categories:iris", "categories:canvas"], shader)
        for facet in (
            "categories:reflections",
            "categories:shadows",
            "categories!=high",
        ):
            self.assertIn([facet], shader)
        pack = catalog_filters.facets(
            "resourcepacks",
            {"resolution": ["8x-", "512x+", "!32x"], "license": ["!open_source"]},
            "follows",
        )
        self.assertIn(["categories:8x-", "categories:512x+"], pack)
        self.assertIn(["categories!=32x"], pack)
        self.assertIn(["open_source!=true"], pack)

    def test_dependencies_positive_is_any_negative_is_none(self):
        filters = {
            "dependencies": [
                {"id": "Fabric", "types": ["required", "embedded"]},
                {"id": "Sodium", "types": ["required", "optional"], "exclude": True},
            ]
        }
        self.assertEqual(
            catalog_filters.facets("mods", filters, "relevance"),
            [
                [
                    "embedded_dependency_project_ids:Fabric",
                    "required_dependency_project_ids:Fabric",
                ],
                ["optional_dependency_project_ids!=Sodium"],
                ["required_dependency_project_ids!=Sodium"],
            ],
        )

    def test_invalid_options_never_reach_modrinth(self):
        invalid = [
            ("mods", {"category": ["realistic"]}, "downloads"),
            ("mods", {"category": ["library", "!library"]}, "downloads"),
            ("mods", {"category": ["foo OR magic"]}, "relevance"),
            ("resourcepacks", {"environment": ["client"]}, "relevance"),
            ("shaderpacks", {"exclude": ["telemetry"]}, "relevance"),
            (
                "resourcepacks",
                {"dependencies": [{"id": "A", "types": ["other"]}]},
                "relevance",
            ),
            (
                "mods",
                {
                    "dependencies": [
                        {"id": "A", "types": ["required"], "exclude": "yes"}
                    ]
                },
                "relevance",
            ),
            ("mods", {}, "custom"),
        ]
        with patch.object(modrinth, "_get") as api:
            for kind, filters, sort in invalid:
                with (
                    self.subTest(kind=kind, filters=filters, sort=sort),
                    self.assertRaises(ValueError),
                ):
                    modrinth.search(self.profile, kind, "", 0, filters, sort)
            api.assert_not_called()

    def test_every_visible_option_works_under_its_own_category(self):
        for kind in ("mods", "shaderpacks", "resourcepacks"):
            schema = catalog_filters.schema(kind)
            for group in schema["groups"]:
                for opt in group["options"]:
                    with self.subTest(kind=kind, option=opt["id"]):
                        self.assertTrue(
                            catalog_filters.facets(
                                kind, {group["id"]: [opt["id"]]}, "newest"
                            )
                        )
                        if group["mode"] == "both":
                            self.assertTrue(
                                catalog_filters.facets(
                                    kind, {group["id"]: ["!" + opt["id"]]}, "newest"
                                )
                            )
            self.assertEqual(
                set(schema["sort"]),
                {"relevance", "downloads", "follows", "newest", "updated"},
            )

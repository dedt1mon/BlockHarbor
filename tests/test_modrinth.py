import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import modrinth
from launcher.store import Store


def jar(mod, version):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": mod, "version": version}))
    return out.getvalue()


class ModrinthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.pid = self.store.create("Мой мир", "1.20.1", "fabric")
        self.mods = self.store.content_folder(self.pid, "mods")
        self.data = {}
        self.projects = {}
        self.byid = {}
        self.api = self.enterContext(
            patch.object(modrinth, "_get", side_effect=self.get)
        )
        self.request = self.enterContext(
            patch.object(
                modrinth,
                "_open",
                side_effect=lambda url, hosts: io.BytesIO(self.data[url]),
            )
        )
        self.recognized = self.enterContext(
            patch.object(modrinth, "_versions_by_hash", return_value={})
        )
        self.root = self.add(
            "root", "root1", "root.jar", deps=[("required", "dep", None)]
        )
        self.dep = self.add("dep", "dep1", "dep.jar")

    def add(
        self, project, identifier, filename, *, deps=(), loader="fabric", game="1.20.1"
    ):
        url = (
            "https://cdn.modrinth.com/data/"
            + project
            + "/versions/"
            + identifier
            + "/"
            + filename
        )
        content = jar(project, identifier)
        self.data[url] = content
        row = {
            "id": identifier,
            "project_id": project,
            "version_type": "release",
            "version_number": identifier,
            "name": identifier,
            "date_published": "2026-01-01",
            "loaders": [loader],
            "game_versions": [game],
            "dependencies": [
                {"dependency_type": t, "project_id": p, "version_id": v}
                for t, p, v in deps
            ],
            "files": [
                {
                    "filename": filename,
                    "url": url,
                    "size": len(content),
                    "hashes": {"sha512": hashlib.sha512(content).hexdigest()},
                    "primary": True,
                }
            ],
        }
        self.projects.setdefault(project, []).append(row)
        self.byid[identifier] = row
        return row

    def get(self, path, **params):
        if path == "/search":
            return {"hits": [{"project_id": "root", "title": "Мод"}], "total_hits": 67}
        _, section, identifier, *tail = path.split("/")
        if section == "version":
            return self.byid[identifier]
        if tail:
            return self.projects[identifier]
        return {"id": identifier, "project_type": "mod"}

    def install(self, acknowledgement=""):
        return modrinth.install(
            self.store,
            self.pid,
            "mods",
            "root",
            "root1",
            lambda _: None,
            acknowledgement=acknowledgement,
        )

    def test_search_and_versions_filter_game_loader_type_and_page(self):
        self.add("root", "forge", "other.jar", loader="forge")
        self.add("root", "old", "old.jar", game="1.18.2")
        profile = self.store.profile(self.pid)
        result = modrinth.search(profile, "mods", "список", 12)
        self.assertEqual(result["offset"], 12)
        self.assertEqual(result["total"], 67)
        request = self.api.call_args.kwargs
        self.assertEqual(request["query"], "список")
        self.assertEqual(
            json.loads(request["facets"]),
            [["project_type:mod"], ["versions:1.20.1"], ["categories:fabric"]],
        )
        self.assertEqual(
            [
                v["id"]
                for v in modrinth.versions(self.store, self.pid, "mods", "root")[
                    "versions"
                ]
            ],
            ["root1"],
        )
        profile["loader"] = "vanilla"
        with self.assertRaisesRegex(ValueError, "загрузчиком"):
            modrinth.search(profile, "mods", "", 0)
        modrinth.search(profile, "shaderpacks", "", 0)
        self.assertEqual(
            json.loads(self.api.call_args.kwargs["facets"]),
            [["project_type:shader"], ["versions:1.20.1"]],
        )

    def test_search_exposes_only_the_projects_own_cdn_image(self):
        base = "https://cdn.modrinth.com/data/root/"
        valid = [base + name for name in ("icon.png", "397d2_96.webp", "397d2.svg")]
        invalid = [
            None,
            [],
            "http://cdn.modrinth.com/data/root/icon.png",
            "https://cdn.modrinth.com.example/data/root/icon.png",
            "https://cdn.modrinth.com@elsewhere.example/data/root/icon.png",
            "https://cdn.modrinth.com/data/other/icon.png",
            base + "../other/icon.png",
            base + "versions/abc/mod.jar",
            base + "asset.html",
            "data:image/png;base64,AAAA",
        ]
        inputs = valid + invalid
        self.api.side_effect = None
        self.api.return_value = {
            "hits": [
                {"project_id": "root", "title": "Мод", "icon_url": url}
                for url in inputs
            ],
            "total_hits": len(inputs),
        }
        result = modrinth.search(self.store.profile(self.pid), "mods", "", 0)
        self.assertEqual(
            [h["icon"] for h in result["hits"]], valid + [""] * len(invalid)
        )

    def test_transitive_cycle_ignores_optional_reuses_manual_file_and_retries(self):
        self.dep["dependencies"] = [
            {
                "dependency_type": "required",
                "project_id": "leaf",
                "version_id": "leaf1",
            },
            {"dependency_type": "optional", "project_id": "irrelevant"},
        ]
        leaf = self.add("leaf", "leaf1", "leaf.jar", deps=[("required", "root", None)])
        (self.mods / "renamed.jar").write_bytes(self.data[leaf["files"][0]["url"]])
        self.assertIn("2", self.install())
        self.assertTrue(
            modrinth.versions(self.store, self.pid, "mods", "root")["versions"][0][
                "installed"
            ]
        )
        self.assertEqual(
            {f.name for f in self.mods.glob("*.jar")},
            {"root.jar", "dep.jar", "renamed.jar"},
        )
        self.request.reset_mock()
        self.assertIn("уже", self.install())
        self.request.assert_not_called()

    def test_mismatched_hash_or_forged_type_publishes_nothing(self):
        self.root["files"][0]["hashes"]["sha512"] = "0" * 128
        with self.assertRaisesRegex(ValueError, "Контрольная"):
            self.install()
        self.assertEqual(list(self.mods.iterdir()), [])
        with self.assertRaisesRegex(ValueError, "категории"):
            modrinth.versions(self.store, self.pid, "resourcepacks", "root")

    def test_block_redirects_names_and_unrelated_version(self):
        with self.assertRaisesRegex(ValueError, "адрес"):
            modrinth._Redirect(modrinth._CDN_HOSTS).redirect_request(
                None, None, 302, "", {}, "http://127.0.0.1/file.jar"
            )
        with self.assertRaisesRegex(ValueError, "совместимого файла"):
            modrinth.install(
                self.store, self.pid, "mods", "root", "dep1", lambda _: None
            )
        self.root["files"][0]["filename"] = "../escape.jar"
        with self.assertRaisesRegex(ValueError, "имя файла"):
            self.install()
        self.request.assert_not_called()

    def test_existing_import_with_other_bytes_same_id_never_overwritten(self):
        (self.mods / "manual.jar").write_bytes(jar("dep", "else"))
        with self.assertRaisesRegex(ValueError, "manual.jar"):
            self.install()
        self.assertEqual(set(p.name for p in self.mods.iterdir()), {"manual.jar"})

    def test_failed_commit_rolls_back_only_new_files(self):
        real = modrinth.publish_new

        def publish(src, dest):
            if dest.name == "root.jar":
                dest.write_bytes(b"user file created concurrently")
            real(src, dest)

        with (
            patch.object(modrinth, "publish_new", side_effect=publish),
            self.assertRaisesRegex(ValueError, "файл занят"),
        ):
            self.install()
        self.assertEqual([p.name for p in self.mods.iterdir()], ["root.jar"])
        self.assertEqual(
            (self.mods / "root.jar").read_bytes(), b"user file created concurrently"
        )

    def test_declared_incompatible_pins_fail_before_download(self):
        self.root["dependencies"] += [
            {"dependency_type": "required", "project_id": "next", "version_id": None}
        ]
        self.dep["dependencies"] = [
            {"dependency_type": "required", "project_id": "lib", "version_id": "v1"}
        ]
        self.add("next", "next1", "next.jar", deps=[("required", "lib", "v2")])
        self.add("lib", "v1", "lib-1.jar")
        self.add("lib", "v2", "lib-2.jar")
        with self.assertRaisesRegex(ValueError, "разные версии"):
            self.install()
        self.request.assert_not_called()

    def local_release(self, project, version, local_name, *, deps=()):
        v = self.add(project, version, project + ".jar", deps=deps)
        file = v["files"][0]
        (self.mods / local_name).write_bytes(self.data[file["url"]])
        self.recognized.return_value[file["hashes"]["sha512"]] = v
        return v

    def check(self):
        return modrinth.check(self.store, self.pid, "mods", "root", "root1")

    def test_catalog_preview_separates_pinned_optional_embedded_and_required(self):
        self.local_release("extras", "extra1", "renamed-extra.jar")
        self.root["dependencies"] += [
            {
                "dependency_type": "optional",
                "project_id": "extras",
                "version_id": "extra1",
            },
            {"dependency_type": "embedded", "project_id": "inner"},
        ]
        info = self.check()
        self.assertFalse(info["blocked"])
        self.assertEqual(
            [(i["filename"], i["installed"]) for i in info["required"]],
            [("dep.jar", False)],
        )
        self.assertEqual(info["optional"][0]["filename"], "renamed-extra.jar")
        self.assertEqual(info["optional"][0]["version"], "extra1")
        self.assertTrue(info["optional"][0]["installed"])
        self.assertEqual(info["embedded"][0]["name"], "inner")
        self.assertFalse(self.request.called)

    def test_conflicts_check_both_directions_and_version_pin_without_downloading(self):
        self.local_release("other", "other1", "friend-manual.jar")
        self.root["dependencies"].append(
            {
                "dependency_type": "incompatible",
                "project_id": "other",
                "version_id": "other2",
            }
        )
        self.assertFalse(self.check()["blocked"])
        self.root["dependencies"][-1]["version_id"] = "other1"
        result = self.check()
        self.assertFalse(result["blocked"])
        self.assertIn("friend-manual.jar", result["warnings"][0])
        with self.assertRaises(modrinth.NeedsReview) as first:
            self.install()
        self.assertEqual(first.exception.review["token"], result["reviewToken"])
        self.root["dependencies"].pop()
        self.byid["other1"]["dependencies"] = [
            {
                "dependency_type": "incompatible",
                "project_id": None,
                "version_id": "dep1",
            }
        ]
        self.assertIn("dep.jar", self.check()["warnings"][0])
        with self.assertRaises(modrinth.NeedsReview) as second:
            self.install()
        self.assertNotEqual(
            first.exception.review["token"], second.exception.review["token"]
        )
        self.request.assert_not_called()
        self.assertEqual([f.name for f in self.mods.iterdir()], ["friend-manual.jar"])
        self.assertIn("2", self.install(second.exception.review["token"]))

    def test_conflict_inside_plan_is_refused_and_changed_inventory_is_rechecked(self):
        self.dep["dependencies"] = [
            {"dependency_type": "incompatible", "project_id": "root"}
        ]
        self.assertTrue(self.check()["reviewToken"])
        self.dep["dependencies"] = []

        def download(url, hosts):
            (self.mods / "changed.jar").write_bytes(jar("new", "1"))
            return io.BytesIO(self.data[url])

        self.request.side_effect = download
        with self.assertRaisesRegex(ValueError, "изменились"):
            self.install()
        self.assertEqual({f.name for f in self.mods.iterdir()}, {"changed.jar"})

    def test_declared_breaks_in_downloaded_jar_refused_before_any_publish(self):
        file = self.root["files"][0]
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as out:
            out.writestr(
                "fabric.mod.json",
                json.dumps({"id": "root", "version": "1", "breaks": {"other": "*"}}),
            )
        b = raw.getvalue()
        self.data[file["url"]] = b
        file.update(size=len(b), hashes={"sha512": hashlib.sha512(b).hexdigest()})
        (self.mods / "local.jar").write_bytes(jar("other", "custom"))
        with self.assertRaisesRegex(ValueError, "local.jar"):
            self.install()
        self.assertEqual({f.name for f in self.mods.iterdir()}, {"local.jar"})

    def test_staged_review_precedes_commit_and_stale_approval_cannot_publish(self):
        file = self.root["files"][0]
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as out:
            out.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "id": "root",
                        "version": "1",
                        "depends": {"missing": ">=1"},
                        "recommends": {"nice": "*"},
                        "suggests": {"extra": "*"},
                    }
                ),
            )
        b = raw.getvalue()
        self.data[file["url"]] = b
        file.update(size=len(b), hashes={"sha512": hashlib.sha512(b).hexdigest()})
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as out:
            out.writestr(
                "fabric.mod.json",
                json.dumps(
                    {"id": "earlier", "version": "2", "conflicts": {"root": "*"}}
                ),
            )
        (self.mods / "manual.jar").write_bytes(raw.getvalue())
        self.assertFalse(self.check()["reviewToken"])
        with self.assertRaises(modrinth.NeedsReview) as first:
            self.install()
        review = first.exception.review
        self.assertEqual(
            review["request"],
            {"id": self.pid, "kind": "mods", "project": "root", "version": "root1"},
        )
        self.assertEqual(len(review["warnings"]), 4)
        for word in ("missing", "nice", "extra", "manual.jar"):
            self.assertTrue(any(word in w for w in review["warnings"]))
        self.assertEqual({f.name for f in self.mods.iterdir()}, {"manual.jar"})
        (self.mods / "extra.jar").write_bytes(jar("extra", "1"))
        with self.assertRaises(modrinth.NeedsReview) as stale:
            self.install(review["token"])
        new = stale.exception.review
        self.assertNotEqual(new["token"], review["token"])
        self.assertEqual(len(new["warnings"]), 3)
        self.assertIn("2", self.install(new["token"]))

    def test_job_returns_review_request_and_resets_on_next_new_job(self):
        import time

        from main import App

        app = App(self.store.root)
        # The application now routes verified binary transfers through its queue.
        # Keep this orchestration test offline; transfer integrity has its own tests.
        self.enterContext(patch.object(app.transfers, "download", side_effect=lambda url, target, **_options: target.write_bytes(self.data[url])))
        self.root["dependencies"].append(
            {"dependency_type": "incompatible", "project_id": "dep"}
        )
        request = {
            "id": self.pid,
            "kind": "mods",
            "project": "root",
            "version": "root1",
        }

        def completed():
            for _ in range(400):
                job = app.job_snapshot()
                if not job["busy"]:
                    return job
                time.sleep(0.005)
            self.fail("job did not complete")

        app.action("installModrinth", request)
        job = completed()
        self.assertFalse(job["ok"])
        self.assertEqual(job["review"]["request"], request)
        app.action(
            "installModrinth", {**request, "acknowledgement": job["review"]["token"]}
        )
        second = completed()
        self.assertTrue(second["ok"], second["message"])
        self.assertNotIn("review", second)

    def test_author_named_external_dep_can_be_provided_manually(self):
        self.root["dependencies"].append(
            {"dependency_type": "required", "file_name": "vendor-lib.jar"}
        )
        self.assertTrue(self.check()["blocked"])
        (self.mods / "vendor-lib.jar").write_bytes(jar("vendorlib", "1"))
        info = self.check()
        self.assertFalse(info["blocked"])
        self.assertIn(
            {"name": "vendor-lib.jar", "filename": "vendor-lib.jar", "installed": True},
            info["required"],
        )
        self.assertIn("2", self.install())

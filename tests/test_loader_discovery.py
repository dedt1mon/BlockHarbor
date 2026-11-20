"""Regression cases for selection without network discovery and Forge artifact resolution."""

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import minecraft_launcher_lib as mc
from minecraft_launcher_lib.mod_loader import _forge

from launcher import game, legacy_forge, loader_catalog, preparation, prism


class LoaderDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = self.cache / "build"

    def test_optional_refresh_uses_a_single_short_connection_attempt(self):
        with (
            patch.object(preparation, "urlopen", side_effect=TimeoutError("offline")) as connect,
            patch.object(preparation.lifecycle, "wait", side_effect=AssertionError("retry")),
            self.assertRaises(preparation.DownloadError),
        ):
            loader_catalog._fetch_provider("forge")
        self.assertEqual(connect.call_count, 1)
        self.assertLessEqual(connect.call_args.kwargs["timeout"], 5)

    def test_stale_bundled_catalog_never_waits_for_a_provider(self):
        with (
            patch.object(loader_catalog, "refresh_catalog", side_effect=AssertionError("would block")),
            patch.object(loader_catalog.time, "time", return_value=5_000_000_000),
        ):
            def choice(version):
                rows = loader_catalog.availability(self.cache, version)["loaders"]
                return next(row for row in rows if row["id"] == "forge")
            self.assertTrue(choice("1.7.10-pre4")["supported"])
            self.assertTrue(choice("26.3")["stale"])
            for version in ("1.4.7", "1.2.5"):
                self.assertTrue(choice(version)["manual"])
                self.assertIn("ручной установки", choice(version)["message"])
            self.assertFalse(choice("1.0")["manual"])

    def test_recommended_forge_coordinates_preserve_all_branch_suffixes(self):
        cases = {
            "1.7.2": "10.12.2.1161-mc172",
            "1.7.10": "10.13.4.1614-1.7.10",
            "1.8.9": "11.15.1.2318-1.8.9",
            "1.9.4": "12.17.0.2317-1.9.4",
            "1.10": "12.18.0.2000-1.10.0",
            "1.21.1": "52.1.20",
            "26.3": "66.1.9",
        }
        with patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("network")):
            for version, build in cases.items():
                with self.subTest(version=version):
                    full = f"{version}-{build}"
                    self.assertEqual(loader_catalog.forge_coordinate(self.cache, version), full)
                    short = build.partition("-")[0]
                    self.assertEqual(loader_catalog.forge_coordinate(self.cache, version, short), full)
                    self.assertTrue(_forge.Forge().get_installer_url(version, build).endswith(f"/{full}/forge-{full}-installer.jar"))

    def test_auto_meta_profile_takes_one_request_then_none_for_a_second_build(self):
        for loader in ("fabric", "quilt"):
            with self.subTest(loader=loader):
                pin = loader_catalog.meta_pin(self.cache, "1.20.1", loader)
                installed = f"{loader}-loader-{pin}-1.20.1"
                profile = {"id": installed, "inheritsFrom": "1.20.1", "mainClass": "fixture.Main"}
                with patch.object(game, "_get_json", return_value=profile) as fetch:
                    for _ in range(2):
                        self.assertEqual(game._meta_profile(self.cache, "1.20.1", loader, None), (installed, pin))
                fetch.assert_called_once_with(f"{game.META[loader]}/versions/loader/1.20.1/{pin}/profile/json")

    def test_incompatible_default_meta_pin_falls_back_to_version_specific_pin(self):
        pin = "0.19.7"
        installed = f"fabric-loader-{pin}-1.20.1"
        profile = {"id": installed, "inheritsFrom": "1.20.1", "mainClass": "fixture.Main"}
        response = [
            preparation.DownloadError("not available for this pair", status_code=404),
            [{"loader": {"stable": True, "version": pin}}],
            profile,
        ]
        with patch.object(game, "_get_json", side_effect=response) as fetch:
            self.assertEqual(game._meta_profile(self.cache, "1.20.1", "fabric", None), (installed, pin))
            self.assertEqual(fetch.call_count, 3)

    def test_forge_old_installer_versioninfo_writes_real_manifest_and_jar(self):
        version = "1.7.10"
        build = "10.13.4.1614-1.7.10"
        coordinate = f"{version}-{build}"
        parent = game._manifest_path(self.cache, version)
        game._atomic_json(parent, {"javaVersion": {"component": "jre-legacy"}})
        parent.with_suffix(".jar").write_bytes(b"vanilla-client")
        fixture = self.cache / "vendor-installer.jar"
        with zipfile.ZipFile(fixture, "w") as archive:
            archive.writestr("install_profile.json", json.dumps({
                "install": {"minecraft": version},
                "versionInfo": {"id": "1.7.10-Forge10.13.4.1614-1.7.10", "inheritsFrom": version,
                                "mainClass": "net.minecraft.launchwrapper.Launch", "libraries": []},
            }))
            archive.writestr(f"forge-{coordinate}-universal.jar", b"legacy-forge")

        def download(url, path, callback):
            self.assertTrue(url.endswith(f"/{coordinate}/forge-{coordinate}-installer.jar"))
            shutil.copyfile(fixture, path)

        with (
            patch.object(_forge, "download_file", side_effect=download),
            patch.object(_forge, "install_minecraft_version"),
            patch.object(game, "_prepare_files"),
            patch.object(mc.runtime, "get_executable_path", return_value="java"),
            patch.object(_forge.Forge, "get_minecraft_versions", side_effect=AssertionError("redundant metadata")),
        ):
            installed = game._install(version, "forge", self.cache, self.instance, lambda _: None, official=True)
        manifest = game._manifest_path(self.cache, installed)
        doc = json.loads(manifest.read_bytes())
        self.assertEqual(doc["id"], installed)
        self.assertEqual(doc["inheritsFrom"], version)
        self.assertEqual(manifest.with_suffix(".jar").read_bytes(), b"vanilla-client")
        lib = self.cache / "libraries/net/minecraftforge/forge" / coordinate / f"forge-{coordinate}.jar"
        self.assertEqual(lib.read_bytes(), b"legacy-forge")

    def test_pre4_profile_uses_mojang_parent_and_published_universal_classifier(self):
        version = "1.7.10-pre4"
        parent = game._manifest_path(self.cache, version)
        game._atomic_json(parent, {"libraries": [], "minecraftArguments": "--username ${auth_player_name}",
                                   "javaVersion": {"component": "jre-legacy"}})
        with (
            patch.object(game, "_prepare_files"),
            patch.object(game, "_repair_loader_files") as repair,
            patch.object(mc.runtime, "get_executable_path", return_value="java"),
        ):
            installed = game._install(version, "forge", self.cache, self.instance, lambda _: None, official=True)
        doc = game._launch_metadata(self.cache, installed)
        self.assertEqual(doc["id"], installed)
        self.assertEqual(doc["jar"], version)
        self.assertEqual(doc["inheritsFrom"], version)
        self.assertIn("--tweakClass cpw.mods.fml.common.launcher.FMLTweaker", doc["minecraftArguments"])
        self.assertEqual(doc["libraries"][0]["sha1"], "1a4750617b3296ed636ddf0890a924b0de3a731b")
        self.assertEqual(doc["libraries"][0]["name"], "net.minecraftforge:forge:1.7.10_pre4-10.12.2.1149-prerelease:universal")
        repair.assert_called_once()

    def test_legacy_152_installs_embedded_forge_and_preserves_signed_vanilla(self):
        version, name = "1.5.2", "net.minecraftforge:minecraftforge:7.8.1.738"
        installed = "1.5.2-forge-7.8.1.738"
        base = game._manifest_path(self.cache, version)
        game._atomic_json(base, {"libraries": [{"name": "org.lwjgl.lwjgl:lwjgl-platform:2.9.0",
                                               "downloads": {"classifiers": {}}}]})
        with zipfile.ZipFile(base.with_suffix(".jar"), "w") as vanilla:
            vanilla.writestr("META-INF/MOJANG_C.SF", "signature")
            vanilla.writestr("example.class", b"game")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as core:
            core.writestr("Forge.class", b"core")
        def download(_, url, dest, sha1):
            self.assertTrue(url.endswith("forge-1.5.2-7.8.1.738-installer.jar"))
            self.assertEqual(sha1, "69cf98dbaa771545678fdb60b8a6a22019146dfb")
            with zipfile.ZipFile(dest, "w") as vendor:
                member = "minecraftforge-universal-1.5.2-7.8.1.738.jar"
                vendor.writestr(member, payload.getvalue())
                vendor.writestr("install_profile.json", json.dumps({
                    "install": {"minecraft": version, "path": name, "filePath": member, "stripMeta": True},
                    "versionInfo": {"mainClass": "net.minecraft.launchwrapper.Launch", "minecraftArguments": "args",
                                    "libraries": [{"name": name}, {"name": "org.lwjgl.lwjgl:lwjgl-platform:2.9.0"}]}
                }))
        with patch.object(preparation.Download, "__call__", download):
            profile = legacy_forge.profile(self.cache, installed, "1.5.2-7.8.1.738",
                                          json.loads(base.read_bytes()), lambda _: None)
        self.assertEqual(profile["jar"], installed + "-client")
        self.assertEqual([lib["name"] for lib in profile["libraries"]], [name])
        artifact = profile["libraries"][0]["downloads"]["artifact"]
        self.assertTrue(artifact["url"].endswith("-universal.zip"))
        self.assertEqual((self.cache / "libraries" / artifact["path"]).read_bytes(), payload.getvalue())
        with zipfile.ZipFile(base.with_suffix(".jar")) as vanilla:
            self.assertIn("META-INF/MOJANG_C.SF", vanilla.namelist())
        with zipfile.ZipFile(game._manifest_path(self.cache, profile["jar"]).with_suffix(".jar")) as child:
            self.assertEqual(child.namelist(), ["example.class"])

    def test_forge_161_repair_uses_published_universal_hash(self):
        version = "1.6.1"
        coordinate = "1.6.1-8.9.0.775"
        name = "net.minecraftforge:minecraftforge:8.9.0.775"
        embedded = io.BytesIO()
        with zipfile.ZipFile(embedded, "w") as jar:
            jar.writestr("Forge.class", b"recompressed upstream")
        def download(_, url, dest, sha1):
            with zipfile.ZipFile(dest, "w") as installer:
                installer.writestr("embedded.jar", embedded.getvalue())
                installer.writestr("install_profile.json", json.dumps({
                    "install": {"minecraft": version, "path": name, "filePath": "embedded.jar"},
                    "versionInfo": {"mainClass": "net.minecraft.launchwrapper.Launch",
                                    "minecraftArguments": "--tweakClass forge", "libraries": [{"name": name}]},
                }))
        with patch.object(preparation.Download, "__call__", download):
            profile = legacy_forge.profile(self.cache, "forge-test", coordinate, {"libraries": []}, lambda _: None)
        artifact = profile["libraries"][0]["downloads"]["artifact"]
        self.assertEqual(artifact["sha1"], "8f01b4612dc486524a2d4192a229a885b8bf76f7")
        self.assertEqual(artifact["size"], 1853061)
        self.assertTrue(artifact["url"].endswith("forge-1.6.1-8.9.0.775-universal.jar"))

    def test_prism_uses_numeric_build_for_a_branch_suffixed_direct_install(self):
        version = "1.7.10"
        repository = {"net.minecraft": [{"version": version}], "net.minecraftforge": [
            {"version": "10.13.4.1614", "requires": [{"uid": "net.minecraft", "equals": version}]}
        ]}
        game._atomic_json(self.instance / game.STAMP, {
            "version": version, "loader": "forge", "loader_version": "10.13.4.1614-1.7.10"
        })
        with patch.object(prism, "_rows", side_effect=repository.__getitem__):
            self.assertEqual(prism._components(self.instance, version, "forge")[-1]["version"], "10.13.4.1614")

    def test_calendar_neoforge_installs_without_running_old_major_version_detector(self):
        version = "26.3"
        provider = mc.mod_loader.get_mod_loader("neoforge")
        pin = loader_catalog.latest_neoforge(self.cache, version)
        installed = provider.get_installed_version(version, pin)
        game._atomic_json(game._manifest_path(self.cache, version), {"javaVersion": {"component": "java-runtime-delta"}})

        def install(mc_version, cache, *, callback, java, loader_version):
            self.assertEqual((mc_version, loader_version), (version, pin))
            game._atomic_json(game._manifest_path(Path(cache), installed), {"id": installed, "inheritsFrom": version})

        with (
            patch.object(mc.mod_loader, "get_mod_loader", return_value=provider),
            patch.object(mc.runtime, "get_executable_path", return_value="java"),
            patch.object(game, "_prepare_files"),
            patch.object(provider._base, "install", side_effect=install),
            patch.object(provider._base, "get_minecraft_versions", side_effect=AssertionError("obsolete detector")),
        ):
            self.assertEqual(game._install(version, "neoforge", self.cache, self.instance, lambda _: None, official=True), installed)

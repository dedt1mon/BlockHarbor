"""Loader availability must fail early without breaking cached installations."""

import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import RemoteDisconnected
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

import minecraft_launcher_lib as mc

from launcher import e4steam, game, lifecycle, loader_catalog, preparation
from launcher.store import input_profile
from main import App, Handler, Server


def json_response(raw=b'{"ok":true}'):
    reply = io.BytesIO(raw)
    reply.headers = {"Content-Length": str(len(raw))}
    reply.geturl = lambda: "https://metadata.example.org/catalog"
    return reply


def catalog_fixture():
    stamp = datetime.now(timezone.utc).isoformat()
    versions = {
        "fabric": ["1.20.1", "1.21.1", "26.3"],
        "quilt": ["1.20.1", "1.21.1"],
        "forge": ["1.7.10", "1.20.1", "1.21.1", "26.3"],
        "neoforge": ["1.21.1", "26.3"],
    }
    result = {
        "format": 1,
        "fetchedAt": stamp,
        "minecraft": ["1.0", "1.7.10", "1.20.1", "1.21.1", "26.3", "b1.6 Test Build 3"],
        "providers": {
            loader: {
                "source": loader_catalog.SOURCES[loader],
                "versions": rows,
                "fetchedAt": stamp,
                "origin": "cache",
            }
            for loader, rows in versions.items()
        },
    }
    result["providers"]["neoforge"]["releases"] = {
        "1.21.1": ["21.1.90"],
        "26.3": ["26.3.0.9-beta", "26.3.0.2", "26.3.0.10"],
    }
    return result


def write_catalog(cache, snapshot=None):
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / ".blockharbor-loader-catalog.json"
    path.write_text(json.dumps(snapshot or catalog_fixture()), encoding="utf-8")
    return path


def by_loader(response):
    return {row["id"]: row for row in response["loaders"]}


class LoaderMetadataTransportTests(unittest.TestCase):
    def test_permanent_http_errors_preserve_status_and_url_without_retry(self):
        url = "https://meta.fabricmc.net/v2/versions/loader/unsupported"
        for status in (400, 404):
            with (
                self.subTest(status=status),
                patch.object(
                    preparation,
                    "urlopen",
                    side_effect=HTTPError(url, status, "unsupported", {}, None),
                ) as fetch,
                patch.object(preparation.lifecycle, "wait") as wait,
                self.assertRaises(preparation.DownloadError) as error,
            ):
                preparation.json_document(url)
            self.assertEqual(fetch.call_count, 1)
            wait.assert_not_called()
            self.assertEqual(error.exception.status_code, status)
            self.assertEqual(error.exception.url, url)

    def test_transient_network_and_http_errors_retry_then_return_metadata(self):
        url = "https://metadata.example.org/catalog"
        for error in (
            URLError("temporary network failure"),
            HTTPError(url, 503, "temporarily unavailable", {}, None),
            HTTPError(url, 429, "rate limit", {}, None),
        ):
            with (
                self.subTest(error=str(error)),
                patch.object(
                    preparation, "urlopen", side_effect=[error, json_response()]
                ) as fetch,
                patch.object(preparation.lifecycle, "wait") as wait,
            ):
                self.assertEqual(preparation.json_document(url), {"ok": True})
            self.assertEqual(fetch.call_count, 2)
            wait.assert_called_once_with(0.5)

    def test_retry_exhaustion_preserves_transient_status(self):
        url = "https://metadata.example.org/catalog"
        with (
            patch.object(
                preparation,
                "urlopen",
                side_effect=HTTPError(url, 502, "bad gateway", {}, None),
            ) as fetch,
            patch.object(preparation.lifecycle, "wait") as wait,
            self.assertRaises(preparation.DownloadError) as error,
        ):
            preparation.json_document(url)
        self.assertEqual(fetch.call_count, preparation.ATTEMPTS)
        self.assertEqual(wait.call_count, preparation.ATTEMPTS - 1)
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(error.exception.url, url)


class MinecraftVersionIdentifiers(unittest.TestCase):
    def test_historical_and_snapshot_ids_keep_their_exact_names(self):
        for version in (
            "1.20.1",
            "26.1",
            "24w14potato",
            "1.RV-Pre1",
            "b1.6 Test Build 3",
            "rd-132211",
        ):
            with self.subTest(version=version):
                self.assertEqual(game._version_id(version), version)
                self.assertEqual(
                    input_profile("История Minecraft", version, "vanilla"),
                    ("История Minecraft", version, "vanilla"),
                )

    def test_unsafe_ids_cannot_become_paths_in_either_entry_point(self):
        for version in (
            "../1.20.1",
            "..",
            ".",
            "1.20.1/escape",
            "1.20.1\\escape",
            "C:\\game",
            "1.20.1:bad",
            "bad?",
            "bad*",
            "bad<",
            "bad>",
            'bad"',
            "bad|",
            "bad\0",
            "CON",
            "NUL",
            "AUX",
            "COM1",
            "x" * 151,
        ):
            with self.subTest(version=repr(version)):
                with self.assertRaises(ValueError):
                    game._version_id(version)
                with self.assertRaises(ValueError):
                    input_profile("Проверка", version, "fabric")


class LoaderProfileRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.cache = Path(directory) / "game-cache"
        self.instance = Path(directory) / "instance"
        write_catalog(self.cache)

    def test_unsupported_fabric_and_quilt_stop_before_loader_profile_requests(self):
        for loader in ("fabric", "quilt"):
            with self.subTest(loader=loader), patch.object(
                game, "_get_json", side_effect=AssertionError("unsupported metadata request")
            ) as fetch:
                with self.assertRaisesRegex(ValueError, "1\\.7\\.10"):
                    game._meta_profile(self.cache, "1.7.10", loader, None)
                fetch.assert_not_called()
        self.assertFalse((self.cache / "versions").exists())

    def test_unsupported_forge_and_neoforge_stop_before_game_or_java_download(self):
        for version, loader in (("1.0", "forge"), ("1.7.10", "neoforge")):
            with (
                self.subTest(loader=loader),
                patch.object(game, "_get_json", side_effect=AssertionError("metadata")) as metadata,
                patch.object(game, "_prepare_files", side_effect=AssertionError("game download")) as prepare,
                patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("catalog")) as catalog,
            ):
                with self.assertRaisesRegex(ValueError, version.replace(".", r"\.")):
                    game._install(version, loader, self.cache, self.instance, lambda _: None, official=True)
                metadata.assert_not_called()
                prepare.assert_not_called()
                catalog.assert_not_called()
        self.assertFalse((self.instance / game.STAMP).exists())

    def test_pinned_loader_profiles_are_reused_offline_without_metadata_fetch(self):
        snapshot = catalog_fixture()
        for entry in snapshot["providers"].values():
            entry["versions"] = []
        write_catalog(self.cache, snapshot)
        for loader in ("fabric", "quilt"):
            with self.subTest(loader=loader):
                version, pin = "1.20.1", "0.20.1"
                installed = f"{loader}-loader-{pin}-{version}"
                profile = {
                    "id": installed,
                    "inheritsFrom": version,
                    "mainClass": f"{loader}.test.KnotClient",
                }
                manifest = game._manifest_path(self.cache, installed)
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps(profile), encoding="utf-8")
                before = manifest.read_bytes()
                with patch.object(
                    game, "_get_json", side_effect=AssertionError("offline metadata")
                ) as fetch:
                    self.assertEqual(
                        game._meta_profile(self.cache, version, loader, pin),
                        (installed, pin),
                    )
                fetch.assert_not_called()
                self.assertEqual(manifest.read_bytes(), before)

    def test_pinned_forge_and_neoforge_ignore_changed_catalog_and_keep_offline_pin(self):
        snapshot = catalog_fixture()
        for entry in snapshot["providers"].values():
            entry["versions"] = []
        write_catalog(self.cache, snapshot)
        for loader, pin in (("forge", "52.1.20"), ("neoforge", "21.1.90")):
            with self.subTest(loader=loader):
                version = "1.21.1"
                provider = mc.mod_loader.get_mod_loader(loader)
                installed = provider.get_installed_version(version, pin)
                game._atomic_json(game._manifest_path(self.cache, installed), {
                    "id": installed, "inheritsFrom": version, "mainClass": "fixture.Game",
                })
                game._atomic_json(self.instance / game.STAMP, {
                    "version": version, "loader": loader, "loader_version": pin,
                    "id": installed, "cache": str(self.cache),
                })
                with (
                    patch.object(mc.mod_loader, "get_mod_loader", return_value=provider),
                    patch.object(provider, "install", side_effect=AssertionError("installer")) as installer,
                    patch.object(game, "_get_json", side_effect=AssertionError("metadata")),
                    patch.object(game, "_prepare_files", side_effect=AssertionError("download")),
                    patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("catalog")),
                ):
                    self.assertEqual(
                        game._install(version, loader, self.cache, self.instance, lambda _: None, official=True),
                        installed,
                    )
                    installer.assert_not_called()
                self.assertEqual(game._record(self.instance)["loader_version"], pin)

    def test_vendor_rejection_reports_unsupported_pair_without_retry_or_partial_profile(self):
        version = "99.99"  # Not in the bundled snapshot: the vendor decides support.
        for loader in ("fabric", "quilt"):
            url = f"{game.META[loader]}/versions/loader/{version}"
            for status in (400, 404):
                with (
                    self.subTest(loader=loader, status=status),
                    patch.object(preparation, "urlopen", side_effect=HTTPError(url, status, "unsupported", {}, None)) as fetch,
                    patch.object(preparation.lifecycle, "wait") as wait,
                    self.assertRaises(ValueError) as error,
                ):
                    game._meta_profile(self.cache, version, loader, None)
                self.assertIn(version, str(error.exception))
                self.assertIn(loader_catalog.NAMES[loader], str(error.exception))
                self.assertIn("Vanilla", str(error.exception))
                self.assertNotIn("позже", str(error.exception))
                self.assertEqual(fetch.call_count, 1)
                wait.assert_not_called()
        self.assertFalse((self.cache / "versions").exists())

    def test_invalid_remote_pin_names_the_pin_without_marking_whole_game_unsupported(self):
        url = f"{game.META['fabric']}/versions/loader/1.20.1/0.0.0/profile/json"
        path = self.cache / ".blockharbor-loader-catalog.json"
        before = path.read_bytes()
        with (
            patch.object(preparation, "urlopen", side_effect=HTTPError(url, 400, "bad pin", {}, None)) as fetch,
            self.assertRaises(ValueError) as error,
        ):
            game._meta_profile(self.cache, "1.20.1", "fabric", "0.0.0")
        self.assertIn("0.0.0", str(error.exception))
        self.assertIn("1.20.1", str(error.exception))
        self.assertEqual(fetch.call_args.args[0].full_url, url)
        self.assertEqual(path.read_bytes(), before)
        loader_catalog.require_supported(self.cache, "1.20.1", "fabric")

    def test_temporary_metadata_failure_is_not_relabelled_as_incompatibility(self):
        failure = preparation.DownloadError("temporary network outage", url="https://meta.fabricmc.net", status_code=503)
        with patch.object(game, "_get_json", side_effect=failure), self.assertRaises(preparation.DownloadError) as error:
            game._meta_profile(self.cache, "1.20.1", "fabric", None)
        self.assertIs(error.exception, failure)
        self.assertFalse((self.cache / "versions").exists())

    def test_metadata_url_encodes_historical_id_without_changing_local_id(self):
        version, pin = "b1.6 Test Build 3", "0.20.1"
        snapshot = catalog_fixture()
        snapshot["providers"]["fabric"]["versions"].append(version)
        write_catalog(self.cache, snapshot)
        installed = f"fabric-loader-{pin}-{version}"
        profile = {"id": installed, "inheritsFrom": version, "mainClass": "fixture.Game"}
        with patch.object(game, "_get_json", return_value=profile) as fetch:
            self.assertEqual(game._meta_profile(self.cache, version, "fabric", pin), (installed, pin))
        self.assertEqual(fetch.call_args.args[0], f"{game.META['fabric']}/versions/loader/b1.6%20Test%20Build%203/{pin}/profile/json")
        self.assertTrue(game._manifest_path(self.cache, installed).is_file())


class LoaderCatalogTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.cache = Path(directory) / "game-cache"
        self.path = write_catalog(self.cache)

    def test_cached_availability_distinguishes_supported_unsupported_and_unknown(self):
        with patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("network")) as fetch:
            old = by_loader(loader_catalog.availability(self.cache, "1.7.10"))
            self.assertIs(old["fabric"]["supported"], False)
            self.assertIs(old["quilt"]["supported"], False)
            self.assertIs(old["neoforge"]["supported"], False)
            self.assertIs(old["forge"]["supported"], True)
            self.assertIs(old["vanilla"]["supported"], True)
            unknown = by_loader(loader_catalog.availability(self.cache, "99.99"))
            for loader in loader_catalog.SOURCES:
                self.assertIsNone(unknown[loader]["supported"])
                loader_catalog.require_supported(self.cache, "99.99", loader)
                self.assertFalse(old[loader]["stale"])
                self.assertEqual(old[loader]["source"], "cache")
            fetch.assert_not_called()

    def test_failed_refresh_keeps_cached_compatibility_and_marks_it_stale(self):
        before = self.path.read_bytes()
        with patch.object(loader_catalog, "_fetch_provider", side_effect=preparation.DownloadError("offline")) as fetch:
            result = by_loader(loader_catalog.availability(self.cache, "1.20.1", refresh=True))
        self.assertEqual(fetch.call_count, 4)
        self.assertIs(result["fabric"]["supported"], True)
        self.assertIs(result["quilt"]["supported"], True)
        self.assertTrue(result["fabric"]["stale"])
        self.assertEqual(self.path.read_bytes(), before)
        loader_catalog.require_supported(self.cache, "1.20.1", "fabric")

    def test_refresh_updates_and_persists_new_provider_versions(self):
        future = "99.99"
        snapshot = catalog_fixture()
        snapshot["providers"]["fabric"]["versions"].append(future)

        def fetch(loader):
            return {**snapshot["providers"][loader], "origin": "live"}

        with patch.object(loader_catalog, "_fetch_provider", side_effect=fetch) as requests:
            result = by_loader(loader_catalog.availability(self.cache, future, refresh=True))
        self.assertEqual(requests.call_count, 4)
        self.assertIs(result["fabric"]["supported"], True)
        self.assertEqual(result["fabric"]["source"], "live")
        self.assertIn(future, json.loads(self.path.read_text())["providers"]["fabric"]["versions"])
        with patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("second refresh")):
            reopened = by_loader(loader_catalog.availability(self.cache, future))
        self.assertIs(reopened["fabric"]["supported"], True)
        self.assertEqual(reopened["fabric"]["source"], "cache")

    def test_forge_catalog_retains_calendar_releases_but_excludes_manual_patch_versions(self):
        pins = ("1.4.7-6.6.2.534", "1.5.2-7.8.1.738", "1.7.10-10.13.4.1614-1.7.10", "1.21.1-52.1.0", "26.3-66.0.0")
        xml = "<metadata><versioning><versions>" + "".join(f"<version>{pin}</version>" for pin in pins) + "</versions></versioning></metadata>"
        with patch.object(preparation, "metadata_response", return_value=SimpleNamespace(text=xml)):
            result = loader_catalog._fetch_provider("forge")
        self.assertEqual(set(result["versions"]), {"1.5.2", "1.7.10", "1.21.1", "26.3"})

    def test_neoforge_catalog_understands_legacy_calendar_and_preview_pins(self):
        pins = ("20.2.3-beta", "21.1.90", "26.1.2.3", "26.3.0.10", "26.1.0.0-alpha.15+pre-3", "26.1.0.0-alpha.14+snapshot-11")
        xml = "<metadata><versioning><versions>" + "".join(f"<version>{pin}</version>" for pin in pins) + "</versions></versioning></metadata>"
        with patch.object(preparation, "metadata_response", return_value=SimpleNamespace(text=xml)):
            result = loader_catalog._fetch_provider("neoforge")
        self.assertEqual(set(result["versions"]), {"1.20.2", "1.21.1", "26.1.2", "26.3", "26.1-pre-3", "26.1-snapshot-11"})
        with patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("network")):
            self.assertEqual(loader_catalog.latest_neoforge(self.cache, "26.3"), "26.3.0.10")

    def test_official_manifest_keeps_all_types_and_exact_ids_without_duplicates(self):
        rows = [
            {"id": "26.3", "type": "release"},
            {"id": "26.4-snapshot-10", "type": "snapshot"},
            {"id": "b1.6 Test Build 3", "type": "old_beta"},
            {"id": "rd-132211", "type": "old_alpha"},
            {"id": "26.3", "type": "release"},
        ]
        with patch.object(game, "_get_json", return_value={"versions": rows}):
            self.assertEqual(game.version_catalog(), rows[:-1])
            self.assertEqual(game.list_versions(), ["26.3"])


class LoaderCompatibilityApiTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.app = App(Path(directory))
        self.pid = self.app.store.create("Существующая сборка", "1.20.1", "vanilla")
        write_catalog(self.app.store.root / "game-cache")
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.app = self.app
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def call(self, path, data=None, *, auth=True):
        headers = {"Authorization": "Bearer " + self.app.token} if auth else {}
        if data is not None:
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        with self.opener.open(Request(self.base + path, data=data, headers=headers), timeout=10) as response:
            return json.loads(response.read())

    def test_loader_endpoint_is_authenticated_validated_and_reads_local_cache(self):
        with patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("network")) as fetch:
            for path, auth, expected in (
                ("/api/loaders?version=1.7.10", False, 403),
                ("/api/loaders?" + urlencode({"version": "../escape"}), True, 400),
            ):
                with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                    self.call(path, auth=auth)
                self.assertEqual(error.exception.code, expected)
            result = by_loader(self.call("/api/loaders?version=1.7.10"))
            self.assertIs(result["forge"]["supported"], True)
            self.assertIs(result["fabric"]["supported"], False)
            fetch.assert_not_called()

    def test_unsupported_create_preserves_existing_profiles_and_stops_before_steam(self):
        before = self.app.store.all()
        active = self.app.store.get("active")
        folders = set(self.app.store.instances.iterdir())
        with (
            patch.object(e4steam, "support", side_effect=AssertionError("Steam before compatibility")) as steam,
            patch.object(loader_catalog, "_fetch_provider", side_effect=AssertionError("network")) as fetch,
            self.assertRaises(HTTPError) as error,
        ):
            self.call("/api/action", {"action": "create", "name": "Невозможная пара", "version": "1.7.10", "loader": "fabric"})
        self.assertEqual(error.exception.code, 400)
        self.assertIn("1.7.10", error.exception.read().decode())
        steam.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(self.app.store.all(), before)
        self.assertEqual(self.app.store.get("active"), active)
        self.assertEqual(set(self.app.store.instances.iterdir()), folders)

    def test_release_refresh_exposes_and_persists_full_catalog_with_release_only_legacy_field(self):
        rows = [
            {"id": "27.1", "type": "release"},
            {"id": "27.1-snapshot-1", "type": "snapshot"},
            {"id": "b1.6 Test Build 3", "type": "old_beta"},
        ]
        with patch.object(game, "version_catalog", return_value=rows) as fetch:
            result = self.call("/api/releases?refresh=1")
        fetch.assert_called_once()
        self.assertIn("27.1", result["versions"])
        self.assertNotIn("27.1-snapshot-1", result["versions"])
        self.assertNotIn("b1.6 Test Build 3", result["versions"])
        self.assertEqual(result["allVersions"][:3], rows)
        reopened = App(self.app.store.root)
        self.assertEqual(reopened.version_catalog()[:3], rows)
        self.assertEqual(reopened.store.profile(self.pid)["version"], "1.20.1")

    def test_release_endpoint_keeps_all_categories_available_when_refresh_is_offline(self):
        before = self.app.version_catalog()
        with patch.object(game, "version_catalog", side_effect=preparation.DownloadError("offline")):
            result = self.call("/api/releases?refresh=1")
        self.assertTrue(result["stale"])
        self.assertIn("сохранённые", result["message"])
        self.assertEqual(result["allVersions"], before)
        self.assertEqual({row["type"] for row in result["allVersions"]}, {"release", "snapshot", "old_beta", "old_alpha"})
        self.assertEqual(result["versions"], self.app.release_catalog())

    def test_closing_launcher_cancels_loader_refresh_without_late_cache_publication(self):
        path = self.app.store.root / "game-cache" / ".blockharbor-loader-catalog.json"
        before = path.read_bytes()
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        scopes, outcome = [], []
        snapshot = catalog_fixture()

        def fetch(loader):
            with lock:
                scopes.append(lifecycle.current_scope())
                if len(scopes) == len(loader_catalog.SOURCES):
                    entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release provider workers")
            entry = {**snapshot["providers"][loader], "origin": "live"}
            entry["versions"] = [*entry["versions"], "99.99"]
            return entry

        def request():
            try:
                outcome.append(self.call("/api/loaders?version=1.20.1&refresh=1"))
            except HTTPError as error:
                outcome.append({"status": error.code, "error": json.loads(error.read())["error"]})
            except Exception as error:
                outcome.append(error)

        with patch.object(loader_catalog, "_fetch_provider", side_effect=fetch):
            client = threading.Thread(target=request, daemon=True)
            client.start()
            try:
                self.assertTrue(entered.wait(5), "Provider workers did not start")
                self.assertEqual(scopes, [self.app.scope] * len(loader_catalog.SOURCES))
                self.assertTrue(self.app._signal_close("user"))
                self.assertTrue(self.app.scope.event.is_set())
            finally:
                release.set()
                client.join(timeout=5)
            self.assertFalse(client.is_alive(), "Cancelled HTTP request did not finish")
        self.assertEqual(len(outcome), 1)
        # The HTTP handler deliberately closes a cancelled request without a
        # response; a structured cancellation error is also safe to return.
        if not isinstance(outcome[0], RemoteDisconnected):
            self.assertIsInstance(outcome[0], dict)
            self.assertEqual(outcome[0]["status"], 400)
            self.assertIn("Операция остановлена", outcome[0]["error"])
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

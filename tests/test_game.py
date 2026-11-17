import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

import minecraft_launcher_lib as mc
from launcher import game


class GameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cache = self.root / "library"
        self.cache.mkdir()
        self.instance = self.root / "Остров с друзьями"
        self.profile_file = self.cache / game.STORE_PROFILES
        self.original = {
            "profiles": {"existing": {"name": "Старый мир"}},
            "settings": {"language": "ru_RU"},
        }
        self.profile_file.write_text(json.dumps(self.original), encoding="utf-8")
        paths = patch.object(game, "_profile_paths", return_value=[self.profile_file])
        closed = patch.object(game, "_check_official_closed")
        paths.start()
        self.check_closed = closed.start()
        self.addCleanup(paths.stop)
        self.addCleanup(closed.stop)

    def prepare(self, loader="vanilla", memory=4096):
        return game.official_prepare(
            "1.20.1", loader, self.instance, "BlockHarbor · Остров", memory_mb=memory
        )

    def test_official_upsert_preserves_other_profiles_and_game_dir(self):
        with patch.object(game, "list_versions", return_value=["1.20.1"]):
            self.assertEqual(self.prepare(), "BlockHarbor · Остров")
            self.prepare(memory=6144)
        data = json.loads(self.profile_file.read_text(encoding="utf-8"))
        self.assertEqual(data["settings"], self.original["settings"])
        self.assertEqual(
            data["profiles"]["existing"], self.original["profiles"]["existing"]
        )
        self.assertEqual(len(data["profiles"]), 2)
        entry = next(v for k, v in data["profiles"].items() if k != "existing")
        self.assertEqual(
            (entry["gameDir"], entry["lastVersionId"], entry["javaArgs"]),
            (str(self.instance), "1.20.1", "-Xmx6144M"),
        )
        self.check_closed.side_effect = AssertionError(
            "Existing installation must work with the launcher open"
        )
        self.assertEqual(self.prepare(memory=6144), "BlockHarbor · Остров")

    def test_fabric_real_profile_schema_and_loader_pin_survive_relaunch(self):
        installed = "fabric-loader-0.20.10-1.20.1"
        profile = {
            "id": installed,
            "inheritsFrom": "1.20.1",
            "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
        }
        versions = [
            {"loader": {"stable": True, "version": v}} for v in ("0.20.2", "0.20.10")
        ]
        with patch.object(game, "_get_json", side_effect=[versions, profile]) as get:
            self.prepare("fabric")
            self.prepare("fabric", memory=8192)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            get.call_args.args[0],
            "https://meta.fabricmc.net/v2/versions/loader/1.20.1/0.20.10/profile/json",
        )
        self.assertEqual(
            json.loads(
                game._manifest_path(self.cache, installed).read_text(encoding="utf-8")
            ),
            profile,
        )
        self.assertEqual(game._record(self.instance)["loader_version"], "0.20.10")

    def test_bad_version_or_loader_metadata_preserves_profile_list(self):
        original = self.profile_file.read_bytes()
        with (
            patch.object(game, "list_versions", return_value=[]),
            self.assertRaises(ValueError),
        ):
            self.prepare()
        bad_profile = {"id": "wrong", "inheritsFrom": "1.20.1", "mainClass": "wrong"}
        with (
            patch.object(
                game,
                "_get_json",
                side_effect=[[{"loader": {"version": "0.20.10"}}], bad_profile],
            ),
            self.assertRaises(ValueError),
        ):
            self.prepare("fabric")
        self.assertEqual(self.profile_file.read_bytes(), original)
        self.assertFalse((self.instance / game.STAMP).exists())

    def test_forge_uses_recommended_build_and_mojang_runtime(self):
        version = "1.21.1"
        installed = "1.21.1-forge-52.1.20"
        provider = mc.mod_loader.get_mod_loader("forge")
        base = game._manifest_path(self.cache, version)
        game._atomic_json(
            base,
            {"javaVersion": {"component": "java-runtime-delta", "majorVersion": 21}},
        )
        managed_java = str(
            self.cache / "runtime" / "java-runtime-delta" / "bin" / "java.exe"
        )

        def fake_install(*args, **kwargs):
            game._atomic_json(
                game._manifest_path(self.cache, installed), {"id": installed}
            )
            return installed

        with (
            patch.object(mc.mod_loader, "get_mod_loader", return_value=provider),
            patch.object(
                game,
                "_get_json",
                return_value={
                    "promos": {
                        "1.21.1-recommended": "52.1.20",
                        "1.21.1-latest": "52.1.21",
                    }
                },
            ),
            patch.object(mc.install, "install_minecraft_version"),
            patch.object(
                mc.runtime, "get_executable_path", return_value=managed_java
            ) as runtime,
            patch.object(provider, "install", side_effect=fake_install) as installer,
        ):
            game.official_prepare(version, "forge", self.instance, "Forge · Test")
        runtime.assert_called_once_with("java-runtime-delta", self.cache)
        self.assertEqual(installer.call_args.kwargs["loader_version"], "52.1.20")
        self.assertEqual(installer.call_args.kwargs["java"], managed_java)
        self.assertEqual(game._record(self.instance)["id"], installed)

    def test_direct_alias_has_own_uuid_and_never_receives_online_token(self):
        self.instance.mkdir()
        version = "1.20.1"
        metadata = {
            "id": version,
            "type": "release",
            "libraries": [],
            "mainClass": "test.Main",
            "arguments": {
                "game": [
                    "--username",
                    "${auth_player_name}",
                    "--uuid",
                    "${auth_uuid}",
                    "--gameDir",
                    "${game_directory}",
                    "--accessToken",
                    "${auth_access_token}",
                    "--clientId",
                    "${clientid}",
                    "--xuid",
                    "${auth_xuid}",
                ]
            },
        }
        game._atomic_json(game._manifest_path(self.cache, version), metadata)
        game._atomic_json(
            self.instance / game.STAMP, {"id": version, "cache": str(self.cache)}
        )
        session = {
            "name": "Owner",
            "id": uuid4().hex,
            "client_id": str(uuid4()),
            "access_token": "fixture",
        }
        with patch.object(game.subprocess, "Popen") as run:
            game.launch(self.cache, self.instance, session, "Notch")
            args = run.call_args.args[0]
            self.assertEqual(
                args[args.index("--uuid") + 1], "b50ad385829d3141a2167e7d7539ba7f"
            )
            self.assertIn("Notch", args)
            self.assertNotIn(session["access_token"], args)
            self.assertIn(str(self.instance), args)
            game.launch(self.cache, self.instance, session)
            args = run.call_args.args[0]
            self.assertIn(session["access_token"], args)
            self.assertIn(session["name"], args)
            self.assertIn(session["id"], args)
            self.assertNotIn("${auth_xuid}", args)
            self.assertNotIn("${clientid}", args)
        with self.assertRaises(ValueError):
            game.launch(self.cache, self.instance, {}, "LocalUser")

    def test_profile_change_during_write_is_preserved(self):
        self.profile_file.write_bytes(b"new-data")
        with self.assertRaises(RuntimeError):
            game._atomic_json(self.profile_file, self.original, expected=b"old-data")
        self.assertEqual(self.profile_file.read_bytes(), b"new-data")
        self.assertEqual(list(self.cache.glob(".blockharbor-*")), [])

    def test_first_run_opens_installed_launcher_or_explains_installation(self):
        with (
            patch.object(game, "_profile_paths", return_value=[]),
            patch.object(game, "open_official") as open_launcher,
        ):
            with self.assertRaisesRegex(RuntimeError, "Открыт Minecraft Launcher"):
                self.prepare()
            open_launcher.assert_called_once_with()
            open_launcher.side_effect = RuntimeError("missing")
            with self.assertRaisesRegex(
                RuntimeError, r"https://www.minecraft.net/download"
            ):
                self.prepare()

    def test_browser_login_checks_state_and_returns_only_java_credentials(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            redirect = f"http://localhost:{sock.getsockname()[1]}/callback"
        responses = []
        browser = None
        client_id = str(uuid4())
        profile = {
            "id": uuid4().hex,
            "name": "MyJavaName",
            "access_token": "access",
            "refresh_token": "private",
        }

        def open_login(url):
            nonlocal browser
            state = parse_qs(urlsplit(url).query)["state"][0]

            def authorize():
                opener = build_opener(ProxyHandler({}))
                for value in ("incorrect", state):
                    query = urlencode({"state": value, "code": "test-code"})
                    try:
                        with opener.open(redirect + "?" + query, timeout=4) as response:
                            responses.append(response.status)
                    except HTTPError as exc:
                        responses.append(exc.code)

            browser = threading.Thread(target=authorize, daemon=True)
            browser.start()
            return True

        with (
            patch.object(game, "REDIRECT_URI", redirect),
            patch.object(game.webbrowser, "open", side_effect=open_login),
            patch.object(
                mc.microsoft_account, "complete_login", return_value=profile
            ) as exchange,
        ):
            session = game.login(client_id)
        browser.join(5)
        self.assertEqual(responses, [400, 200])
        self.assertEqual(
            exchange.call_args.args[:4], (client_id, None, redirect, "test-code")
        )
        self.assertTrue(exchange.call_args.args[4])  # PKCE verifier
        self.assertEqual(
            session,
            {
                "id": profile["id"],
                "name": "MyJavaName",
                "access_token": "access",
                "client_id": client_id,
            },
        )


if __name__ == "__main__":
    unittest.main()

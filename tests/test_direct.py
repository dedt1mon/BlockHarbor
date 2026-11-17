import hashlib
import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import minecraft_launcher_lib as mc
from launcher import game, preparation


class DirectPlayTests(unittest.TestCase):
    def setUp(self):
        host_arch = patch("platform.architecture", return_value=("64bit", ""))
        host_arch.start()
        self.addCleanup(host_arch.stop)
        compatible_java = patch.object(
            preparation, "legacy_system_java", return_value="java"
        )
        compatible_java.start()
        self.addCleanup(compatible_java.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.cache = self.root / "game-cache"
        self.instance = self.root / "Сборка"
        self.version = "1.20.1"
        self.jar = game._manifest_path(self.cache, self.version).with_suffix(".jar")
        self.library = (
            self.cache
            / "libraries"
            / "org"
            / "example"
            / "fixture"
            / "1"
            / "fixture-1.jar"
        )
        self.asset_key = hashlib.sha1(b"asset").hexdigest()
        self.asset = (
            self.cache / "assets" / "objects" / self.asset_key[:2] / self.asset_key
        )
        self.game = {
            "id": self.version,
            "type": "release",
            "mainClass": "example.Game",
            "arguments": {
                "jvm": ["-cp", "${classpath}"],
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
                ],
            },
            "libraries": [
                {
                    "name": "org.example:fixture:1",
                    "downloads": {
                        "artifact": {
                            "path": "org/example/fixture/1/fixture-1.jar",
                            "size": 3,
                        }
                    },
                }
            ],
            "downloads": {"client": {"size": 4}},
            "assets": "5",
            "assetIndex": {"id": "5"},
        }

    def write_cache(self):
        game._atomic_json(game._manifest_path(self.cache, self.version), self.game)
        self.jar.write_bytes(b"game")
        self.library.parent.mkdir(parents=True, exist_ok=True)
        self.library.write_bytes(b"lib")
        self.asset.parent.mkdir(parents=True, exist_ok=True)
        self.asset.write_bytes(b"asset")
        game._atomic_json(
            self.cache / "assets/indexes/5.json",
            {"objects": {"music/test": {"size": 5, "hash": self.asset_key}}},
        )

    def test_local_launch_first_download_once_then_runs_entirely_offline(self):
        with (
            patch.object(
                mc.install,
                "install_minecraft_version",
                side_effect=lambda *a, **kw: self.write_cache(),
            ) as fetch,
            patch.object(
                game, "open_official", side_effect=AssertionError("external launcher")
            ),
            patch.object(game, "login", side_effect=AssertionError("account gate")),
            patch.object(subprocess, "Popen") as run,
        ):
            game.install(self.version, "vanilla", self.cache, self.instance)
            game.launch_local(self.cache, self.instance, "DedTimon", 3072)
            game.install(self.version, "vanilla", self.cache, self.instance)
            self.assertEqual(fetch.call_count, 1)
            argv = run.call_args.args[0]
            self.assertIn("-Xmx3072M", argv)
            self.assertEqual(argv[argv.index("--username") + 1], "DedTimon")
            self.assertEqual(
                argv[argv.index("--uuid") + 1], game._java_offline_uuid("DedTimon")
            )
            self.assertEqual(argv[argv.index("--gameDir") + 1], str(self.instance))
            self.assertEqual(argv[argv.index("--accessToken") + 1], "0")
            self.assertEqual(argv[argv.index("--clientId") + 1], "")
            self.assertNotIn("--demo", argv)

    def test_legacy_release_without_java_metadata_installs_managed_java_only_when_needed(
        self,
    ):
        self.version = "1.6.4"
        self.game["id"] = self.version
        self.jar = game._manifest_path(self.cache, self.version).with_suffix(".jar")
        java = self.cache / "runtime/jre-legacy/windows-x64/jre-legacy/bin/java.exe"
        inventory = java.parents[2] / "jre-legacy.sha1"

        def install_runtime(component, cache, **_kwargs):
            self.assertEqual(component, "jre-legacy")
            java.parent.mkdir(parents=True)
            java.write_bytes(b"MZ managed Java")
            inventory.write_text(
                f"bin/java.exe /#// {hashlib.sha1(java.read_bytes()).hexdigest()} 0\n"
            )

        with (
            patch.object(preparation, "legacy_system_java", return_value=None),
            patch.object(
                mc.install,
                "install_minecraft_version",
                side_effect=lambda *_a, **_k: self.write_cache(),
            ) as setup,
            patch.object(
                mc.runtime,
                "get_executable_path",
                side_effect=lambda *_a: str(java) if java.exists() else None,
            ),
            patch.object(
                mc.runtime, "install_jvm_runtime", side_effect=install_runtime
            ) as runtime,
            patch.object(subprocess, "Popen") as spawn,
        ):
            game.install(self.version, "vanilla", self.cache, self.instance)
            self.assertEqual(game._record(self.instance)["java"], str(java))
            game.launch_local(self.cache, self.instance, "Player", 2048)
            self.assertEqual(spawn.call_args.args[0][0], str(java))
            game.install(self.version, "vanilla", self.cache, self.instance)
            self.assertEqual(runtime.call_count, 1)
            self.assertEqual(setup.call_count, 1)
        with (
            patch.object(
                preparation, "legacy_system_java", return_value="existing-java.exe"
            ),
            patch.object(mc.runtime, "install_jvm_runtime") as runtime,
        ):
            self.assertEqual(
                game._legacy_java(self.cache, lambda _: None, {}), "existing-java.exe"
            )
            runtime.assert_not_called()

    def test_pinned_fabric_runs_from_private_cache_with_real_command_builder(self):
        self.write_cache()
        installed = "fabric-loader-0.20.1-1.20.1"
        profile = {
            "id": installed,
            "type": "release",
            "inheritsFrom": self.version,
            "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
            "libraries": [],
            "arguments": {"game": []},
        }
        game._atomic_json(game._manifest_path(self.cache, installed), profile)
        game._manifest_path(self.cache, installed).with_suffix(".jar").write_bytes(
            b"game"
        )
        game._atomic_json(
            self.instance / game.STAMP,
            {
                "version": self.version,
                "loader": "fabric",
                "loader_version": "0.20.1",
                "id": installed,
                "cache": str(self.cache),
            },
        )
        with (
            patch.object(game, "_get_json", side_effect=AssertionError("network")),
            patch.object(
                mc.install,
                "install_minecraft_version",
                side_effect=AssertionError("repair"),
            ),
            patch.object(subprocess, "Popen") as run,
        ):
            self.assertEqual(
                game.install(self.version, "fabric", self.cache, self.instance),
                installed,
            )
            game.launch_local(self.cache, self.instance, "Player_02")
            self.assertIn(profile["mainClass"], run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["cwd"], self.instance)

    def test_missing_asset_and_truncated_library_are_repaired(self):
        self.write_cache()
        for path, bad in ((self.asset, None), (self.library, b"x")):
            with self.subTest(path=path.name):
                if bad is None:
                    path.unlink()
                else:
                    path.write_bytes(bad)
                with patch.object(
                    mc.install,
                    "install_minecraft_version",
                    side_effect=lambda *a, **kw: self.write_cache(),
                ) as fetch:
                    game.install(self.version, "vanilla", self.cache, self.instance)
                    fetch.assert_called_once()
                    self.assertIsNone(
                        game._missing_install_file(
                            self.cache, self.version, self.version
                        )
                    )

    def test_incomplete_download_does_not_start_or_claim_install(self):
        self.write_cache()
        self.asset.unlink()
        with (
            patch.object(mc.install, "install_minecraft_version"),
            self.assertRaisesRegex(RuntimeError, "Загрузка не завершена"),
        ):
            game.install(self.version, "vanilla", self.cache, self.instance)
        self.assertFalse((self.instance / game.STAMP).exists())

    def test_missing_java_component_is_detected_before_reuse(self):
        self.game["javaVersion"] = {
            "component": "java-runtime-gamma",
            "majorVersion": 17,
        }
        self.write_cache()
        with patch.object(mc.runtime, "get_executable_path", return_value=None):
            self.assertEqual(
                game._missing_install_file(self.cache, self.version, self.version),
                "Java (java-runtime-gamma)",
            )
        root = self.cache / "runtime/java-runtime-gamma/windows-x64"
        runtime = root / "java-runtime-gamma"
        java = runtime / "bin/java.exe"
        java.parent.mkdir(parents=True)
        java.write_bytes(b"runtime")
        (root / "java-runtime-gamma.sha1").write_text(
            f"bin/java.exe /#// {hashlib.sha1(b'runtime').hexdigest()} 0\n"
            f"bin/server/jvm.dll /#// {hashlib.sha1(b'vm').hexdigest()} 0\n"
        )
        with patch.object(mc.runtime, "get_executable_path", return_value=str(java)):
            self.assertIn(
                "jvm.dll",
                game._missing_install_file(self.cache, self.version, self.version),
            )
            (runtime / "bin/server").mkdir()
            (runtime / "bin/server/jvm.dll").write_bytes(b"vm")
            self.assertIsNone(
                game._missing_install_file(self.cache, self.version, self.version)
            )

    def test_loader_library_is_repaired_atomically_and_its_checksum_is_verified(self):
        actual = io.BytesIO()
        with zipfile.ZipFile(actual, "w") as jar:
            jar.writestr("pkg/Main.class", b"fixture")
        expected = actual.getvalue()
        installed = "fabric-loader-0.20.1-1.20.1"
        game._atomic_json(
            game._manifest_path(self.cache, installed),
            {
                "libraries": [
                    {
                        "name": "org.example:fixture:1",
                        "url": "https://example.org/",
                        "size": len(expected),
                        "sha256": hashlib.sha256(expected).hexdigest(),
                    }
                ]
            },
        )
        self.library.parent.mkdir(parents=True)
        self.library.write_bytes(b"partial")

        def response(content):
            stream = io.BytesIO(content)
            stream.headers = {"Content-Length": str(len(content))}
            stream.geturl = lambda: "https://example.org/library.jar"
            return stream

        with (
            patch.object(
                preparation, "urlopen", return_value=response(b"bad response")
            ),
            self.assertRaisesRegex(ValueError, "проверить"),
        ):
            game._repair_loader_files(self.cache, installed, lambda _msg: None)
        self.assertEqual(self.library.read_bytes(), b"partial")
        with patch.object(
            preparation, "urlopen", return_value=response(expected)
        ) as download:
            for _ in range(2):
                game._repair_loader_files(self.cache, installed, lambda _msg: None)
            self.assertEqual(download.call_count, 1)
        self.assertEqual(self.library.read_bytes(), expected)

    def test_invalid_name_never_starts_a_child(self):
        with patch.object(subprocess, "Popen") as start:
            for bad in ("", None, "Дед Тимон", "12345678901234567"):
                with self.subTest(name=bad), self.assertRaisesRegex(ValueError, "Ник"):
                    game.launch_local(self.cache, self.instance, bad)
            start.assert_not_called()

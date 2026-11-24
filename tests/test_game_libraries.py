"""Library metadata regressions with real command construction and local JARs."""

import hashlib
import io
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import minecraft_launcher_lib as mc

from launcher import game, lifecycle, preparation


class GameLibraryTests(unittest.TestCase):
    def setUp(self):
        for target, result in (
            ("platform.system", "Windows"),
            ("platform.architecture", ("64bit", "")),
            ("launcher.preparation.legacy_system_java", "java"),
        ):
            active = patch(target, return_value=result)
            active.start()
            self.addCleanup(active.stop)
        for target in ("launcher.game._get_json", "launcher.preparation.urlopen"):
            active = patch(target, side_effect=AssertionError("unexpected network"))
            active.start()
            self.addCleanup(active.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "game-cache"
        self.instance = self.root / "Старая сборка"
        self.version = "1.7.10"
        self.jar = game._manifest_path(self.cache, self.version).with_suffix(".jar")
        self.native_bytes = self._zip("native/fixture.dll", b"native fixture")
        self.artifact_bytes = self._zip("example/Fixture.class", b"class fixture")
        self.metadata = {
            "id": self.version,
            "type": "release",
            "mainClass": "net.minecraft.client.main.Main",
            "minecraftArguments": (
                "--username ${auth_player_name} --uuid ${auth_uuid} "
                "--gameDir ${game_directory} --accessToken ${auth_access_token}"
            ),
            "libraries": [],
            "downloads": {"client": {"size": 4}},
        }

    @staticmethod
    def _zip(name, content):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(name, content)
        return stream.getvalue()

    @staticmethod
    def _maven_relative(name):
        coordinate, _, extension = name.partition("@")
        group, artifact, version, *classifiers = coordinate.split(":")
        suffix = "".join("-" + item for item in classifiers)
        return (
            group.replace(".", "/")
            + f"/{artifact}/{version}/{artifact}-{version}{suffix}.{extension or 'jar'}"
        )

    def _download(self, path, content):
        return {
            "path": path,
            "size": len(content),
            "sha1": hashlib.sha1(content).hexdigest(),
            "url": "https://libraries.example.invalid/" + path,
        }

    def _native_library(self, name, artifact=False, arch=False):
        plain = self._maven_relative(name)
        classifiers = {}
        natives = {}
        for system in ("windows", "linux", "osx"):
            natives[system] = "natives-" + system + ("-${arch}" if arch else "")
            for bits in ("32", "64") if arch else ("",):
                classifier = "natives-" + system + ("-" + bits if arch else "")
                path = plain.removesuffix(".jar") + "-" + classifier + ".jar"
                classifiers[classifier] = self._download(path, self.native_bytes)
        downloads = {"classifiers": classifiers}
        if artifact:
            downloads["artifact"] = self._download(plain, self.artifact_bytes)
        return {
            "name": name,
            "downloads": downloads,
            "natives": natives,
            "extract": {"exclude": ["META-INF/"]},
        }

    def _artifact_library(self, name, path=None):
        return {
            "name": name,
            "downloads": {
                "artifact": self._download(
                    path or self._maven_relative(name), self.artifact_bytes
                )
            },
        }

    def _put(self, entry, content):
        path = self.cache / "libraries" / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _write_metadata(self, libraries):
        self.metadata["libraries"] = libraries
        game._atomic_json(game._manifest_path(self.cache, self.version), self.metadata)
        self.jar.write_bytes(b"game")

    def _classpath(self, command):
        return command[command.index("-cp") + 1].split(
            mc.command.get_classpath_separator()
        )

    def _command(self, installed=None):
        return game._minecraft_command(
            installed or self.version,
            self.cache,
            {"username": "Fixture", "uuid": "0" * 32, "token": "0"},
        )

    def _missing(self, installed=None):
        return game._missing_install_file(
            self.cache, self.version, installed or self.version
        )

    def test_classifiers_only_libraries_install_and_launch_without_plain_jars(self):
        # The first two coordinates reproduce the reported old-version errors;
        # the third prevents a fix that merely exempts those two filenames.
        for name in (
            "org.lwjgl.lwjgl:lwjgl-platform:2.9.1",
            "net.java.jinput:jinput-platform:2.0.5",
            "org.example:another-native-library:7",
        ):
            with self.subTest(library=name):
                library = self._native_library(name)
                self._write_metadata([library])
                native = self._put(
                    library["downloads"]["classifiers"]["natives-windows"],
                    self.native_bytes,
                )
                plain = self.cache / "libraries" / self._maven_relative(name)
                self.assertFalse(plain.exists())
                self.assertIsNone(self._missing())
                process = Mock()
                process.poll.return_value = 0
                process.wait.return_value = 0
                with (
                    patch.object(mc.install, "install_minecraft_version") as install,
                    patch.object(subprocess, "Popen", return_value=process) as spawn,
                ):
                    installed = game.install(
                        self.version, "vanilla", self.cache, self.instance
                    )
                    self.assertEqual(installed, self.version)
                    self.assertIs(
                        game.launch_local(self.cache, self.instance, "Fixture", 2048),
                        process,
                    )
                    install.assert_not_called()
                    command = spawn.call_args.args[0]
                    self.assertEqual(self._classpath(command), [str(native), str(self.jar)])
                    self.assertNotIn(str(plain), self._classpath(command))
                    self.assertEqual(command[command.index("--username") + 1], "Fixture")
                self.assertTrue((self.instance / game.STAMP).is_file())

    def test_artifact_path_is_authoritative_for_validation_and_command(self):
        name = "org.example:client:1"
        library = self._artifact_library(name, "custom/published/client-special.jar")
        self._write_metadata([library])
        artifact = self._put(library["downloads"]["artifact"], self.artifact_bytes)
        self.assertIsNone(self._missing())
        self.assertEqual(self._classpath(self._command()), [str(artifact), str(self.jar)])
        artifact.unlink()
        # A Maven-derived file must not hide a missing explicitly named artifact.
        self._put({"path": self._maven_relative(name)}, self.artifact_bytes)
        self.assertEqual(self._missing(), artifact.name)

    def test_same_size_library_corruption_is_detected_and_repaired(self):
        library = self._artifact_library("org.example:integrity:1")
        self._write_metadata([library])
        entry = library["downloads"]["artifact"]
        artifact = self._put(entry, self.artifact_bytes)
        self.assertIsNone(self._missing())
        artifact.write_bytes(b"x" * len(self.artifact_bytes))
        self.assertEqual(self._missing(), artifact.name)
        with patch.object(mc.install, "install_minecraft_version", side_effect=lambda *_args, **_kwargs: artifact.write_bytes(self.artifact_bytes)) as install:
            self.assertEqual(game.install(self.version, "vanilla", self.cache, self.instance), self.version)
            install.assert_called_once()
        self.assertIsNone(self._missing())

    def test_same_size_client_corruption_is_detected_without_network(self):
        self.metadata["downloads"]["client"]["sha1"] = hashlib.sha1(b"game").hexdigest()
        self._write_metadata([])
        self.assertIsNone(self._missing())
        self.jar.write_bytes(b"xxxx")
        self.assertEqual(self._missing(), self.jar.name)

    def test_unchanged_artifact_hashes_reuse_the_stamp_cache(self):
        library = self._artifact_library("org.example:cached:1")
        self._write_metadata([library])
        self._put(library["downloads"]["artifact"], self.artifact_bytes)
        self.assertIsNone(self._missing())
        with patch.object(hashlib, "file_digest", side_effect=AssertionError("unchanged artifact rehashed")):
            self.assertIsNone(self._missing())

    def test_actual_artifact_with_native_classifier_coordinate_is_required(self):
        library = self._artifact_library("org.lwjgl:lwjgl:3.3.3:natives-windows")
        self._write_metadata([library])
        artifact = self.cache / "libraries" / library["downloads"]["artifact"]["path"]
        self.assertEqual(self._missing(), artifact.name)
        self._put(library["downloads"]["artifact"], self.artifact_bytes)
        self.assertIsNone(self._missing())
        self.assertEqual(self._classpath(self._command()), [str(artifact), str(self.jar)])

    def test_library_with_artifact_and_native_requires_both(self):
        library = self._native_library("org.example:combined-library:1", artifact=True)
        self._write_metadata([library])
        artifact = self._put(library["downloads"]["artifact"], self.artifact_bytes)
        native = self._put(
            library["downloads"]["classifiers"]["natives-windows"], self.native_bytes
        )
        self.assertIsNone(self._missing())
        self.assertEqual(
            self._classpath(self._command()), [str(artifact), str(native), str(self.jar)]
        )
        artifact.unlink()
        self.assertEqual(self._missing(), artifact.name)
        artifact.write_bytes(self.artifact_bytes)
        native.unlink()
        self.assertEqual(self._missing(), native.name)

    def test_missing_empty_or_truncated_native_blocks_install_without_stamp(self):
        library = self._native_library("org.lwjgl.lwjgl:lwjgl-platform:2.9.1")
        self._write_metadata([library])
        entry = library["downloads"]["classifiers"]["natives-windows"]
        native = self.cache / "libraries" / entry["path"]
        for content in (None, b"", self.native_bytes[:-1]):
            with self.subTest(size=None if content is None else len(content)):
                if content is None:
                    native.unlink(missing_ok=True)
                else:
                    self._put(entry, content)
                self.assertEqual(self._missing(), native.name)
                with patch.object(mc.install, "install_minecraft_version") as install:
                    with self.assertRaisesRegex(RuntimeError, native.name):
                        game.install(self.version, "vanilla", self.cache, self.instance)
                    self.assertEqual(install.call_count, 2)
                self.assertFalse((self.instance / game.STAMP).exists())

    def test_truncated_selected_native_is_repaired_before_install_succeeds(self):
        library = self._native_library("net.java.jinput:jinput-platform:2.0.5")
        self._write_metadata([library])
        entry = library["downloads"]["classifiers"]["natives-windows"]
        native = self._put(entry, self.native_bytes[:-1])
        with patch.object(
            mc.install,
            "install_minecraft_version",
            side_effect=lambda *_args, **_kwargs: native.write_bytes(self.native_bytes),
        ) as install:
            self.assertEqual(
                game.install(self.version, "vanilla", self.cache, self.instance),
                self.version,
            )
            install.assert_called_once()
        self.assertIsNone(self._missing())
        self.assertTrue((self.instance / game.STAMP).is_file())

    def test_only_native_selected_for_system_and_architecture_is_required(self):
        library = self._native_library("org.example:architecture-native:1", arch=True)
        self._write_metadata([library])
        for system, key, bits in (
            ("Windows", "windows", "32"),
            ("Windows", "windows", "64"),
            ("Linux", "linux", "64"),
            ("Darwin", "osx", "64"),
        ):
            with (
                self.subTest(system=system, architecture=bits),
                patch("platform.system", return_value=system),
                patch("platform.architecture", return_value=(bits + "bit", "")),
                # Native selection is simulated, but these are real host paths.
                # A Linux ':' classpath cannot encode a Windows C: drive.
                patch("minecraft_launcher_lib.command.get_classpath_separator", return_value=os.pathsep),
            ):
                for entry in library["downloads"]["classifiers"].values():
                    (self.cache / "libraries" / entry["path"]).unlink(missing_ok=True)
                entry = library["downloads"]["classifiers"][f"natives-{key}-{bits}"]
                native = self._put(entry, self.native_bytes)
                self.assertIsNone(self._missing())
                self.assertEqual(
                    self._classpath(self._command()), [str(native), str(self.jar)]
                )
                native.unlink()
                self.assertEqual(self._missing(), native.name)

    def test_rules_foreign_natives_and_explicit_empty_downloads_are_respected(self):
        linux_only = self._artifact_library("org.example:linux-only:1")
        linux_only["rules"] = [{"action": "allow", "os": {"name": "linux"}}]
        foreign = self._native_library("org.example:foreign-native:1")
        foreign["natives"] = {"linux": "natives-linux"}
        empty = {"name": "org.example:no-artifact:1", "downloads": {}}
        self._write_metadata([linux_only, foreign, empty])
        self.assertIsNone(self._missing())
        self.assertEqual(self._classpath(self._command()), [str(self.jar)])
        with patch("platform.system", return_value="Linux"):
            artifact = Path(linux_only["downloads"]["artifact"]["path"])
            self.assertEqual(self._missing(), artifact.name)
            self._put(linux_only["downloads"]["artifact"], self.artifact_bytes)
            native = Path(foreign["downloads"]["classifiers"]["natives-linux"]["path"])
            self.assertEqual(self._missing(), native.name)

    def test_inherited_loader_sizes_and_library_replacements_are_verified(self):
        old_library = self._artifact_library("org.example:loader-runtime:1")
        base_library = self._artifact_library("org.example:base-runtime:1")
        self._write_metadata([old_library, base_library])
        base_path = self._put(base_library["downloads"]["artifact"], self.artifact_bytes)
        replacement = self._artifact_library("org.example:loader-runtime:2")
        installed = "fixture-loader-1.7.10"
        game._atomic_json(
            game._manifest_path(self.cache, installed),
            {
                "id": installed,
                "inheritsFrom": self.version,
                "jar": self.version,
                "mainClass": "example.Loader",
                "libraries": [replacement],
            },
        )
        replacement_path = self._put(
            replacement["downloads"]["artifact"], self.artifact_bytes[:-1]
        )
        self.assertEqual(self._missing(installed), replacement_path.name)
        replacement_path.write_bytes(self.artifact_bytes)
        self.assertIsNone(self._missing(installed))
        command = self._command(installed)
        self.assertEqual(
            self._classpath(command), [str(replacement_path), str(base_path), str(self.jar)]
        )
        self.assertIn("example.Loader", command)
        self.assertFalse(
            (self.cache / "libraries" / old_library["downloads"]["artifact"]["path"]).exists()
        )

    def test_legacy_loader_metadata_still_requires_its_maven_library(self):
        for name in ("org.example:loader:1", "org.example:loader:1:runtime"):
            with self.subTest(coordinate=name):
                self._write_metadata([{"name": name, "url": "https://example.invalid/"}])
                path = self.cache / "libraries" / self._maven_relative(name)
                self.assertEqual(self._missing(), path.name)
                self._put({"path": self._maven_relative(name)}, self.artifact_bytes)
                self.assertIsNone(self._missing())
                self.assertEqual(self._classpath(self._command()), [str(path), str(self.jar)])

    def test_embedded_classpath_is_corrected_without_replacing_module_path(self):
        native_library = self._native_library("net.java.jinput:jinput-platform:2.0.5")
        module_library = self._artifact_library("org.example:modular-runtime:1")
        libraries = [native_library, module_library]
        native = self._put(
            native_library["downloads"]["classifiers"]["natives-windows"],
            self.native_bytes,
        )
        module = self._put(module_library["downloads"]["artifact"], self.artifact_bytes)
        del self.metadata["minecraftArguments"]
        self.metadata["arguments"] = {
            "jvm": [
                "-cp", "${classpath}", "-DlegacyClassPath=${classpath}",
                "--module-path", str(module), "-Dfixture.option=unchanged",
            ],
            "game": ["--username", "${auth_player_name}"],
        }
        self._write_metadata(libraries)
        command = self._command()
        expected = [str(native), str(module), str(self.jar)]
        self.assertEqual(self._classpath(command), expected)
        self.assertIn(
            "-DlegacyClassPath=" + mc.command.get_classpath_separator().join(expected),
            command,
        )
        self.assertEqual(command[command.index("--module-path") + 1], str(module))
        self.assertIn("-Dfixture.option=unchanged", command)
        self.assertEqual(command[command.index("--username") + 1], "Fixture")
        self.assertIsNone(self._missing())
        module.unlink()
        self.assertEqual(self._missing(), module.name)
        module.write_bytes(self.artifact_bytes)

        # A loader may name an additional module only in JVM arguments. It must
        # still be checked, even though it is absent from metadata.libraries.
        extra_module = self.cache / "loader-modules" / "additional-module.jar"
        jvm = self.metadata["arguments"]["jvm"]
        jvm[jvm.index("--module-path") + 1] = str(extra_module)
        self._write_metadata(libraries)
        command = self._command()
        self.assertEqual(command[command.index("--module-path") + 1], str(extra_module))
        self.assertEqual(self._classpath(command), expected)
        self.assertEqual(self._missing(), extra_module.name)
        extra_module.parent.mkdir(parents=True)
        extra_module.write_bytes(self.artifact_bytes)
        self.assertIsNone(self._missing())

    def test_loader_repair_skips_explicit_downloads_and_disallowed_legacy_libraries(self):
        native = self._native_library("org.lwjgl.lwjgl:lwjgl-platform:2.9.1")
        disallowed = {
            "name": "org.example:linux-loader:1",
            "url": "https://example.invalid/",
            "rules": [{"action": "allow", "os": {"name": "linux"}}],
        }
        self._write_metadata([native, disallowed])
        status = Mock()
        with patch.object(
            preparation.Download, "__call__", side_effect=AssertionError("unexpected repair")
        ) as download:
            game._repair_loader_files(self.cache, self.version, status)
        download.assert_not_called()
        status.assert_not_called()
        self.assertFalse((self.cache / "libraries").exists())

    def test_cancelling_native_repair_propagates_without_retry_or_install_stamp(self):
        library = self._native_library("net.java.jinput:jinput-platform:2.0.5")
        self._write_metadata([library])
        scope = lifecycle.CancellationScope()

        def cancelled(*_args, **_kwargs):
            scope.cancel()
            raise lifecycle.OperationCancelled("launcher closed during repair")

        with (
            lifecycle.bind(scope),
            patch.object(mc.install, "install_minecraft_version", side_effect=cancelled) as install,
            self.assertRaises(lifecycle.OperationCancelled),
        ):
            game.install(self.version, "vanilla", self.cache, self.instance)
        install.assert_called_once()
        self.assertFalse((self.instance / game.STAMP).exists())


if __name__ == "__main__":
    unittest.main()

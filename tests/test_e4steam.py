import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import e4steam as steam


def jar_bytes(mod, version, forge=False, provides=()):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as jar:
        if forge:
            jar.writestr(
                "META-INF/mods.toml",
                f'license="test"\n[[mods]]\nmodId="{mod}"\nversion="{version}"\n',
            )
        else:
            jar.writestr(
                "fabric.mod.json",
                json.dumps(dict(id=mod, version=version, provides=provides)),
            )
    return out.getvalue()


class SteamTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.world = self.root / "instances" / "first"
        self.world.mkdir(parents=True)
        self.bundled = self.root / "supplied"
        self.bundled.mkdir()
        self.cache = self.root / "shared"
        self.mods = self.world / "mods"
        self.mods.mkdir()
        self.assets = [
            self.component("core", "core.jar", jar_bytes("E4Steam", "0.3.6", True)),
            self.component(
                "addon",
                "Public.jar",
                jar_bytes("e4steam_public_servers", "0.1.0", True),
            ),
        ]
        self.network = {}

    def component(self, role, name, content, *, keep=True):
        if keep:
            (self.bundled / name).write_bytes(content)
        return steam.Asset(
            role,
            name,
            len(content),
            "sha256",
            hashlib.sha256(content).hexdigest(),
            ("https://unit.test/" + name,),
        )

    def setup_mocks(self):
        self.addCleanup(patch.stopall)
        patch.object(steam, "_BUNDLED", self.bundled).start()
        patch.object(steam, "_plan", return_value=tuple(self.assets)).start()
        return patch.object(
            steam,
            "urlopen",
            side_effect=lambda req, **kw: io.BytesIO(self.network[req.full_url]),
        ).start()

    def install(self, where=None, loader="forge"):
        return steam.ensure(where or self.world, "1.20.1", loader, cache_dir=self.cache)

    def test_release_matrix_has_real_versions_ranges_and_pins(self):
        for version, loaders in [
            ("1.17", {"fabric", "quilt"}),
            ("1.17.1", {"fabric", "quilt", "forge"}),
            ("1.18.2", {"fabric", "quilt", "forge"}),
            ("1.20.1", {"fabric", "quilt", "forge"}),
            ("1.20.2", {"fabric", "quilt", "forge", "neoforge"}),
            ("1.20.3", {"fabric", "quilt", "neoforge"}),
            ("1.21.11", {"fabric", "quilt", "neoforge"}),
            ("26.3", {"fabric", "quilt", "neoforge"}),
            ("26.1.2", {"fabric", "quilt", "neoforge"}),
            ("1.20.99", set()),
            ("26.4", set()),
            ("1.16.5", set()),
            ("26w02a", set()),
        ]:
            with self.subTest(version=version):
                self.assertEqual(
                    {
                        ld
                        for ld in ("fabric", "quilt", "forge", "neoforge", "vanilla")
                        if steam.support(version, ld)
                    },
                    loaders,
                )
        for version in steam._CATALOG["fabric_api"]:
            for ld in ("fabric", "quilt", "forge", "neoforge"):
                files = steam._plan(version, ld)
                if files:
                    self.assertEqual(len(files), 3 if ld in ("fabric", "quilt") else 2)
                    for f in files:
                        self.assertGreater(f.size, 0)
                        self.assertEqual(
                            len(f.digest), hashlib.new(f.algorithm).digest_size * 2
                        )
                        self.assertTrue(all(u.startswith("https://") for u in f.urls))
        self.assertEqual(
            steam._plan("1.20.1", "forge")[0].digest,
            "afab51e869a097af43964e73d5c47c2e95f035a3c402be3683a13fd9d49cdfc5",
        )
        self.assertEqual(
            steam._plan("1.20.1", "fabric")[2].filename, "fabric-api-0.92.12+1.20.1.jar"
        )
        self.assertNotIn(
            "fabric-api-0.92.12+1.20.1.jar", steam.protected_names("1.20.1", "fabric")
        )

    def test_bundle_rename_and_repeated_play_offline(self):
        net = self.setup_mocks()
        files = self.install()
        self.assertTrue(steam.installed(self.world, "1.20.1", "forge"))
        renamed = self.mods / "renamed.jar"
        files[0].rename(renamed)
        self.assertEqual(self.install()[0], renamed)
        self.assertEqual(len(list(self.mods.iterdir())), 2)
        net.assert_not_called()

    def test_one_download_shared_and_damage_repaired_via_cache(self):
        file = self.bundled / self.assets[0].filename
        data = file.read_bytes()
        file.unlink()
        self.network[self.assets[0].urls[0]] = data
        net = self.setup_mocks()
        fresh = self.install()[0]
        fresh.unlink()
        self.install(self.world.parent / "second")
        self.install()
        self.assertEqual(net.call_count, 1)
        self.assertEqual(fresh.read_bytes(), data)
        cache = self.cache / (self.assets[0].digest + ".jar")
        cache.write_bytes(b"0" * len(data))
        fresh.unlink()
        self.install()
        self.assertEqual(net.call_count, 2)
        self.assertEqual(cache.read_bytes(), data)

    def test_corrupt_download_leaves_no_partial_component(self):
        file = self.bundled / self.assets[1].filename
        data = file.read_bytes()
        file.unlink()
        self.network[self.assets[1].urls[0]] = b"x" * len(data)
        self.setup_mocks()
        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            self.install()
        self.assertFalse(any(self.mods.iterdir()))
        self.assertFalse(any(self.cache.iterdir()))
        self.network[self.assets[1].urls[0]] = data
        self.assertEqual(len(self.install()), 2)

    def test_existing_conflicting_renamed_or_mismatched_jar_is_preserved(self):
        self.setup_mocks()
        old = self.mods / "renamed.jar"
        old.write_bytes(jar_bytes("e4steam", "0.2.0", True))
        with self.assertRaisesRegex(ValueError, old.name):
            self.install()
        self.assertEqual(list(self.mods.iterdir()), [old])
        self.assertFalse(steam.installed(self.world, "1.20.1", "forge"))
        old.unlink()
        corrupt = self.mods / self.assets[0].filename.upper()
        corrupt.write_bytes(b"invalid")
        with self.assertRaisesRegex(ValueError, "уже есть"):
            self.install()
        self.assertEqual(corrupt.read_bytes(), b"invalid")

    def test_conflict_on_commit_rolls_back_only_new_files(self):
        self.setup_mocks()
        third = self.mods / "worldgen.jar"
        third.write_bytes(b"other")
        publish = steam._publish

        def conflict(source, target):
            if target.name == self.assets[1].filename:
                target.write_bytes(b"imported concurrently")
                raise FileExistsError(target)
            publish(source, target)

        with (
            patch.object(steam, "_publish", side_effect=conflict),
            self.assertRaises(FileExistsError),
        ):
            self.install()
        self.assertEqual(
            sorted(p.name for p in self.mods.iterdir()),
            [self.assets[1].filename, "worldgen.jar"],
        )
        self.assertEqual(
            (self.mods / self.assets[1].filename).read_bytes(), b"imported concurrently"
        )
        self.assertEqual(third.read_bytes(), b"other")

    def test_existing_compatible_api_does_not_duplicate_players_mod(self):
        self.assets.append(
            self.component(
                "fabric-api",
                "pinned.jar",
                jar_bytes("fabric-api", "0.92+1.20.1", provides=["fabric"]),
            )
        )
        old = self.mods / "players-old-api.jar"
        old.write_bytes(jar_bytes("fabric-api", "0.91+1.20.1", provides=["fabric"]))
        self.setup_mocks()
        self.assertEqual(self.install(loader="fabric")[-1], old)
        self.assertFalse((self.mods / "pinned.jar").exists())
        other = self.mods / "duplicate.jar"
        other.write_bytes(jar_bytes("fabric-api", "0.88+1.20.1", provides=["fabric"]))
        with self.assertRaisesRegex(ValueError, "две версии"):
            self.install(loader="fabric")

    def test_refuses_external_mods_folder(self):
        self.setup_mocks()
        target = self.root / "outside"
        target.mkdir()
        self.mods.rmdir()
        try:
            self.mods.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic link creation unavailable")
        with self.assertRaisesRegex(ValueError, "Папка модов"):
            self.install()
        self.assertFalse(any(target.iterdir()))

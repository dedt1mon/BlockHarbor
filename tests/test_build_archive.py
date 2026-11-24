import io
import json
import os
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import build_archive as archive
from launcher.store import Store


def sample_zip(extra=(), **meta):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as zipped:
        zipped.writestr(
            archive.MANIFEST,
            json.dumps(
                {
                    "format": "BlockHarbor",
                    "formatVersion": 1,
                    "name": "Архипелаг",
                    "minecraft": "1.20.1",
                    "loader": "fabric",
                    **meta,
                }
            ),
        )
        for name, data in extra:
            if isinstance(name, str) and "\\" in name:
                # ZipInfo(name) normalizes Windows backslashes before writing.
                # Populate the raw member name after construction so this is
                # genuinely an unsafe ZIP on every test platform.
                raw = zipfile.ZipInfo()
                raw.filename = raw.orig_filename = name
                name = raw
            zipped.writestr(name, data)
    result.seek(0)
    return result


class BuildArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name)
        self.pid = self.store.create("Остров друзей", "1.20.1", "fabric")
        self.folder = self.store.folder(self.pid)

    def test_portable_mods_packs_config_and_pin_without_private_files(self):
        root = self.folder
        sample = {
            "mods/ручной.jar": b"jar",
            "mods/off.jar.disabled": b"off",
            "shaderpacks/shader.zip": b"shader",
            "resourcepacks/grass.zip": b"zip",
            "resourcepacks/Unpacked/pack.mcmeta": b"{}",
            "config/renderer/settings.toml": b"fancy = true",
            "defaultconfigs/ore.cfg": b"ores=true",
        }
        private = {
            "config/auth/accounts.json": b"secret",
            "config/pretty.toml": b'apiKey="secret"',
            "config/skinss/skin/player.png": b"photo",
            "options.txt": b'fov:80\nresourcePacks:["file/grass.zip"]\nlastServer:private\n',
            "logs/latest.log": b"log",
            "servers.dat": b"private",
            "saves/Island/level.dat": b"world",
            "saves/Island/session.lock": b"lock",
            "config/skinss/settings.json": b'{"selectedSkin":"friend.png"}',
            archive.STAMP: json.dumps(
                {
                    "id": "fabric-loader-0.16.0-1.20.1",
                    "version": "1.20.1",
                    "loader": "fabric",
                    "cache": "/private/user",
                    "loader_version": "0.16.0",
                }
            ).encode(),
        }
        for path, content in {**sample, **private}.items():
            dst = root / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)
        for worlds in (False, True):
            with self.subTest(worlds=worlds):
                out = io.BytesIO()
                name = archive.export_build(self.store, self.pid, out, worlds)
                self.assertTrue(name.endswith(".zip"))
                with zipfile.ZipFile(out) as z:
                    self.assertEqual(
                        set(z.namelist()),
                        set(sample)
                        | {archive.MANIFEST}
                        | ({"saves/Island/level.dat"} if worlds else set()),
                    )
                    self.assertNotIn(b"/private", z.read(archive.MANIFEST))
                r = archive.import_build(self.store, out)
                folder = self.store.folder(r["id"])
                self.assertNotEqual(r["id"], self.pid)
                self.assertEqual(self.store.get("active"), r["id"])
                self.assertNotEqual(r["name"], self.store.profile(self.pid)["name"])
                for path, data in sample.items():
                    self.assertEqual((folder / path).read_bytes(), data)
                self.assertEqual((folder / "saves/Island/level.dat").exists(), worlds)
                self.assertEqual(
                    json.loads((folder / archive.STAMP).read_text())["loader_version"],
                    "0.16.0",
                )
                self.assertNotIn("private", (folder / "options.txt").read_text())
                for path in private.keys() - {
                    archive.STAMP,
                    "options.txt",
                    "saves/Island/level.dat",
                }:
                    self.assertFalse((folder / path).exists())

    def test_invalid_paths_duplicates_symlinks_and_limits_roll_back(self):
        link = zipfile.ZipInfo("mods/link.jar")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        before = self.store.all()
        on_disk = set(self.store.instances.iterdir())
        old = self.store.get("active")
        cases = [
            [("mods/../outside.jar", b"!")],
            [("mods\\file.jar", b"!")],
            [("config/CON.txt", b"x")],
            [("mods/test.jar", b"1"), ("mods/TEST.jar", b"2")],
            [("config/foo.ini", b"bar"), ("config/foo.ini/deeper.conf", b"bad")],
            [("config/New/a.cfg", b"1"), ("config/new/b.cfg", b"2")],
            [(link, b"other.jar")],
            [
                ("mods/valid.jar", b"1"),
                ("config/common.properties", b"password=private"),
            ],
        ]
        for records in cases:
            with self.subTest(records=records), warnings.catch_warnings():
                with self.assertRaises(ValueError):
                    archive.import_build(self.store, sample_zip(records))
        with (
            patch.object(archive, "MAX_TOTAL", 4),
            self.assertRaisesRegex(ValueError, "лимиты"),
        ):
            archive.import_build(self.store, sample_zip([("mods/a.jar", b"12345")]))
        with self.assertRaises(ValueError):
            archive.import_build(self.store, sample_zip([], loaderVersion="../../"))
        self.assertEqual(self.store.all(), before)
        self.assertEqual(set(self.store.instances.iterdir()), on_disk)
        self.assertEqual(self.store.get("active"), old)

    def test_broken_crc_after_manifest_does_not_publish(self):
        data = sample_zip(
            [("mods/first.jar", b"first"), ("mods/second.jar", b"damaged-here")]
        ).getvalue()
        before = self.store.all()
        old = self.store.get("active")
        with self.assertRaises(ValueError):
            archive.import_build(
                self.store,
                io.BytesIO(data.replace(b"damaged-here", b"damaged-XXXX", 1)),
            )
        self.assertEqual(self.store.all(), before)
        self.assertEqual(self.store.get("active"), old)
        self.assertEqual(len(list(self.store.instances.iterdir())), len(before))

    def test_backslash_member_is_present_on_wire_and_rejected(self):
        payload = sample_zip([("mods\\file.jar", b"!")])
        self.assertEqual(payload.getvalue().count(b"mods\\file.jar"), 2)
        with zipfile.ZipFile(payload) as zipped:
            self.assertEqual(zipped.infolist()[1].orig_filename, "mods\\file.jar")
        with self.assertRaises(ValueError):
            archive.import_build(self.store, payload)

    def test_download_capability_requires_correct_cookie_and_can_only_be_used_once(
        self,
    ):
        files = archive.Downloads()
        f = io.BytesIO(b"zip")
        ticket, cookie = files.issue(f, "test.zip")
        with self.assertRaises(ValueError):
            files.take(ticket, "wrong")
        self.assertEqual(files.take(ticket, cookie), (f, "test.zip"))
        with self.assertRaises(ValueError):
            files.take(ticket, cookie)
        f.close()

    def test_privacy_links_in_instance_are_not_exported(self):
        descriptor, name = tempfile.mkstemp(dir=self.store.root)
        os.close(descriptor)
        private = Path(name)
        private.write_text("private")
        try:
            (self.folder / "mods/normal.jar").symlink_to(private)
            (self.folder / "config").symlink_to(private.parent, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows denied symbolic-link creation (ERROR_PRIVILEGE_NOT_HELD)")
            raise
        f = io.BytesIO()
        archive.export_build(self.store, self.pid, f)
        self.assertEqual(zipfile.ZipFile(f).namelist(), [archive.MANIFEST])

import hashlib
import io
import json
import os
import struct
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from http.client import HTTPConnection, IncompleteRead
from pathlib import Path
from unittest.mock import patch

from launcher import content_icons, modrinth
from launcher.store import Store, verify_pack
from main import App, Handler, Server
from PIL import Image


def picture(color="green", size=(48, 64)):
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, "PNG")
    return output.getvalue()


def archive(path, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, data in entries.items():
            target.writestr(name, data)


class ContentIconsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root)
        self.pid = self.store.create("Icons", "1.20.1", "fabric")
        self.folder = self.store.content_folder(self.pid, "mods")

    def test_all_loaders_and_pack_icons_use_the_same_bounded_raster_cache(self):
        images = {
            "fabric.mod.json": {"icon": {"16": "missing.png", "96": "assets/icon.png"}},
            "quilt.mod.json": {
                "quilt_loader": {"metadata": {"icon": "assets/icon.png"}}
            },
            "META-INF/mods.toml": '[[mods]]\nmodId="test"\nlogoFile="assets/icon.png"\n',
            "META-INF/neoforge.mods.toml": '[[mods]]\nmodId="test"\nlogoFile="assets/icon.png"\n',
        }
        urls = []
        for index, (descriptor, data) in enumerate(images.items()):
            file = self.folder / (str(index) + ".jar")
            archive(
                file,
                {
                    descriptor: json.dumps(data) if isinstance(data, dict) else data,
                    "assets/icon.png": picture(),
                },
            )
            urls.append(self.store.icons.icon(file))
        for kind in ("shaderpacks", "resourcepacks"):
            file = self.store.content_folder(self.pid, kind) / "pack.zip"
            archive(file, {"pack.png": picture()})
            urls.append(self.store.icons.icon(file))
        self.assertTrue(all(urls))
        self.assertEqual(len(set(urls)), 1)
        digest = urls[0].split("/")[-1].removesuffix(".png")
        data = self.store.icons.read(digest)
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (96, 96))
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.info, {})
        self.assertEqual(self.store.content(self.pid)["mods"][0]["icon"], urls[0])

    def test_old_and_manual_files_work_offline_and_unchanged_polls_do_not_read_zip(
        self,
    ):
        existing = self.folder / "old-manual.jar"
        blank = self.folder / "no-icon.jar"
        archive(
            existing,
            {
                "fabric.mod.json": '{"icon":"assets/icon.png"}',
                "assets/icon.png": picture(),
            },
        )
        archive(blank, {"fabric.mod.json": '{"id":"blank"}'})
        urls = [self.store.icons.icon(existing), self.store.icons.icon(blank)]
        with patch.object(
            content_icons,
            "archive_thumbnail",
            side_effect=AssertionError("archive opened again"),
        ):
            for _ in range(3):
                self.assertEqual(
                    [r["icon"] for r in self.store.content(self.pid)["mods"]],
                    [urls[1], urls[0]],
                )
            reopened = Store(self.root)
            self.assertEqual(reopened.icons.icon(existing), urls[0])
            self.assertEqual(reopened.icons.icon(blank), "")
        archive(existing, {"pack.png": picture("red")})
        self.assertNotEqual(self.store.icons.icon(existing), urls[0])

    def test_archive_paths_symlinks_zip_bombs_and_large_images_fall_back(self):
        outside = self.root / "private.png"
        outside.write_bytes(picture())
        for path in (
            "../private.png",
            "/private.png",
            "C:/private.png",
            "folder/../private.png",
            "folder\\private.png",
        ):
            with self.subTest(path=path):
                file = self.folder / "bad.jar"
                archive(
                    file,
                    {"fabric.mod.json": json.dumps({"icon": path}), path: picture()},
                )
                self.assertEqual(self.store.icons.icon(file), "")
        file = self.folder / "symlink.jar"
        with zipfile.ZipFile(file, "w") as jar:
            jar.writestr("fabric.mod.json", '{"icon":"logo.png"}')
            link = zipfile.ZipInfo("logo.png")
            link.external_attr = 0o120777 << 16
            jar.writestr(link, picture())
        self.assertEqual(self.store.icons.icon(file), "")
        file = self.folder / "bomb.jar"
        archive(file, {"pack.png": b"x" * (content_icons.MAX_IMAGE + 1)})
        with patch.object(
            content_icons,
            "thumbnail",
            side_effect=AssertionError("expanded oversized image"),
        ):
            self.assertEqual(self.store.icons.icon(file), "")
        archive(file, {"pack.png": picture(size=(4097, 1))})
        self.assertEqual(self.store.icons.icon(file), "")
        with zipfile.ZipFile(file, "w") as jar:
            for index in range(8193):
                jar.writestr(str(index), b"")
        with patch.object(
            content_icons.zipfile,
            "ZipFile",
            side_effect=AssertionError("allocated oversized directory"),
        ):
            self.assertEqual(self.store.icons.icon(file), "")
        self.assertEqual(outside.read_bytes(), picture())

    def test_modrinth_fallback_is_cached_at_install_and_its_failure_is_optional(self):
        file = self.folder / "project.jar"
        archive(file, {"fabric.mod.json": '{"id":"test","version":"1"}'})
        release = modrinth.Package(
            "v1",
            "project",
            "mods",
            "1",
            "Test",
            "release",
            "2026",
            file.name,
            file.stat().st_size,
            hashlib.sha512(file.read_bytes()).hexdigest(),
            "https://cdn.modrinth.com/data/project/versions/v1/project.jar",
            (),
            "https://cdn.modrinth.com/data/project/logo.png",
        )
        with patch.object(
            modrinth, "_open", return_value=io.BytesIO(picture())
        ) as request:
            modrinth._save_project_icons(
                self.store, self.pid, [release], lambda message: None
            )
            self.assertEqual(request.call_args.kwargs["timeout"], 3)
        url = self.store.icons.icon(file)
        self.assertTrue(url)
        with patch.object(
            modrinth, "_open", side_effect=AssertionError("network on poll")
        ):
            self.assertEqual(Store(self.root).icons.icon(file), url)
            modrinth._save_project_icons(
                self.store, self.pid, [release], lambda message: None
            )
        second = self.folder / "second.jar"
        second.write_bytes(file.read_bytes())
        failed = replace(release, filename=second.name, sha512="f" * 128)
        with (
            patch.object(modrinth, "_installed", return_value=second),
            patch.object(modrinth, "_open", side_effect=IncompleteRead(b"partial")),
        ):
            modrinth._save_project_icons(
                self.store, self.pid, [failed], lambda message: None
            )
        self.assertEqual(self.store.icons.icon(second), "")
        for data in (b"", b"not an image", picture(size=(4097, 1))):
            with (
                self.subTest(data_length=len(data)),
                patch.object(modrinth, "_installed", return_value=second),
                patch.object(modrinth, "_open", return_value=io.BytesIO(data)),
            ):
                modrinth._save_project_icons(
                    self.store, self.pid, [failed], lambda message: None
                )
                self.assertEqual(self.store.icons.icon(second), "")

    def test_forged_directory_counts_and_zip64_are_rejected_before_zipfile(self):
        file = self.folder / "forged.jar"
        archive(file, {"first": b"", "second": b""})
        original = file.read_bytes()
        end = len(original) - 22
        forged = bytearray(original)
        struct.pack_into("<HH", forged, end + 8, 1, 1)
        with patch.object(
            content_icons.zipfile,
            "ZipFile",
            side_effect=AssertionError("allocated an untrusted directory"),
        ):
            file.write_bytes(forged)
            self.assertEqual(self.store.icons.icon(file), "")
            # zipfile honors ZIP64 even if the classic EOCD uses small values.
            _, _, _, _, entries, size, offset, _ = struct.unpack(
                "<4s4H2IH", original[-22:]
            )
            extended = struct.pack(
                "<4sQ2H2I4Q",
                b"PK\x06\x06",
                44,
                45,
                45,
                0,
                0,
                entries,
                entries,
                size,
                offset,
            )
            locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
            struct.pack_into("<I", forged, end + 12, 1)
            file.write_bytes(original[:end] + extended + locator + forged[end:])
            self.assertEqual(self.store.icons.icon(file), "")

    def test_corrupt_deflate_artwork_does_not_break_the_state(self):
        file = self.store.content_folder(self.pid, "resourcepacks") / "broken.zip"
        archive(
            file,
            {
                "pack.mcmeta": '{"pack":{"pack_format":15,"description":"Test"}}',
                "pack.png": picture(),
            },
        )
        with zipfile.ZipFile(file) as pack:
            offset = pack.getinfo("pack.png").header_offset
        data = bytearray(file.read_bytes())
        name, extra = struct.unpack_from("<HH", data, offset + 26)
        data[offset + 30 + name + extra] = 0xFF  # invalid DEFLATE block type
        file.write_bytes(data)
        verify_pack(file, "resourcepacks", "fabric")
        state = App(self.root).state()
        self.assertEqual(
            state["contents"]["resourcepacks"],
            [{"name": "broken.zip", "size": len(data), "icon": ""}],
        )

    def test_endpoint_serves_only_cached_png_with_local_origin_and_rejects_escape(self):
        file = self.folder / "icon.jar"
        archive(file, {"pack.png": picture()})
        url = self.store.icons.icon(file)
        app = App(self.root)
        server = Server(("127.0.0.1", 0), Handler)
        server.app = app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", url)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/png")
            self.assertIn("immutable", response.getheader("Cache-Control"))
            self.assertTrue(response.read().startswith(b"\x89PNG"))
            connection.request(
                "GET", url, headers={"Origin": "https://example.invalid"}
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            for path in (
                "/api/content-icon/../../settings.sqlite3.png",
                "/api/content-icon/" + "a" * 64 + ".png",
            ):
                connection.request("GET", path)
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_corrupt_or_redirected_cache_cannot_serve_arbitrary_bytes(self):
        file = self.folder / "pack.jar"
        archive(file, {"pack.png": picture()})
        url = self.store.icons.icon(file)
        digest = url.split("/")[-1].removesuffix(".png")
        path = self.store.icons.directory / (digest + ".png")
        path.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            self.store.icons.read(digest)
        reopened = Store(self.root)
        self.assertEqual(reopened.icons.icon(file), url)
        self.assertTrue(reopened.icons.read(digest).startswith(b"\x89PNG"))
        path.unlink()
        target = self.root / "secret"
        target.write_bytes(picture())
        if os.name != "nt":
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                self.store.icons.read(digest)
        with self.assertRaises(ValueError):
            self.store.icons.read("../secret")

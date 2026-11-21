import hashlib
import io
import json
import time
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from launcher import build_archive, community_catalog as catalog
from launcher.store import Store


class Registry:
    """Simulate the public API independently of both clients' private Stores."""
    origin = "https://catalog.example"
    public_id = "BH-A123456789BC"

    def __init__(self):
        self.uploads = 0
        self.parts = {}
        self.meta = self.listing = None
        self.fail_after_commit = False
        self.data = b""

    def json(self, method, path, *, token=None, payload=None, block=None):
        if (method, path) == ("POST", "uploads"):
            self.uploads += 1
            self.meta = payload.copy()
            return {"upload": "a" * 32, "token": "b" * 64, "chunkSize": catalog.CHUNK}
        if path.startswith("uploads/"):
            assert token == "b" * 64
            if method == "PUT":
                self.parts[int(path.split("/")[-1])] = block
                return {}
            if not self.listing:
                self.data = b"".join(self.parts[n] for n in sorted(self.parts))
                assert hashlib.sha256(self.data).hexdigest() == self.meta["sha256"]
                self.listing = {"id": self.public_id, **self.meta, "expiresAt": int(time.time()) + 3600}
            if self.fail_after_commit:
                self.fail_after_commit = False
                raise catalog.CatalogError(0, "Потерян ответ")
            return self.listing.copy()
        if path.startswith("builds?"):
            self.filters = parse_qs(urlsplit(path).query)
            return {"hits": [self.listing] if self.listing else [], "total": int(bool(self.listing))}
        if path == "builds/" + self.public_id and self.listing:
            if method == "DELETE":
                assert token == "b" * 64
                self.listing = None
                return {}
            return self.listing.copy()
        raise catalog.CatalogError(404, "Нет сборки")

    @contextmanager
    def request(self, method, path):
        assert (method, path) == ("GET", "builds/" + self.public_id + "/archive")
        with io.BytesIO(self.data) as source:
            source.headers = {"Content-Length": str(len(self.data))}
            yield source

    def replace_archive(self, path=None, contents=b"", **changes):
        data = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.data)) as old, zipfile.ZipFile(data, "w") as new:
            for name in old.namelist():
                value = old.read(name)
                if name == build_archive.MANIFEST:
                    value = json.dumps({**json.loads(value), **changes}).encode()
                new.writestr(name, value)
            if path:
                new.writestr(path, contents)
        self.data = data.getvalue()
        self.listing.update(size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())


class CommunityCatalogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.author = Store(Path(tmp.name) / "author")
        self.friend = Store(Path(tmp.name) / "friend")
        self.pid = self.author.create("Личное имя", "1.20.1", "fabric")
        self.folder = self.author.folder(self.pid)
        self.server = Registry()
        self.publisher = catalog.CommunityCatalog(self.author, self.server)
        self.reader = catalog.CommunityCatalog(self.friend, self.server)
        self.status = lambda message: None

    def publish(self):
        return self.publisher.publish(self.pid, "Сборка друзей", "adventure", self.status)

    def install(self):
        return self.reader.install(self.server.public_id.lower(), True, self.status)

    def test_publication_install_separate_devices_privacy_and_retraction(self):
        files = {
            "mods/test.jar": b"mod bytes", "mods/off.jar.disabled": b"disabled",
            "resourcepacks/forest.zip": b"resources", "shaderpacks/sun.zip": b"shader",
            "resourcepacks/orphan.txt": b"personal note", "saves/home/level.dat": b"world",
            "logs/latest.log": b"logs", "config/game.json": b"config", "options.txt":
            b'resourcePacks:["vanilla","file/forest.zip","file/private.zip"]\nfov:80\nlastServer:private\n',
            build_archive.STAMP: b'{"version":"1.20.1","loader":"fabric","loader_version":"0.16.10"}',
        }
        for relative, data in files.items():
            target = self.folder / relative
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_bytes(data)
        with patch.object(catalog, "CHUNK", 128):
            self.publish()
        self.assertGreater(len(self.server.parts), 1)
        hit = self.reader.search(dict(q="Сборка", category="adventure", version="1.20.1", loader="fabric"))["hits"][0]
        self.assertEqual(self.server.filters["q"], ["Сборка"])
        self.assertEqual(hit["id"], self.server.public_id)
        self.assertNotIn("token", hit)
        publication = self.publisher.publications(self.pid)
        self.assertEqual(publication["publications"][0]["build"], hit["id"])
        self.assertNotIn("token", str(publication))
        with zipfile.ZipFile(io.BytesIO(self.server.data)) as archive:
            self.assertEqual(archive.namelist()[0], build_archive.MANIFEST)
            self.assertEqual(len(archive.namelist()), 5)
            self.assertNotIn(b"private", archive.read(build_archive.MANIFEST))
        self.assertFalse(self.friend.all())
        result = self.install()
        dest = self.friend.folder(result["id"])
        self.assertEqual(result["name"], "Сборка друзей")
        for path in list(files)[:4]:
            self.assertEqual((dest / path).read_bytes(), files[path])
        self.assertEqual(json.loads((dest / build_archive.STAMP).read_bytes())["loader_version"], "0.16.10")
        self.assertEqual((dest / "options.txt").read_text().strip(),'resourcePacks:["vanilla","file/forest.zip"]')
        with self.assertRaises(ValueError):
            self.reader.unpublish(result["id"], hit["id"])
        self.publisher.unpublish(self.pid, hit["id"])
        self.assertFalse(self.publisher.publications(self.pid)["publications"])
        self.assertFalse(self.reader.search({})["hits"])
        self.assertTrue(dest.exists())

    def test_legacy_publication_recovers_expiry_without_losing_token(self):
        published = self.publish()
        self.assertTrue(published["expiresAt"])
        with self.author.connection() as db:
            cached = {key: value for key, value in published.items() if key != "expiresAt"}
            db.execute("UPDATE community_uploads SET listing=? WHERE pid=?",
                       (json.dumps(cached), self.pid))
        records = self.publisher.publications(self.pid)["publications"]
        self.assertEqual(records[0]["id"], self.server.public_id)
        self.assertEqual(records[0]["expiresAt"], published["expiresAt"])
        with self.author.connection() as db:
            record = db.execute("SELECT token,listing FROM community_uploads WHERE pid=?", (self.pid,)).fetchone()
            self.assertEqual(record["token"], "b" * 64)
            self.assertIn("expiresAt", json.loads(record["listing"]))

    def test_legacy_server_keeps_existing_publication_without_duplicate(self):
        published = self.publish()
        old = {key: val for key, val in published.items() if key != "expiresAt"}
        with self.author.connection() as db:
            db.execute("UPDATE community_uploads SET listing=? WHERE pid=?",
                       (json.dumps(old), self.pid))
        original = self.server.json
        def legacy_server(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if isinstance(result, dict):
                return {key: val for key, val in result.items() if key != "expiresAt"}
            return result
        with patch.object(self.server, "json", side_effect=legacy_server):
            items = self.publisher.publications(self.pid)["publications"]
            self.assertEqual(items[0]["id"], self.server.public_id)
            self.assertTrue(items[0]["statusUnverified"])
            returned = self.publish()
        self.assertEqual(returned["id"], self.server.public_id)
        self.assertEqual(self.server.uploads, 1)
        with self.author.connection() as db:
            record = db.execute("SELECT token FROM community_uploads WHERE pid=?", (self.pid,)).fetchone()
        self.assertEqual(record["token"], "b" * 64)

    def test_legacy_publication_offline_retains_revoke_token(self):
        published = self.publish()
        cached = {key: value for key, value in published.items() if key != "expiresAt"}
        with self.author.connection() as db:
            db.execute("UPDATE community_uploads SET listing=? WHERE pid=?",
                       (json.dumps(cached), self.pid))
        original = self.server.json
        def disconnected(method, path, **kwargs):
            if method == "GET":
                raise catalog.CatalogError(0, "offline")
            return original(method, path, **kwargs)
        with patch.object(self.server, "json", side_effect=disconnected):
            visible = self.publisher.publications(self.pid)["publications"]
        self.assertTrue(visible[0]["statusUnverified"])
        with self.author.connection() as db:
            token = db.execute("SELECT token FROM community_uploads WHERE pid=?", (self.pid,)).fetchone()
        self.assertEqual(token["token"], "b" * 64)

    def test_expired_publication_is_removed_locally_without_touching_build(self):
        published = self.publish()
        self.assertTrue(self.publisher.publications(self.pid)["publications"])
        expired = {**published, "expiresAt": int(time.time()) - 1}
        with self.author.connection() as db:
            db.execute("UPDATE community_uploads SET listing=? WHERE pid=?",
                       (json.dumps(expired), self.pid))
        self.assertEqual(self.publisher.publications(self.pid)["publications"], [])
        self.assertTrue(self.folder.is_dir())

    def test_expired_idempotent_cache_never_prevents_new_publication(self):
        self.publish()
        self.assertEqual(self.server.uploads, 1)
        with self.author.connection() as db:
            row = db.execute("SELECT listing FROM community_uploads WHERE pid=?", (self.pid,)).fetchone()
            expired = {**json.loads(row["listing"]), "expiresAt": int(time.time()) - 1}
            db.execute("UPDATE community_uploads SET listing=? WHERE pid=?",
                       (json.dumps(expired), self.pid))
        self.server.listing = None
        again = self.publish()
        self.assertEqual(self.server.uploads, 2)
        self.assertGreater(again["expiresAt"], time.time())

    def test_lost_completion_is_recovered_after_restart(self):
        self.server.fail_after_commit = True
        with self.assertRaises(catalog.CatalogError):
            self.publish()
        reopened = catalog.CommunityCatalog(self.author, self.server)
        listing = reopened.publish(self.pid, "Сборка друзей", "adventure", self.status)
        self.assertEqual(listing["id"], self.server.public_id)
        self.assertEqual(self.server.uploads, 1)
        self.assertEqual(reopened.publications(self.pid)["build"], listing["id"])
        self.assertEqual(reopened.publish(self.pid, "Сборка друзей", "adventure", self.status), listing)
        self.assertEqual(self.server.uploads, 1)

    def test_real_folder_packs_and_opt_in_configs(self):
        files = {
            "resourcepacks/Forest/pack.mcmeta": b'{"pack":{"pack_format":15,"description":"Test"}}',
            "resourcepacks/Forest/assets/tree.png": b"tree",
            "shaderpacks/Summer/shaders/program/vectors.glsl": b"shader",
            "shaderpacks/Summer/description.txt": b"summer",
            "resourcepacks/notes/private.txt": b"not a pack",
            "shaderpacks/incomplete/info.txt": b"not a shader",
            "config/mymod/client.toml": b"quality = 3",
            "defaultconfigs/server.properties": b"pvp=true",
            "config/token.json": b"secret",
            "config/other.toml": b"password = 'secret'",
            "options.txt": b'resourcePacks:["file/Forest"]\nfov:75\n',
        }
        for relative, data in files.items():
            path = self.folder / relative
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(data)
        link = self.folder / "resourcepacks/Forest/linked.txt"
        try:
            link.symlink_to(self.folder / "config/token.json")
        except OSError:
            pass  # Windows may require a privilege to create symlinks.
        for enabled in (False, True):
            with self.subTest(include_configs=enabled):
                self.publisher.publish(self.pid, "Проверка папок", "magic", self.status, include_configs=enabled)
                dest = self.friend.folder(self.install()["id"])
                for relative in list(files)[:4]:
                    self.assertEqual((dest / relative).read_bytes(), files[relative])
                self.assertEqual((dest / "options.txt").read_text().strip(), 'resourcePacks:["file/Forest"]')
                self.assertFalse((dest / "resourcepacks/Forest/linked.txt").exists())
                for relative in list(files)[4:-1]:
                    allowed = enabled and relative in ("config/mymod/client.toml", "defaultconfigs/server.properties")
                    self.assertEqual((dest / relative).exists(), allowed, relative)
                with zipfile.ZipFile(io.BytesIO(self.server.data)) as z:
                    self.assertIs(json.loads(z.read(build_archive.MANIFEST)).get("publicConfigs", False), enabled)
                self.publisher.unpublish(self.pid, self.server.public_id)

    def test_config_publication_import_still_rejects_private_data(self):
        self.publish()
        self.server.replace_archive("config/example.cfg", b"api_key = hidden", publicConfigs=True)
        with self.assertRaisesRegex(ValueError, "личными"):
            self.install()
        self.assertFalse(self.friend.all())

    def test_download_integrity_metadata_and_scope_precede_profile_creation(self):
        self.publish()
        clean = self.server.data
        original = self.server.listing.copy()
        cases = [
            {"path": "config/anything.toml"}, {"path": "saves/Island/level.dat"},
            {"path": "shaderpacks/notes.txt"}, {"path": "mods/../x.jar"},
            {"minecraft": "1.21.1"}, {"loader": "forge"}, {"name": "Чужое имя"}, {"worlds": True},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.server.data = clean
                self.server.listing = original.copy()
                self.server.replace_archive(**kwargs)
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse(self.friend.all())
                self.assertFalse(list(self.friend.instances.iterdir()))
        self.server.data = clean
        self.server.listing = {**original, "sha256": "f" * 64}
        with self.assertRaisesRegex(ValueError, "контрольная сумма"):
            self.install()
        self.assertFalse(self.friend.all())

    def test_install_creates_new_named_profile_and_accepts_steam_notice(self):
        self.pid = self.author.create("Ваниль", "1.20.1", "vanilla")
        self.publish()
        self.friend.create("Сборка друзей", "1.20.1", "vanilla")
        with self.assertRaisesRegex(ValueError, "Steam"):
            self.reader.install(self.server.public_id, False, self.status)
        imported = self.install()
        self.assertEqual(imported["name"], "Сборка друзей (2)")
        self.assertEqual(self.friend.get("steamNotice:" + imported["id"]), "1.20.1|vanilla")


    def test_unreachable_primary_uses_verified_public_mirror_for_search_and_install(self):
        listing = self.publish()
        archive = self.server.data

        class BlockedPrimary:
            origin = "https://catalog.blocked.example"

            def json(self, method, path, **kwargs):
                raise catalog.CatalogError(403, "Site unavailable without VPN")

            def request(self, method, path):
                raise catalog.CatalogError(0, "Network blocked")

        class PublicMirror:
            def search(self, filters):
                return {"hits": [listing], "total": 1, "limit": catalog.PAGE,
                        "offset": filters["offset"], "source": "github"}

            def listing(self, public_id):
                assert public_id == listing["id"]
                return listing.copy()

            @contextmanager
            def request_archive(self, public_id):
                assert public_id == listing["id"]
                with io.BytesIO(archive) as response:
                    response.headers = {"Content-Length": str(len(archive))}
                    yield response

        reader = catalog.CommunityCatalog(self.friend, BlockedPrimary(), PublicMirror())
        page = reader.search({"q": "Сборка", "category": "adventure"})
        self.assertEqual(page["source"], "github")
        self.assertEqual(page["hits"][0]["id"], listing["id"])
        installed = reader.install(listing["id"], True, self.status)
        self.assertEqual(installed["name"], "Сборка друзей")
        self.assertTrue(self.friend.folder(installed["id"]).is_dir())

    def test_mirror_archive_fallback_verifies_same_primary_digest(self):
        listing = self.publish()
        archive = self.server.data

        class Primary:
            origin = "https://catalog.example"

            def json(self, method, path, **kwargs):
                return listing.copy()

            def request(self, method, path):
                raise catalog.CatalogError(0, "Origin archive blocked")

        class Mirror:
            def listing(self, public_id):
                return listing.copy()

            @contextmanager
            def request_archive(self, public_id):
                with io.BytesIO(archive) as response:
                    response.headers = {"Content-Length": str(len(archive))}
                    yield response

        reader = catalog.CommunityCatalog(self.friend, Primary(), Mirror())
        installed = reader.install(listing["id"], True, self.status)
        self.assertEqual(installed["name"], listing["name"])

    def test_origin_404_is_not_overridden_by_stale_mirror(self):
        self.publish()

        class MissingPrimary:
            origin = "https://catalog.example"

            def json(self, method, path, **kwargs):
                raise catalog.CatalogError(404, "Publication has been removed")

        class UnwantedMirror:
            def search(self, filters):
                raise AssertionError("A removed build must not be restored from stale mirror")

        reader = catalog.CommunityCatalog(self.friend, MissingPrimary(), UnwantedMirror())
        with self.assertRaises(catalog.CatalogError) as caught:
            reader.search({})
        self.assertEqual(caught.exception.code, 404)


import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from launcher import shared_builds as shared
from launcher.store import Store


def jar(mod="demo", version="1", environment="*"):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1, "id": mod, "name": mod.title(), "version": version, "environment": environment}))
    return result.getvalue()


def entry(path, data, ids=(), role="unknown", download=True):
    return {"path": path, "size": len(data), "hashes": {kind: hashlib.new(kind, data).hexdigest() for kind in ("sha1", "sha512")},
            "title": Path(path).stem, "number": "2", "ids": list(ids), "role": role,
            "downloads": ["https://cdn.modrinth.com/data/Test/versions/Version/file.jar"] if download else [], "project": "", "version": ""}


def descriptor(rows=(), **extra):
    return {"format": "BlockHarborSharedBuild", "formatVersion": 1, "name": "Сборка друга", "minecraft": "1.20.1", "loader": "fabric", "loaderVersion": None, "files": list(rows), **extra}


class SharedBuildsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name)
        self.pid = self.store.create("Моя сборка", "1.20.1", "fabric")
        self.folder = self.store.folder(self.pid)
        self.backups = []
        self.share = shared.SharedBuilds(self.store, before_change=lambda pid, label: self.backups.append((pid, label)))

    def test_descriptor_round_trip_has_no_private_paths_settings_or_nickname(self):
        (self.folder / "mods/demo.jar").write_bytes(jar())
        (self.folder / "config").mkdir()
        (self.folder / "config/accounts.json").write_text('{"password":"private"}')
        self.store.set("nickname", "NeverShareMe")
        desc = self.share.descriptor(self.pid, resolve=False)
        raw = json.dumps(desc)
        self.assertNotIn(str(self.store.root), raw)
        self.assertNotIn("NeverShareMe", raw)
        self.assertNotIn("password", raw)
        self.assertEqual(shared._decode(shared.encode_descriptor(desc)), desc)
        self.assertEqual(desc["files"][0]["ids"], ["demo"])
        self.assertEqual(desc["files"][0]["role"], "unknown")

    def test_client_only_mods_optional_missing_and_local_additions_preserved(self):
        local = jar("client", environment="client")
        (self.folder / "mods/client.jar").write_bytes(local)
        wanted = jar("server", "2")
        other_client = jar("clienttwo", environment="client")
        result = self.share.compare(self.pid, descriptor([entry("mods/server.jar", wanted, ["server"]), entry("mods/clienttwo.jar", other_client, ["clienttwo"], role="client")]))
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(len(result["optional"]), 1)
        self.assertEqual(result["extra"][0]["role"], "client")
        self.assertTrue(result["canApply"])

    def test_renamed_exact_files_match_without_duplicate_downloads(self):
        data = jar()
        (self.folder / "mods/renamed.jar").write_bytes(data)
        result = self.share.compare(self.pid, descriptor([entry("mods/original.jar", data, ["demo"])]))
        self.assertEqual(result["matching"], 1)
        self.assertEqual(result["missing"], [])
        self.assertTrue(result["ready"])

    def test_sync_exact_version_snapshots_and_preserves_client_settings(self):
        old, new, client = jar("demo", "1"), jar("demo", "2"), jar("client", environment="client")
        (self.folder / "mods/demo-old.jar").write_bytes(old)
        (self.folder / "mods/client.jar").write_bytes(client)
        (self.folder / "options.txt").write_text("fov:85\n")
        result = self.share.compare(self.pid, descriptor([entry("mods/demo-new.jar", new, ["demo"])]))
        self.assertEqual(len(result["different"]), 1)

        def download(url, target, hashes, size, **kwargs):
            self.assertEqual(hashes["sha512"], hashlib.sha512(new).hexdigest())
            target.write_bytes(new)

        with patch.object(shared, "download_verified", side_effect=download):
            applied = self.share.apply(result["token"], mode="existing")
        self.assertEqual(applied["id"], self.pid)
        self.assertEqual(len(self.backups), 1)
        self.assertFalse((self.folder / "mods/demo-old.jar").exists())
        self.assertEqual((self.folder / "mods/demo-new.jar").read_bytes(), new)
        self.assertEqual((self.folder / "mods/client.jar").read_bytes(), client)
        self.assertEqual((self.folder / "options.txt").read_text(), "fov:85\n")

    def test_clone_keeps_original_and_does_not_copy_worlds(self):
        old, new = jar("demo", "1"), jar("demo", "2")
        (self.folder / "mods/demo.jar").write_bytes(old)
        (self.folder / "config").mkdir()
        (self.folder / "config/personal.json").write_text('{"volume":10}')
        (self.folder / "saves/World").mkdir(parents=True)
        (self.folder / "saves/World/level.dat").write_bytes(b"precious")
        plan = self.share.compare(self.pid, descriptor([entry("mods/demo.jar", new, ["demo"])]))
        with patch.object(shared, "download_verified", side_effect=lambda url, target, *a, **kw: target.write_bytes(new)):
            result = self.share.apply(plan["token"], mode="clone")
        copied = self.store.folder(result["id"])
        self.assertNotEqual(result["id"], self.pid)
        self.assertEqual((copied / "mods/demo.jar").read_bytes(), new)
        self.assertEqual((self.folder / "mods/demo.jar").read_bytes(), old)
        self.assertTrue((copied / "config/personal.json").exists())
        self.assertFalse((copied / "saves").exists())

    def test_changed_build_plan_expiry_and_game_running_block_writes(self):
        data = jar()
        plan = self.share.compare(self.pid, descriptor([entry("mods/demo.jar", data, ["demo"])]))
        (self.folder / "mods/extra.jar").write_bytes(jar("extra"))
        with self.assertRaisesRegex(ValueError, "изменилась"):
            self.share.apply(plan["token"])
        plan = self.share.compare(self.pid, descriptor())
        self.share.plans[plan["token"]]["expires"] = 0
        with self.assertRaisesRegex(ValueError, "устарело"):
            self.share.apply(plan["token"])
        plan = self.share.compare(self.pid, descriptor())
        self.share.game_running = lambda pid: True
        with self.assertRaisesRegex(ValueError, "Закрой"):
            self.share.apply(plan["token"])

    def test_manual_missing_file_and_different_minecraft_require_explicit_clone(self):
        data = jar()
        result = self.share.compare(self.pid, descriptor([entry("mods/manual.jar", data, ["demo"], download=False)]))
        self.assertFalse(result["canApply"])
        self.assertEqual(len(result["manual"]), 1)
        with self.assertRaises(ValueError):
            self.share.apply(result["token"])
        result = self.share.compare(self.pid, descriptor(minecraft="1.21.1"))
        self.assertFalse(result["sameBase"])
        with self.assertRaisesRegex(ValueError, "отдельная"):
            self.share.apply(result["token"], mode="existing")

    def test_publish_failure_restores_original_files(self):
        old, new, second = jar("demo", "1"), jar("demo", "2"), jar("second", "1")
        (self.folder / "mods/demo.jar").write_bytes(old)
        rows = [entry("mods/demo.jar", new, ["demo"]), entry("mods/second.jar", second, ["second"])]
        plan = self.share.compare(self.pid, descriptor(rows))

        def download(url, target, hashes, size, **kwargs):
            target.write_bytes(new if hashes["sha512"] == rows[0]["hashes"]["sha512"] else second)

        original_replace = os.replace
        def replace(source, destination):
            if Path(destination) == self.folder / "mods/second.jar" and "new" in Path(source).parts:
                raise OSError("busy")
            return original_replace(source, destination)

        with patch.object(shared, "download_verified", side_effect=download), patch("launcher.build_care.os.replace", side_effect=replace), self.assertRaises(OSError):
            self.share.apply(plan["token"], mode="existing")
        self.assertEqual((self.folder / "mods/demo.jar").read_bytes(), old)
        self.assertFalse((self.folder / "mods/second.jar").exists())

    def test_process_crash_mid_sync_recovers_original_files_on_restart(self):
        old, new, second = jar("demo", "1"), jar("demo", "2"), jar("second", "1")
        (self.folder / "mods/demo.jar").write_bytes(old)
        rows = [entry("mods/demo.jar", new, ["demo"]), entry("mods/second.jar", second, ["second"])]
        code = r'''
import base64,json,os,sys
from pathlib import Path
from launcher import shared_builds as shared
from launcher.store import Store
store=Store(sys.argv[1]); pid=sys.argv[2]
share=shared.SharedBuilds(store)
desc=json.loads(sys.argv[3]); contents=json.loads(sys.argv[4])
def download(url,target,hashes,size,**kwargs):
    target.write_bytes(base64.b64decode(contents[hashes['sha512']]))
shared.download_verified=download
original=os.replace
def crash(source,target):
    original(source,target)
    if Path(target)==store.folder(pid)/'mods/demo.jar' and 'new' in Path(source).parts:
        os._exit(87)
os.replace=crash
plan=share.compare(pid,desc)
share.apply(plan['token'],mode='existing')
'''
        payload = {row["hashes"]["sha512"]: base64.b64encode(data).decode() for row, data in zip(rows, (new, second))}
        result = subprocess.run([sys.executable, "-c", code, str(self.store.root), self.pid,
                                 json.dumps(descriptor(rows)), json.dumps(payload)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 87, result.stderr)
        self.assertEqual((self.folder / "mods/demo.jar").read_bytes(), new)
        self.assertTrue(list((self.store.root / "recovery").glob("transaction-*")))
        from launcher.build_care import BuildCare
        recovery = BuildCare(Store(self.store.root))
        self.assertEqual(recovery.recovery_errors, {})
        self.assertEqual((self.folder / "mods/demo.jar").read_bytes(), old)
        self.assertFalse((self.folder / "mods/second.jar").exists())
        self.assertFalse(list((self.store.root / "recovery").glob("transaction-*")))

    def test_invalid_codes_paths_hashes_and_decompression_bomb(self):
        bomb = "bhshare:" + base64.urlsafe_b64encode(zlib.compress(b"x" * (shared.MAX_DESCRIPTOR + 1))).decode().rstrip("=")
        cases = [bomb, "bhshare:!bad", descriptor([entry("../demo.jar", b"x")]), descriptor([entry("mods/demo.jar", b"x"), entry("mods/DEMO.jar", b"y")]),
                 descriptor([entry("mods/one.jar", b"x"), entry("mods/two.jar", b"x")]), descriptor(invite={"address": "evil.example.com"})]
        for value in cases:
            with self.subTest(), self.assertRaises(ValueError):
                shared._decode(value)

    def test_real_invite_adds_nbt_server_once_preserving_unknown_tags_and_backup(self):
        tree = (10, b"", [(9, b"servers", (10, [[(8, b"name", b"Existing"), (8, b"ip", b"example.net"), (1, b"acceptTextures", b"\x01")]])),
                          (8, b"future", b"preserve me")])
        original = shared._NBT.encode(tree)
        (self.folder / "servers.dat").write_bytes(original)
        address = "s-abc123-abcdefghijklmnop.steam"
        result = self.share.add_server(self.pid, address, "Друг 🎮")
        self.assertTrue(result["ok"])
        read = shared._NBT((self.folder / "servers.dat").read_bytes()).read()
        self.assertEqual(read[2][1], tree[2][1])
        self.assertEqual(read[2][0][2][1][0], tree[2][0][2][1][0])
        self.assertEqual(len(read[2][0][2][1]), 2)
        self.assertEqual(read[2][0][2][1][1][0][2], shared._modified_utf8("Друг 🎮"))
        self.assertEqual((self.folder / ".blockharbor-servers.bak").read_bytes(), original)
        self.share.add_server(self.pid, address)
        self.assertEqual(len(self.backups), 1)

    def test_gzip_nbt_and_malformed_server_data(self):
        tree = (10, b"", [(9, b"servers", (10, []))])
        path = self.folder / "servers.dat"
        path.write_bytes(gzip.compress(shared._NBT.encode(tree)))
        self.share.add_server(self.pid, "s-123-456.steam")
        self.assertEqual(len(shared._NBT(path.read_bytes()).read()[2][0][2][1]), 1)
        for data in (b"broken", b"\x0a\x00\x00\x09\x00\x07servers\x0a\xff\xff\xff\xff", shared._NBT.encode(tree) + b"extra"):
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                self.share.add_server(self.pid, "s-123-456.steam")
            self.assertEqual(path.read_bytes(), data)

    def test_minecraft_empty_untyped_list_is_supported(self):
        path = self.folder / "servers.dat"
        path.write_bytes(shared._NBT.encode((10, b"", [(9, b"servers", (0, []))])))
        self.share.add_server(self.pid, "s-123-456.steam")
        self.assertEqual(shared._NBT(path.read_bytes()).read()[2][0][2][0], 10)

    def test_room_state_never_fakes_live_players(self):
        (self.folder / "logs").mkdir()
        (self.folder / "logs/latest.log").write_text("Steam LAN share ready for Steam user 1234")
        state = self.share.room_state(self.pid)
        self.assertFalse(state["gameRunning"])
        self.assertEqual(state["lastEvent"], "host")
        self.assertIsNone(state["liveParticipants"])
        self.assertNotIn("1234", json.dumps(state))

    def test_first_run_can_prepare_a_friend_build_without_an_existing_profile(self):
        new = jar("demo", "2")
        plan = self.share.compare("", descriptor([entry("mods/demo.jar", new, ["demo"])]))
        self.assertFalse(plan["sameBase"])
        self.assertTrue(plan["canApply"])
        with patch.object(shared, "download_verified", side_effect=lambda url, target, *a, **kw: target.write_bytes(new)):
            result = self.share.apply(plan["token"], mode="clone")
        self.assertEqual((self.store.folder(result["id"]) / "mods/demo.jar").read_bytes(), new)


if __name__ == "__main__":
    unittest.main()

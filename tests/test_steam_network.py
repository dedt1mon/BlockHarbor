import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from launcher import e4steam, friends, steam_network
from launcher.store import Store


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.s = Store(self.root)
        self.pid = self.s.create("Network", "1.20.1", "fabric")
        self.folder = self.s.folder(self.pid)
        self.cfg = self.folder / "config/e4steam/e4steam.toml"
        self.appid = self.folder / "steam_appid.txt"
        self.mods = self.s.content_folder(self.pid, "mods")

    def check(self):
        return steam_network.check(self.s, self.pid)

    def test_codes_compare_content_across_names_and_fresh_profiles(self):
        (self.mods / "example.jar").write_bytes(b"sample1")
        first = self.check()
        (self.mods / "example.jar").rename(self.mods / "renamed.jar")
        self.assertEqual(self.check()["modCode"], first["modCode"])
        (self.mods / "renamed.jar").write_bytes(b"sample2")
        self.assertNotEqual(self.check()["modCode"], first["modCode"])
        self.assertEqual(first["modCount"], 1)
        self.assertFalse(first["ready"])
        self.assertTrue(first["supported"])

    def test_steam_log_states_are_local_private_and_recent(self):
        log = self.folder / "logs/latest.log"
        log.parent.mkdir()
        with patch.object(
            steam_network,
            "getproxies",
            return_value={"https": "http://account:secret@private.example:1234"},
        ):
            self.assertTrue(self.check()["downloadProxy"])
            for marker, expected in [
                ("SteamAPI_Init failed: account", "signin"),
                (
                    "Steam Networking Messages initialized as 71234123451234123 using App ID 480",
                    "runtime",
                ),
                ("Steam LAN share ready for Steam user 71234123451234123", "host"),
                (
                    "Steam lobby creation failed with Timeout; continuing friends-only sharing by address",
                    "address",
                ),
                (
                    "Steam Networking Messages session with 71234123451234123 failed (reason 10)",
                    "transport",
                ),
            ]:
                with log.open("a") as f:
                    f.write(marker + " s-private-live-token.steam\n")
                current = self.check()
                self.assertEqual(current["state"], expected)
                for value in (
                    "71234123451234123",
                    "secret",
                    "s-private",
                    "private.example",
                ):
                    self.assertNotIn(value, json.dumps(current))

    def test_repairs_upstream_host_flag_and_conflicting_app_id_and_keeps_other_preferences(
        self,
    ):
        self.cfg.parent.mkdir(parents=True)
        old = b"# custom\r\nhostEnabled = false   # keep me\r\nlanOnlineMode = true\r\n[custom]\r\nhostEnabled=false\r\n"
        self.cfg.write_bytes(old)
        self.appid.write_text("1234")
        self.assertEqual(self.check()["issues"], ["host", "appid"])
        steam_network.repair_settings(self.folder)
        self.assertEqual(self.cfg.read_bytes(), old.replace(b"false", b"true", 1))
        self.assertEqual(self.appid.read_bytes(), b"480\n")
        self.assertFalse(self.check()["issues"])
        backup = next((self.folder / "blockharbor-backups/steam").iterdir())
        self.assertEqual((backup / "e4steam.toml").read_bytes(), old)
        self.assertEqual((backup / "steam_appid.txt").read_text(), "1234")
        self.assertIn("уже", steam_network.repair_settings(self.folder))

    def test_settings_symlink_is_not_followed(self):
        outside = self.root / "elsewhere"
        outside.write_text("6000")
        try:
            self.appid.symlink_to(outside)
        except OSError:
            self.skipTest("symlink unavailable")
        with self.assertRaises(ValueError):
            steam_network.repair_settings(self.folder)
        self.assertTrue(self.check()["configNote"])
        self.assertEqual(outside.read_text(), "6000")

    def test_opening_steam_starts_found_client_or_valves_installer(self):
        game = self.root / "steam.exe"
        game.touch()
        with (
            patch.object(friends.sys, "platform", "win32"),
            patch.object(os, "startfile", create=True) as start,
        ):
            with patch.object(friends, "_windows_steam", return_value=(False, None)):
                self.assertEqual(friends.open_steam(), "install")
                start.assert_called_once_with(friends.STEAM_SETUP)
            with (
                patch.object(friends, "_windows_steam", return_value=(False, game)),
                patch.object(friends.subprocess, "Popen") as launch,
            ):
                self.assertEqual(friends.open_steam(), "friends")
                launch.assert_called_once_with(
                    [str(game), friends.STEAM_FRIENDS], cwd=game.parent
                )

    def test_retry_incomplete_https_and_handle_407_without_credentials_in_error(self):
        raw = b"full file"
        url = "https://downloads.example/mod.jar"
        asset = e4steam.Asset(
            "core",
            "core.jar",
            len(raw),
            "sha256",
            hashlib.sha256(raw).hexdigest(),
            (url,),
        )
        cache = self.root / "cache"
        with patch.object(
            e4steam, "urlopen", side_effect=[io.BytesIO(raw[:1]), io.BytesIO(raw)]
        ) as download:
            self.assertEqual(
                e4steam._obtain(asset, cache, lambda _: None).read_bytes(), raw
            )
            self.assertEqual(download.call_count, 2)
        (cache / (asset.digest + ".jar")).unlink()
        for failure in [
            HTTPError(url, 407, "secret", {}, None),
            URLError("Tunnel connection failed: 407 secret"),
        ]:
            with patch.object(e4steam, "urlopen", side_effect=failure) as download:
                with self.assertRaisesRegex(
                    ValueError, "Прокси требует авторизации"
                ) as exc:
                    e4steam._obtain(asset, cache, lambda _: None)
                self.assertNotIn("secret", str(exc.exception))
                self.assertEqual(download.call_count, 1)
                self.assertEqual(list(cache.iterdir()), [])

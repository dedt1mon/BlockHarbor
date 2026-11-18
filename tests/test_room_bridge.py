import hashlib
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import room_bridge as bridge


class RoomBridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        (self.folder / "mods").mkdir()

    def prepare(self):
        with patch.object(bridge.e4steam, "installed", return_value=True):
            self.assertTrue(bridge.prepare(self.folder, "1.20.1", "fabric"))
        return json.loads((self.folder / ".blockharbor-room/request.json").read_text())

    def sample(self, request, **extra):
        result = {"schema": 1, "nonce": request["nonce"], "sequence": 1, "sampledAt": int(time.time() * 1000),
                  "state": "ACTIVE", "role": "INTEGRATED_HOST", "peers": 1, "capacity": 8, "names": ["Друг"],
                  "namesComplete": True, "heapUsed": 123456, "heapMax": 1024**3, **extra}
        (self.folder / ".blockharbor-room/status.json").write_text(json.dumps(result), encoding="utf-8")

    def test_bundled_jar_hash_api_is_not_shaded_and_java8_bytecode(self):
        data = bridge._BUNDLED.read_bytes()
        self.assertEqual(len(data), bridge.SIZE)
        self.assertEqual(hashlib.sha512(data).hexdigest(), bridge.SHA512)
        with zipfile.ZipFile(bridge._BUNDLED) as jar:
            self.assertFalse(any(name.startswith("link/e4steam/") for name in jar.namelist()))
            for name in jar.namelist():
                if name.endswith(".class"):
                    self.assertEqual(int.from_bytes(jar.read(name)[6:8], "big"), 52)
            metadata = json.loads(jar.read("fabric.mod.json"))
            self.assertIn("e4steam", metadata["entrypoints"])
            self.assertEqual(metadata["environment"], "client")

    def test_prepare_preserves_bundled_original_and_rotates_nonce(self):
        before = bridge._BUNDLED.read_bytes()
        request = self.prepare()
        self.assertEqual(bridge._BUNDLED.read_bytes(), before)
        self.assertEqual((self.folder / "mods" / bridge.FILENAME).read_bytes(), before)
        self.sample(request)
        next_request = self.prepare()
        self.assertNotEqual(request["nonce"], next_request["nonce"])
        self.assertFalse((self.folder / ".blockharbor-room/status.json").exists())

    def test_current_game_and_current_nonce_required_for_live_status(self):
        request = self.prepare()
        self.sample(request)
        self.assertIsNone(bridge.status(self.folder, game_running=False))
        state = bridge.status(self.folder, game_running=True)
        self.assertEqual(state["names"], ["Друг"])
        self.assertEqual(state["heapUsed"], 123456)
        self.sample(request, nonce="0" * 48)
        self.assertIsNone(bridge.status(self.folder, game_running=True))
        self.sample(request, sampledAt=int((time.time() - 9) * 1000))
        self.assertIsNone(bridge.status(self.folder, game_running=True))
        self.sample(request, sampledAt=int((time.time() + 3) * 1000))
        self.assertIsNone(bridge.status(self.folder, game_running=True))

    def test_closed_session_clears_all_participant_claims(self):
        request = self.prepare()
        self.sample(request, state="CLOSED")
        state = bridge.status(self.folder, game_running=True)
        self.assertEqual(state["names"], [])
        self.assertEqual(state["peers"], 0)

    def test_corrupt_types_size_capacity_names_and_redirected_files(self):
        request = self.prepare()
        for extra in ({"peers": True}, {"capacity": 300}, {"state": "INVENTED"}, {"names": ["bad\nname"]},
                      {"names": ["A", "B"]}, {"sequence": 0}):
            self.sample(request, **extra)
            self.assertIsNone(bridge.status(self.folder, game_running=True))
        path = self.folder / ".blockharbor-room/status.json"
        path.write_bytes(b"x" * 32769)
        self.assertIsNone(bridge.status(self.folder, game_running=True))
        path.unlink()
        path.symlink_to(self.folder / "outside.json")
        self.assertIsNone(bridge.status(self.folder, game_running=True))

    def test_supported_loader_gating_and_user_file_not_overwritten(self):
        self.assertFalse(bridge.supported("1.20.1", "forge"))
        self.assertFalse(bridge.supported("1.20.1", "neoforge"))
        self.assertFalse(bridge.supported("1.16.5", "fabric"))
        self.assertFalse(bridge.prepare(self.folder, "1.20.1", "vanilla"))
        target = self.folder / "mods" / bridge.FILENAME
        target.write_bytes(b"custom")
        with patch.object(bridge.e4steam, "installed", return_value=True), self.assertRaises(ValueError):
            bridge.prepare(self.folder, "1.20.1", "fabric")
        self.assertEqual(target.read_bytes(), b"custom")


if __name__ == "__main__":
    unittest.main()

"""Compatibility and expiry tests for the opt-in GitHub mirror index."""
import importlib.util
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "sync_community_mirror.py"
spec = importlib.util.spec_from_file_location("community_mirror_sync", SCRIPT)
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)

ROW = {
    "id": "BH-AB1234567890", "name": "Example", "minecraft": "1.20.1",
    "loader": "fabric", "category": "adventure", "size": 42,
    "sha256": "1" * 64,
}


class MirrorIndexExpiryTests(unittest.TestCase):
    def test_expiring_archives_are_never_copied_and_existing_copy_is_withdrawn(self):
        live = {**ROW, "expiresAt": int(time.time()) + 3600}
        tag = mirror.PREFIX + mirror.shard(live["id"])
        releases = {tag: {"assets": [{"name": live["id"] + ".zip", "size": live["size"],
                                      "digest": "sha256:" + live["sha256"]}]},
                    mirror.INDEX_TAG: {"assets": []}}
        calls = []

        def fake_gh(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("release", "upload", mirror.INDEX_TAG):
                raw = Path(args[3]).read_bytes()
                manifest = json.loads(raw)
                self.assertEqual(manifest["items"], [])
                self.assertEqual(manifest["total"], 0)
                releases[mirror.INDEX_TAG]["assets"] = [{"name": "index.json", "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}]
            return ""

        with patch.dict(os.environ, {"GITHUB_REPOSITORY": mirror.REPO, "GH_TOKEN": "test-only"}), \
             patch.object(mirror, "listings", return_value=[live]), \
             patch.object(mirror, "release", side_effect=releases.get), \
             patch.object(mirror, "ensure_release", side_effect=lambda name, title: releases[name]), \
             patch.object(mirror, "download_verified") as download, \
             patch.object(mirror, "gh", side_effect=fake_gh):
            mirror.sync()
        download.assert_not_called()
        self.assertIn(("release", "delete-asset", tag, live["id"] + ".zip", "--yes", "--repo", mirror.REPO), calls)
        self.assertFalse(any(call[:3] == ("release", "upload", tag) for call in calls))

    def test_legacy_fallback_remains_eligible(self):
        self.assertEqual(mirror.mirrorable([ROW, {**ROW, "expiresAt": int(time.time()) + 3600}]), [ROW])

    def test_manifest_carries_expiration_through_validation(self):
        expires = int(time.time()) + 1800
        result = mirror.validate({**ROW, "expiresAt": expires})
        self.assertEqual(result["expiresAt"], expires)
        self.assertEqual(mirror.archive_url(ROW["id"]).split("/")[-1],
                         ROW["id"] + ".zip")

    def test_mirror_skips_expired_entries_without_changing_pagination(self):
        expired = {**ROW, "expiresAt": int(time.time()) - 1}
        live = {**ROW, "id": "BH-CD1234567890",
                "expiresAt": int(time.time()) + 3000}
        with patch.object(mirror, "read_json", return_value={
            "hits": [expired, live], "total": 2
        }) as fetch:
            self.assertEqual(mirror.listings(), [live])
        self.assertEqual(fetch.call_count, 1)

    def test_mirror_keeps_legacy_listings_until_server_rollout(self):
        with patch.object(mirror, "read_json", return_value={
            "hits": [ROW], "total": 1
        }):
            self.assertEqual(mirror.listings(), [ROW])

    def test_cached_index_prunes_expired_assets_when_origin_is_offline(self):
        live = {**ROW, "expiresAt": int(time.time()) + 120}
        expired = {**ROW, "id": "BH-FF1234567890", "expiresAt": int(time.time()) - 10}
        source = json.dumps({"format": 1, "total": 2, "items": [live, expired]}).encode()
        index = {"assets": [{"name": "index.json",
                             "digest": "sha256:" + hashlib.sha256(source).hexdigest()}]}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            def fake_download(*args, **kwargs):
                (folder / "index.json").write_bytes(source)
                return ""
            with patch.object(mirror, "release", return_value=index), patch.object(
                mirror, "gh", side_effect=fake_download
            ):
                self.assertEqual(mirror.cached_listings(folder), [live])

    def test_cached_index_requires_proven_expiry_and_integrity(self):
        legacy = json.dumps({"format": 1, "total": 1, "items": [ROW]}).encode()
        index = {"assets": [{"name": "index.json",
                             "digest": "sha256:" + hashlib.sha256(legacy).hexdigest()}]}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            def fake_download(*args, **kwargs):
                (folder / "index.json").write_bytes(legacy)
                return ""
            with patch.object(mirror, "release", return_value=index), patch.object(
                mirror, "gh", side_effect=fake_download
            ):
                with self.assertRaisesRegex(RuntimeError, "no expiration"):
                    mirror.cached_listings(folder)
            index["assets"][0]["digest"] = "sha256:" + "0" * 64
            with patch.object(mirror, "release", return_value=index), patch.object(
                mirror, "gh", side_effect=fake_download
            ):
                with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                    mirror.cached_listings(folder)

    def test_rejects_invalid_expiry_type(self):
        with self.assertRaises(ValueError):
            mirror.validate({**ROW, "expiresAt": "soon"})
        with self.assertRaises(ValueError):
            mirror.validate({**ROW, "expiresAt": True})


if __name__ == "__main__":
    unittest.main()

"""Tests for GitHub public mirror fallback without network access."""
import hashlib
import io
import json
import time
import unittest

from launcher import community_catalog as catalog


ROW = {
    "id": "BH-B5E5524DA526", "name": "Мир во всём мире",
    "minecraft": "1.20.1", "loader": "forge", "category": "kitchen-sink",
    "size": 3, "sha256": hashlib.sha256(b"ZIP").hexdigest(),
    "expiresAt": int(time.time()) + 3600,
}


class FakeResponse(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Length": str(len(data))}

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, manifest, url="https://github.com/dedt1mon/BlockHarbor/releases/download/community-mirror-index/index.json"):
        self.manifest, self.url = manifest, url

    def open(self, request, timeout=15):
        return FakeResponse(json.dumps(self.manifest).encode(), self.url)


class MirrorFallbackTests(unittest.TestCase):
    def mirror(self, *items, generated=None, url=None):
        manifest = {"format": 1, "generated": int(time.time()) if generated is None else generated,
                    "total": len(items), "items": list(items)}
        return catalog.GitHubMirror(Opener(manifest, url=url or catalog.MIRROR_INDEX))

    def test_mirror_search_filters_and_pages(self):
        other = {**ROW, "id": "BH-AB1234567890", "name": "Приключение", "category": "adventure"}
        mirror = self.mirror(ROW, other)
        params = dict(q="мир", version="1.20.1", loader="forge", category="", offset=0)
        result = mirror.search(params)
        self.assertEqual([row["id"] for row in result["hits"]], [ROW["id"]])
        self.assertEqual(result["source"], "github")
        params["q"] = ROW["id"].lower()
        self.assertEqual(mirror.search(params)["hits"][0]["id"], ROW["id"])
        params.update(q="", category="adventure")
        self.assertEqual(mirror.search(params)["hits"][0]["name"], "Приключение")
        params.update(offset=2, category="")
        self.assertEqual(mirror.search(params)["hits"], [])
        self.assertEqual(mirror.search(params)["total"], 2)

    def test_mirror_uses_fixed_urls_and_validates_ids(self):
        mirror = self.mirror(ROW)
        self.assertEqual(mirror.listing(ROW["id"]), ROW)
        self.assertTrue(mirror.archive_url(ROW["id"]).endswith(
            "/community-mirror-15/BH-B5E5524DA526.zip"))
        with self.assertRaises(ValueError):
            mirror.archive_url("../../private")
        with self.assertRaises(catalog.CatalogError):
            mirror.listing("BH-AAAAAAAAAAAA")

    def test_expired_or_corrupt_index_is_rejected(self):
        with self.assertRaisesRegex(catalog.CatalogError, "давно"):
            self.mirror(ROW, generated=int(time.time()) - catalog.MIRROR_MAX_AGE - 30).search(
                dict(q="", version="", loader="", category="", offset=0))
        invalid = {**ROW, "sha256": "invalid"}
        with self.assertRaises(ValueError):
            self.mirror(invalid).listing(ROW["id"])
        duplicate = self.mirror(ROW, ROW)
        with self.assertRaisesRegex(catalog.CatalogError, "повторяющиеся"):
            duplicate.listing(ROW["id"])

    def test_expired_mirror_record_is_never_offered_or_downloaded(self):
        expired = {**ROW, "expiresAt": int(time.time()) - 5}
        mirror = self.mirror(expired)
        params = dict(q="", version="", loader="", category="", offset=0)
        self.assertEqual(mirror.search(params)["hits"], [])
        self.assertEqual(mirror.search(params)["total"], 0)
        with self.assertRaisesRegex(catalog.CatalogError, "не найдена"):
            mirror.listing(ROW["id"])

    def test_mirror_rejects_legacy_index_without_expiration(self):
        legacy = {key: value for key, value in ROW.items() if key != "expiresAt"}
        with self.assertRaisesRegex(catalog.CatalogError, "сроки публикаций"):
            self.mirror(legacy).listing(ROW["id"])

    def test_mirror_rejects_non_https_or_third_party_redirect(self):
        mirror = self.mirror(ROW, url="http://untrusted.invalid/archive")
        with self.assertRaisesRegex(catalog.CatalogError, "небезопасный"):
            mirror.listing(ROW["id"])


if __name__ == "__main__":
    unittest.main()

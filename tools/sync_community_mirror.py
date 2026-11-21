"""Read-only GitHub mirror for publicly shared BlockHarbor builds.

The original registry remains authoritative. This script replicates only public
ready listings and archive bytes; author secrets and upload sessions never leave
their owners. Runs via a scoped GITHUB_TOKEN in GitHub Actions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = "dedt1mon/BlockHarbor"
SOURCE = "https://blockharbor-community-catalog.ded8timon.chatgpt.site"
INDEX_TAG = "community-mirror-index"
PREFIX = "community-mirror-"
SHARDS = 32
MAX_LISTINGS = 20000
MAX_ARCHIVE = 2 * 1024**3 - 1
PAGE = 20
ID = re.compile(r"BH-[A-F0-9]{12}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
CATEGORIES = set("adventure challenging combat kitchen-sink lightweight magic multiplayer optimization quests technology".split())
LOADERS = {"vanilla", "fabric", "quilt", "forge", "neoforge"}


def shard(public_id):
    if not isinstance(public_id, str) or not ID.fullmatch(public_id):
        raise ValueError("Invalid public build ID")
    return f"{int(public_id[3:5], 16) % SHARDS:02x}"


def archive_url(public_id):
    return f"https://github.com/{REPO}/releases/download/{PREFIX}{shard(public_id)}/{public_id}.zip"


def read_json(url):
    with urlopen(Request(url, headers={"User-Agent": "BlockHarbor-Community-Mirror/1",
                                       "Accept": "application/json"}), timeout=60) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise RuntimeError("Catalog page exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Invalid catalog page")
    return value


def validate(item):
    if not isinstance(item, dict):
        raise ValueError("Invalid listing")
    public_id = item.get("id")
    shard(public_id)
    name, mc, loader, category = (item.get(k) for k in ("name", "minecraft", "loader", "category"))
    size, digest = item.get("size"), item.get("sha256")
    if (not isinstance(name, str) or not name or len(name) > 128
            or not isinstance(mc, str) or not mc or len(mc) > 64
            or loader not in LOADERS or category not in CATEGORIES
            or type(size) is not int or not 0 < size <= MAX_ARCHIVE
            or not isinstance(digest, str) or not HASH.fullmatch(digest)):
        raise ValueError(f"Invalid public metadata for {public_id}")
    return {k: item[k] for k in ("id", "name", "minecraft", "loader", "category", "size", "sha256")}


def listings():
    items, seen = [], set()
    total = None
    while total is None or len(items) < total:
        page = read_json(SOURCE + "/v1/builds?" + urlencode({"offset": len(items)}))
        hits, count = page.get("hits"), page.get("total")
        if (not isinstance(hits, list) or len(hits) > PAGE or type(count) is not int
                or not 0 <= count <= MAX_LISTINGS or (total is not None and count != total)
                or (count > len(items) and not hits)):
            raise RuntimeError("Source catalog pagination changed; retry on next sync")
        total = count
        for raw in hits:
            item = validate(raw)
            if item["id"] in seen:
                raise RuntimeError("Duplicate catalog ID; retry on next sync")
            seen.add(item["id"])
            items.append(item)
        if len(items) > count:
            raise RuntimeError("Catalog changed during pagination")
    return items


def gh(*args, missing_ok=False):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        if missing_ok and ("HTTP 404" in result.stderr or "release not found" in result.stderr.lower()):
            return None
        raise RuntimeError(f"GitHub command failed ({' '.join(args[:3])}): {result.stderr[:400]}")
    return result.stdout


def release(tag):
    value = gh("api", f"repos/{REPO}/releases/tags/{tag}", missing_ok=True)
    return json.loads(value) if value else None


def ensure_release(tag, title):
    existing = release(tag)
    if existing:
        if not existing.get("prerelease") or existing.get("draft"):
            raise RuntimeError("Mirror release is not a public prerelease")
        return existing
    gh("release", "create", tag, "--repo", REPO, "--target", "main", "--prerelease",
       "--latest=false", "--title", title,
       "--notes", "Automated public read-only mirror of opt-in BlockHarbor builds; not a launcher update.")
    created = release(tag)
    if not created:
        raise RuntimeError("Could not create mirror release")
    return created


def assets(rel):
    return {asset["name"]: asset for asset in rel.get("assets", [])}


def download_verified(item, dest):
    public_id = item["id"]
    url = SOURCE + "/v1/builds/" + public_id + "/archive"
    req = Request(url, headers={"User-Agent": "BlockHarbor-Community-Mirror/1"})
    hashed = hashlib.sha256()
    count = 0
    with urlopen(req, timeout=120) as response, dest.open("xb") as out:
        if response.headers.get("Content-Length") not in (None, str(item["size"])):
            raise RuntimeError(f"Unexpected Content-Length: {public_id}")
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > item["size"]:
                raise RuntimeError(f"Archive larger than claimed: {public_id}")
            hashed.update(chunk)
            out.write(chunk)
    if count != item["size"] or hashed.hexdigest() != item["sha256"]:
        raise RuntimeError(f"Archive SHA-256 mismatch: {public_id}")


def sync():
    if os.getenv("GITHUB_REPOSITORY") != REPO or not os.getenv("GH_TOKEN"):
        raise RuntimeError("Run only in the official repository with GITHUB_TOKEN")
    items = listings()
    print(f"Source: {len(items)} public builds", flush=True)
    groups = {}
    for item in items:
        groups.setdefault(shard(item["id"]), []).append(item)

    with tempfile.TemporaryDirectory() as folder:
        scratch = Path(folder)
        for index in range(SHARDS):
            key = f"{index:02x}"
            tag = PREFIX + key
            group = groups.get(key, [])
            current = release(tag)
            if not current and not group:
                continue
            if current is None:
                current = ensure_release(tag, "BlockHarbor community mirror " + key)
            present = assets(current)
            wanted = {item["id"] + ".zip" for item in group}
            for item in group:
                name = item["id"] + ".zip"
                old = present.get(name)
                if old and old.get("size") == item["size"] and old.get("digest") == "sha256:" + item["sha256"]:
                    continue
                print(f"Mirror {name}: {item['size']} bytes", flush=True)
                dest = scratch / name
                download_verified(item, dest)
                gh("release", "upload", tag, str(dest), "--clobber", "--repo", REPO)
                dest.unlink()
            for name in sorted(set(present) - wanted):
                if name.endswith(".zip") and ID.fullmatch(name[:-4]):
                    print(f"Withdraw {name} from mirror", flush=True)
                    gh("release", "delete-asset", tag, name, "--yes", "--repo", REPO)

        manifest = {"format": 1, "generated": int(time.time()), "total": len(items), "items": items}
        ensure_release(INDEX_TAG, "BlockHarbor public community index")
        dest = scratch / "index.json"
        dest.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        gh("release", "upload", INDEX_TAG, str(dest), "--clobber", "--repo", REPO)
        asset = assets(release(INDEX_TAG) or {}).get("index.json", {})
        expected = "sha256:" + hashlib.sha256(dest.read_bytes()).hexdigest()
        if asset.get("digest") != expected:
            raise RuntimeError("Mirror manifest digest could not be verified")
    print(f"Mirror complete: {len(items)} items", flush=True)


if __name__ == "__main__":
    sync()

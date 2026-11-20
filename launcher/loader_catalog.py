"""Published loader compatibility, with a bundled snapshot and an on-demand cache."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import lifecycle, preparation, version_ids

NAMES = {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}
SOURCES = {
    "fabric": "https://meta.fabricmc.net/v2/versions/game",
    "quilt": "https://meta.quiltmc.org/v3/versions/game",
    "forge": "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml",
    "neoforge": "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml",
}
_LOCK = threading.RLock()
_ATTEMPTS = {}
_TTL = 3600
_CACHE_NAME = ".blockharbor-loader-catalog.json"


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return 0


def _read_snapshot(cache: Path) -> dict:
    bundled = json.loads(Path(__file__).with_name("loader_catalog.json").read_text(encoding="utf-8"))
    for entry in bundled["providers"].values():
        entry["origin"] = "bundled"
        entry.setdefault("fetchedAt", bundled["fetchedAt"])
    path = Path(cache) / _CACHE_NAME
    try:
        if path.stat().st_size > 4 * 1024**2:
            return bundled
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("format") != 1:
            return bundled
        for loader, entry in saved.get("providers", {}).items():
            if (
                loader in SOURCES and isinstance(entry, dict)
                and isinstance(entry.get("versions"), list)
                and len(entry["versions"]) <= 10000
                and all(version_ids.valid(v) for v in entry["versions"])
                and _timestamp(entry.get("fetchedAt")) >= _timestamp(bundled["providers"][loader]["fetchedAt"])
            ):
                base = bundled["providers"][loader]
                if loader == "forge" and "catalogRevision" not in entry:
                    # Clients before 2.5 discarded Forge's historical _pre4 ID.
                    entry["versions"] = list(dict.fromkeys([*entry["versions"], "1.7.10-pre4"]))
                bundled["providers"][loader] = {**base, **entry, "origin": "cache"}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return bundled


def _write_snapshot(cache: Path, snapshot: dict) -> None:
    lifecycle.check()
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".blockharbor-metadata-", delete=False) as out:
            temporary = Path(out.name)
            out.write(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        with lifecycle.guard():
            os.replace(temporary, cache / _CACHE_NAME)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _neo_minecraft(pin: str) -> str | None:
    # NeoForge's pre-2026 numbering starts with Minecraft's minor/patch;
    # calendar releases use MC year.release.patch followed by the loader build.
    special = re.fullmatch(r"0\.(\d{2}w\d{2}[A-Za-z0-9_-]+)\.\d+(?:-.*)?", pin)
    if special:
        return special[1]
    calendar = re.fullmatch(r"(\d{2})\.(\d+)\.(\d+)\.\d+(?:-.*)?", pin)
    if calendar and int(calendar[1]) >= 26:
        base = f"{calendar[1]}.{calendar[2]}"
        if int(calendar[3]):
            base += "." + calendar[3]
        suffix = pin.partition("+")[2]
        return base + "-" + suffix if suffix else base
    legacy = re.fullmatch(r"(\d{2})\.(\d+)\.\d+(?:-.*)?", pin)
    if legacy and 20 <= int(legacy[1]) < 26:
        return f"1.{legacy[1]}" + ("." + legacy[2] if int(legacy[2]) else "")
    return None


def _pin_key(pin):
    numbers = tuple(int(n) for n in re.findall(r"\d+", pin))
    return (bool(re.fullmatch(r"\d+(?:\.\d+)+", pin)), numbers, pin)


def _forge_game(coordinate: str) -> str | None:
    slug, separator, _build = coordinate.partition("-")
    if separator and re.fullmatch(r"\d+(?:\.\d+)+(?:_pre\d+)?", slug):
        return slug.replace("_pre", "-pre")
    return None


def _fetch_provider(loader: str) -> dict:
    url = SOURCES[loader]
    releases = {}
    if loader in {"fabric", "quilt"}:
        rows = preparation.json_document(url, quick_catalog=True)
        versions = [version_ids.validate(row["version"]) for row in rows]
    else:
        body = preparation.metadata_response(url, quick_catalog=True).text
        root = ET.fromstring(body)
        pins = [row.text for row in root.findall("./versioning/versions/version") if row.text]
        for pin in pins:
            if loader == "forge":
                version = _forge_game(pin)
                if not version or tuple(map(int, version.split("-")[0].split("."))) < (1, 5, 2):
                    continue
            else:
                version = _neo_minecraft(pin)
            if version and version_ids.valid(version) and version_ids.valid(pin):
                releases.setdefault(version, []).append(pin)
        for pins in releases.values():
            pins.sort(key=_pin_key, reverse=True)
        versions = list(releases)
    if not versions:
        raise ValueError(f"Каталог {NAMES[loader]} вернул пустой список.")
    result = {"source": url, "versions": list(dict.fromkeys(versions)), "fetchedAt": datetime.now(timezone.utc).isoformat(), "origin": "live"}
    if loader in {"fabric", "quilt"}:
        try:
            builds = preparation.json_document(url.rsplit("/", 1)[0] + "/loader", quick_catalog=True)
        except preparation.DownloadError:
            # A usable game compatibility list does not require a new preferred build.
            builds = []
        stable = [row["version"] for row in builds if row.get("stable", True) and re.fullmatch(r"\d+(?:\.\d+)+", row["version"])]
        if stable:
            result["stablePin"] = max(stable, key=_pin_key)
    if loader == "forge":
        result["catalogRevision"] = 2
    if releases:
        result["releases"] = releases
    return result


def refresh_catalog(cache: Path, *, force=False) -> dict:
    with _LOCK:
        snapshot = _read_snapshot(cache)
        now = time.time()
        oldest = min((_timestamp(p.get("fetchedAt")) for p in snapshot["providers"].values()), default=0)
        key = str(Path(cache).resolve())
        if not force and (0 <= now - oldest < _TTL or now - _ATTEMPTS.get(key, 0) < 60):
            return snapshot
        _ATTEMPTS[key] = now
        changed = False
        with lifecycle.Executor(max_workers=4) as pool:
            requests = {loader: pool.submit(_fetch_provider, loader) for loader in SOURCES}
            for loader, request in requests.items():
                try:
                    lifecycle.check()
                    previous = snapshot["providers"][loader]
                    snapshot["providers"][loader] = {**previous, **request.result()}
                    snapshot["providers"][loader].pop("refreshFailed", None)
                    changed = True
                except lifecycle.OperationCancelled:
                    raise
                except (OSError, ValueError, KeyError, TypeError, ET.ParseError, preparation.DownloadError):
                    snapshot["providers"][loader]["refreshFailed"] = True
        if changed:
            _write_snapshot(cache, snapshot)
        return snapshot


def unsupported_message(version: str, loader: str) -> str:
    return (
        f"Для Minecraft {version} нет поддерживаемой установки {NAMES[loader]}. "
        "Выбери Vanilla для игры без модов или другой доступный загрузчик. "
        "Для существующей сборки создай отдельную сборку с подходящим загрузчиком."
    )


def _availability(snapshot: dict, version: str) -> dict:
    rows = [{"id": "vanilla", "name": "Vanilla", "supported": True, "message": "Без модов.", "source": "mojang", "stale": False}]
    known = version in snapshot.get("minecraft", [])
    for loader in ("fabric", "forge", "neoforge", "quilt"):
        entry = snapshot["providers"][loader]
        supported = True if version in entry["versions"] else (False if known else None)
        stale = bool(entry.get("refreshFailed")) or time.time() - _timestamp(entry.get("fetchedAt")) >= _TTL
        message = unsupported_message(version, loader) if supported is False else (
            "Совместимость пока не подтверждена. Проверим её при подготовке игры."
            if supported is None else "Доступен для этой версии Minecraft."
        )
        archived = loader == "forge" and version in entry.get("manualVersions", ())
        if archived:
            message = f"Архивный Forge для Minecraft {version} существует. Эта версия требует ручной установки патча в игровой JAR."
            supported = False
        if stale:
            message += " Используются сохранённые данные совместимости."
        rows.append({"id": loader, "name": NAMES[loader], "manual": archived, "supported": supported, "message": message, "source": entry.get("origin", "bundled"), "stale": stale})
    return {"version": version, "loaders": rows, "checkedAt": max((p.get("fetchedAt", "") for p in snapshot["providers"].values()), default=snapshot.get("fetchedAt", ""))}


def availability(cache: Path, version: str, *, refresh=False) -> dict:
    version_ids.validate(version)
    snapshot = refresh_catalog(cache, force=True) if refresh else _read_snapshot(cache)
    return _availability(snapshot, version)


def require_supported(cache: Path, version: str, loader: str) -> None:
    """Local preflight: offline installed games never need a catalog request."""
    version_ids.validate(version)
    if loader not in NAMES:
        raise ValueError("Неизвестный загрузчик модов.")
    if loader == "vanilla":
        return
    row = next(row for row in _availability(_read_snapshot(cache), version)["loaders"] if row["id"] == loader)
    if row["supported"] is False:
        raise ValueError(row["message"])


def latest_neoforge(cache: Path, version: str) -> str:
    snapshot = _read_snapshot(cache)
    pins = snapshot["providers"]["neoforge"].get("releases", {}).get(version, [])
    if not pins:
        pins = _fetch_provider("neoforge").get("releases", {}).get(version, [])
    if not pins:
        raise ValueError(unsupported_message(version, "neoforge"))
    return version_ids.validate(sorted(pins, key=_pin_key, reverse=True)[0])


def meta_pin(cache: Path, version: str, loader: str) -> str | None:
    entry = _read_snapshot(cache)["providers"][loader]
    pin = entry.get("stablePin")
    return pin if version in entry["versions"] and isinstance(pin, str) and re.fullmatch(r"\d+(?:\.\d+)+", pin) else None


def forge_coordinate(cache: Path, version: str, requested: str | None = None) -> str:
    """Resolve a short marketing version to the actual (sometimes branch-suffixed) artifact."""
    version_ids.validate(version)
    if requested:
        version_ids.validate(requested)
    entry = _read_snapshot(cache)["providers"]["forge"]

    def choose(source):
        pins = source.get("releases", {}).get(version, [])
        preferred = source.get("preferred", {}).get(version)
        candidates = [preferred] if preferred and (not pins or preferred in pins) else []
        for candidate in [*candidates, *pins]:
            if not version_ids.valid(candidate) or _forge_game(candidate) != version:
                continue
            build = candidate.partition("-")[2]
            if requested is None or requested in (candidate, build, build.partition("-")[0]):
                return candidate
        return None

    coordinate = choose(entry)
    if not coordinate:
        coordinate = choose(_fetch_provider("forge"))
    if not coordinate:
        if requested:
            raise ValueError(f"Forge {requested} отсутствует в каталоге Minecraft {version}.")
        raise ValueError(unsupported_message(version, "forge"))
    return coordinate

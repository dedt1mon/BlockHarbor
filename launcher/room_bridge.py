"""Pinned read-only e4steam status addon; no launcher token enters Minecraft."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from . import e4steam
from .modfiles import file_hash, file_stamp

FILENAME = "blockharbor-room-status.jar"
SIZE = 8522
SHA512 = "1a31d46742fa198cf2d7ce1396640c0e89ffd15a003c8c498af68645a18144ea77600f088f7e28a6534630cabac1b4cfeda027e6822580de16cd98242b80adc6"
_BUNDLED = Path(__file__).resolve().parents[1] / "bundled" / "bridge" / FILENAME
_DIRECTORY = ".blockharbor-room"
_ROLES = {"NONE", "INTEGRATED_HOST", "GUEST", "DEDICATED_SERVER_CLIENT", "DEDICATED_SERVER"}
_STATES = {"NONE", "CREATING", "ACTIVE", "RECONNECTING", "CLOSING", "CLOSED", "FAILED", "UNAVAILABLE"}


def supported(version, loader):
    # Runtime/API discovery and automatic preparation are verified against this
    # pinned upstream family. Do not inject a Fabric mod into Forge/NeoForge.
    return loader in {"fabric", "quilt"} and e4steam.CORE_VERSION == "0.3.6" and e4steam.support(version, loader)


def _verified(path):
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size == SIZE and file_hash(path, file_stamp(path), "sha512") == SHA512
    except OSError:
        return False


def installed(folder):
    return _verified(Path(folder) / "mods" / FILENAME)


def protected_names(version, loader):
    return frozenset({FILENAME}) if supported(version, loader) else frozenset()


def managed_names(folder, version, loader):
    return protected_names(version, loader) if supported(version, loader) and installed(folder) else frozenset()


def _local(folder, name):
    root = Path(folder).resolve()
    directory = root / _DIRECTORY
    path = directory / name
    if directory.is_symlink() or directory.resolve() != directory or path.is_symlink() or path.resolve().parent != directory:
        raise ValueError("Файлы статуса BlockHarbor перенаправлены. Проверь папку сборки.")
    return path


def prepare(folder, version, loader):
    """Call after preparing e4steam, before every real Minecraft launch.

    A fresh per-launch nonce invalidates any earlier process's snapshot, including
    a copied or restored world. The old status is removed before launching Java.
    """
    if not supported(version, loader):
        return False
    folder = Path(folder).resolve()
    if not e4steam.installed(folder, version, loader):
        return False
    if not _verified(_BUNDLED):
        raise ValueError("Встроенный компонент статуса BlockHarbor повреждён. Переустанови лаунчер.")
    mods = e4steam._mods_folder(folder)
    mods.mkdir(exist_ok=True)
    target = mods / FILENAME
    if target.exists() and not _verified(target):
        raise ValueError("Файл " + FILENAME + " отличается от встроенного компонента. Сохрани его отдельно и повтори запуск.")
    if not target.exists():
        from .modfiles import publish_new
        with tempfile.NamedTemporaryFile(dir=mods, prefix=".room-status-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_BUNDLED.read_bytes())
        try:
            if not _verified(temporary):
                raise ValueError("Компонент статуса изменился во время копирования.")
            publish_new(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    request, state = _local(folder, "request.json"), _local(folder, "status.json")
    request.parent.mkdir(exist_ok=True)
    state.unlink(missing_ok=True)
    value = {"schema": 1, "nonce": secrets.token_hex(24), "issuedAt": int(time.time() * 1000)}
    with tempfile.NamedTemporaryFile(dir=request.parent, prefix="request-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    try:
        os.replace(temporary, request)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def status(folder, *, game_running=False, now=None):
    """Only a fresh snapshot tied to this launch can report participants."""
    if not game_running or not installed(folder):
        return None
    now = time.time() if now is None else now
    try:
        request, path = _local(folder, "request.json"), _local(folder, "status.json")
        if request.stat().st_size > 1024 or path.stat().st_size > 32768:
            return None
        expected, data = json.loads(request.read_bytes()), json.loads(path.read_bytes())
        if not isinstance(expected, dict) or not isinstance(data, dict) or data.get("schema") != 1 or expected.get("schema") != 1:
            return None
        nonce = expected.get("nonce", "")
        if not isinstance(nonce, str) or not re.fullmatch("[a-f0-9]{48}", nonce) or data.get("nonce") != nonce:
            return None
        sampled, sequence = data.get("sampledAt"), data.get("sequence")
        if type(sampled) is not int or type(sequence) is not int or not 1 <= sequence < 2**53 or not -2 <= now - sampled / 1000 <= 8:
            return None
        peers, capacity = data.get("peers"), data.get("capacity")
        if type(peers) is not int or type(capacity) is not int or not 0 <= peers <= capacity <= 256:
            return None
        if data.get("state") not in _STATES or data.get("role") not in _ROLES:
            return None
        names = data.get("names")
        if not isinstance(names, list) or len(names) > min(64, peers) or any(not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name) for name in names):
            return None
        # If the core has not reported an active session, no old participant data
        # is allowed to remain visible, even if a caller writes inconsistent JSON.
        active = data["state"] == "ACTIVE"
        result = {"source": "e4steam-api", "live": True, "state": data["state"], "role": data["role"],
                  "peers": peers if active else 0, "capacity": capacity, "names": names if active else [],
                  "namesComplete": data.get("namesComplete") is True, "sequence": sequence, "sampledAt": sampled}
        for field in ("heapUsed", "heapMax"):
            amount = data.get(field)
            if type(amount) is int and 0 <= amount <= 2**40:
                result[field] = amount
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        return None

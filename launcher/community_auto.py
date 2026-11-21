"""Lightweight, privacy-aware fingerprint for previously shared modpacks.

Hashes only public-archive inputs' metadata; actual bytes receive a full SHA-256
at upload time. Worlds, logs, accounts and arbitrary unrelated files are ignored.
"""
from __future__ import annotations

import hashlib
import json

from . import build_archive, lifecycle
from .store import KINDS


def signature(store, pid, *, include_configs=False):
    if type(include_configs) is not bool:
        raise ValueError("Invalid config sharing preference")
    profile = store.profile(pid)
    root = store.folder(pid)
    folders = build_archive._catalog_folders(root)
    sections = list(KINDS)
    if include_configs:
        sections += ["config", "defaultconfigs"]
    rows = []
    for path, parts, stat in build_archive._walk(
        root, sections, False, catalog_folders=folders, configs=include_configs
    ):
        lifecycle.check()
        rows.append(["/".join(parts), stat.st_size, stat.st_mtime_ns,
                     stat.st_ctime_ns])
    for name in ("options.txt", build_archive.STAMP):
        file = root / name
        try:
            stat = file.lstat()
        except FileNotFoundError:
            continue
        if not build_archive._reparse(stat) and file.is_file():
            rows.append([name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
    rows.sort(key=lambda row: row[0])
    payload = [profile["version"], profile["loader"], bool(include_configs), rows]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

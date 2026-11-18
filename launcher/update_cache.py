"""Cross-window release metadata cache and GitHub retry deadlines.

Only metadata lives here. Installer bytes still go through TransferManager and
the existing verified update handoff. The OS lock expires with its process;
no background service or token is needed to share results between windows.
"""

from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

from .lifecycle import check, guard
from .sessions import FileMutex
from .transfers import _atomic_json

CHECK_TTL = 5 * 60
MIN_RETRY = 60
MAX_RETRY = 60 * 60
MAX_SERVER_RETRY = 24 * 60 * 60


def number(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def header_values(headers):
    return {str(key).lower(): str(value) for key, value in (headers or {}).items()}


def retry_at(headers, *, now=None, failures=1):
    """Respect both primary reset and Retry-After; otherwise back off >=60s.

    GitHub normally resets the anonymous quota hourly. Bound malformed/far future
    header values to one day, and cap our exponential fallback at one hour.
    No active worker sleeps until this deadline: a later check uses the cache.
    """
    now = time.time() if now is None else now
    headers = header_values(headers)
    delay = min(MAX_RETRY, MIN_RETRY * 2 ** min(6, max(0, int(failures) - 1)))
    retry = headers.get("retry-after", "")
    seconds = number(retry, -1)
    if seconds < 0 and retry:
        try:
            seconds = parsedate_to_datetime(retry).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            seconds = -1
    if seconds >= 0:
        delay = max(delay, seconds)
    if headers.get("x-ratelimit-remaining") == "0":
        delay = max(delay, number(headers.get("x-ratelimit-reset")) - now + 1)
    return now + min(MAX_SERVER_RETRY, delay)


class SharedUpdateCache:
    def __init__(self, root, repository):
        self.root, self.repository = root, repository
        self.path = root / "release-index.json"
        self.lock_path = root / "release-index.lock"

    def _load(self):
        empty = {"format": 1, "repository": self.repository, "api": {}, "checks": {}, "versions": {}}
        if self.root.is_symlink() or self.path.is_symlink():
            raise ValueError("Кэш сервиса обновлений перенаправлен.")
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                return empty
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("format") != 1 or value.get("repository") != self.repository:
                return empty
            for key in ("api", "checks", "versions"):
                if not isinstance(value.get(key), dict):
                    value[key] = {}
            value["checks"] = {key: entry for key, entry in value["checks"].items()
                               if key in {"stable", "test"} and isinstance(entry, dict)}
            value["versions"] = {key: entry for key, entry in list(value["versions"].items())[-6:]
                                 if isinstance(entry, dict)}
            return value
        except (OSError, ValueError, UnicodeError):
            return empty

    def save(self, value):
        check()
        value["versions"] = dict(list(value["versions"].items())[-6:])
        with guard():
            _atomic_json(self.path, value)

    @contextmanager
    def lease(self, *, timeout=30):
        check()
        if self.root.is_symlink() or self.root.resolve().parent != self.root.parent.resolve():
            raise ValueError("Папка сервиса обновлений перенаправлена.")
        mutex = FileMutex(self.lock_path)
        deadline = time.monotonic() + timeout
        try:
            while not mutex.acquire():
                check()
                if time.monotonic() >= deadline:
                    raise ValueError("Обновления уже проверяются в другом окне BlockHarbor. Дождись завершения проверки.")
                time.sleep(0.05)
            check()
            yield self._load()
        finally:
            mutex.close()
